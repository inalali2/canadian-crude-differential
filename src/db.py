"""SQLite storage.

Design notes worth being able to defend in an interview:

* One long table, not one column per series. New sources need no migration.
* The primary key is (metric, obs_date), so re-running the pipeline updates rows
  instead of duplicating them. The whole thing is safe to run on a cron.
* Every row keeps its source and the time it was written, so a bad print can be
  traced back to where it came from.
* Revisions are real. EIA restates weekly stocks, so an upsert that overwrites
  the value but preserves the first-seen timestamp is the correct behaviour.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    metric      TEXT NOT NULL,
    obs_date    TEXT NOT NULL,
    value       REAL,
    units       TEXT,
    source      TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (metric, obs_date)
);

CREATE INDEX IF NOT EXISTS idx_observations_date ON observations (obs_date);

CREATE TABLE IF NOT EXISTS ingest_log (
    run_id      TEXT NOT NULL,
    metric      TEXT NOT NULL,
    source      TEXT NOT NULL,
    rows_seen   INTEGER NOT NULL,
    rows_written INTEGER NOT NULL,
    min_date    TEXT,
    max_date    TEXT,
    status      TEXT NOT NULL,
    detail      TEXT,
    logged_at   TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init(path: str | Path) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def upsert_observations(
    conn: sqlite3.Connection,
    rows: Sequence[dict],
) -> int:
    """Insert or update observations. Returns the number of rows touched.

    Expected keys per row: metric, obs_date (YYYY-MM-DD), value, units, source.
    Rows with a null value are skipped rather than stored, because a null here
    means the source had a gap, not that the price was zero.
    """
    now = utc_now()
    payload = []
    for row in rows:
        if row.get("value") is None:
            continue
        payload.append(
            (
                row["metric"],
                str(row["obs_date"])[:10],
                float(row["value"]),
                row.get("units", ""),
                row.get("source", "unknown"),
                now,
                now,
            )
        )
    if not payload:
        return 0

    conn.executemany(
        """
        INSERT INTO observations
            (metric, obs_date, value, units, source, first_seen, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (metric, obs_date) DO UPDATE SET
            value      = excluded.value,
            units      = excluded.units,
            source     = excluded.source,
            updated_at = excluded.updated_at
        WHERE observations.value IS NOT excluded.value
        """,
        payload,
    )
    return len(payload)


def log_ingest(
    conn: sqlite3.Connection,
    run_id: str,
    metric: str,
    source: str,
    rows_seen: int,
    rows_written: int,
    min_date: str | None,
    max_date: str | None,
    status: str,
    detail: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO ingest_log
            (run_id, metric, source, rows_seen, rows_written,
             min_date, max_date, status, detail, logged_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            metric,
            source,
            rows_seen,
            rows_written,
            min_date,
            max_date,
            status,
            detail[:2000],
            utc_now(),
        ),
    )


def read_metrics(
    path: str | Path,
    metrics: Iterable[str] | None = None,
    start: str | None = None,
) -> pd.DataFrame:
    """Return a long dataframe of observations."""
    query = "SELECT metric, obs_date, value, units, source FROM observations"
    clauses: list[str] = []
    params: list[object] = []

    metrics = list(metrics) if metrics else []
    if metrics:
        clauses.append(f"metric IN ({','.join('?' * len(metrics))})")
        params.extend(metrics)
    if start:
        clauses.append("obs_date >= ?")
        params.append(start)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY obs_date"

    with connect(path) as conn:
        frame = pd.read_sql_query(query, conn, params=params)

    if frame.empty:
        return pd.DataFrame(columns=["metric", "obs_date", "value", "units", "source"])
    frame["obs_date"] = pd.to_datetime(frame["obs_date"])
    return frame


def coverage(path: str | Path) -> pd.DataFrame:
    """Per metric row counts and date ranges. Used by the `status` command."""
    query = """
        SELECT metric,
               source,
               COUNT(*)      AS rows,
               MIN(obs_date) AS first_date,
               MAX(obs_date) AS last_date,
               MAX(updated_at) AS last_updated
        FROM observations
        GROUP BY metric, source
        ORDER BY metric
    """
    with connect(path) as conn:
        return pd.read_sql_query(query, conn)
