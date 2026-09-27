"""Tests for storage.

The property that matters most is idempotency: running the pipeline twice must
not duplicate rows or corrupt history. That is what makes it safe on a schedule.
"""

from __future__ import annotations

import pandas as pd

from src import db


def rows(*pairs):
    return [
        {
            "metric": "wti_spot",
            "obs_date": date,
            "value": value,
            "units": "USD/bbl",
            "source": "test",
        }
        for date, value in pairs
    ]


def test_upsert_is_idempotent(tmp_path):
    path = tmp_path / "test.db"
    db.init(path)

    with db.connect(path) as conn:
        db.upsert_observations(conn, rows(("2026-01-02", 70.0), ("2026-01-05", 71.0)))
    with db.connect(path) as conn:
        db.upsert_observations(conn, rows(("2026-01-02", 70.0), ("2026-01-05", 71.0)))

    frame = db.read_metrics(path)
    assert len(frame) == 2


def test_revision_overwrites_the_value(tmp_path):
    path = tmp_path / "test.db"
    db.init(path)

    with db.connect(path) as conn:
        db.upsert_observations(conn, rows(("2026-01-02", 70.0)))
    with db.connect(path) as conn:
        db.upsert_observations(conn, rows(("2026-01-02", 70.55)))

    frame = db.read_metrics(path)
    assert len(frame) == 1
    assert frame["value"].iloc[0] == 70.55


def test_null_values_are_skipped_not_stored_as_zero(tmp_path):
    path = tmp_path / "test.db"
    db.init(path)

    with db.connect(path) as conn:
        written = db.upsert_observations(
            conn,
            [
                {"metric": "wti_spot", "obs_date": "2026-01-02", "value": None, "source": "test"},
                {"metric": "wti_spot", "obs_date": "2026-01-05", "value": 71.0, "source": "test"},
            ],
        )

    assert written == 1
    frame = db.read_metrics(path)
    assert frame["obs_date"].tolist() == [pd.Timestamp("2026-01-05")]


def test_read_metrics_filters_by_metric_and_start(tmp_path):
    path = tmp_path / "test.db"
    db.init(path)

    with db.connect(path) as conn:
        db.upsert_observations(conn, rows(("2026-01-02", 70.0), ("2026-02-02", 71.0)))
        db.upsert_observations(
            conn,
            [
                {
                    "metric": "wcs_hardisty",
                    "obs_date": "2026-01-02",
                    "value": 56.0,
                    "source": "test",
                }
            ],
        )

    assert len(db.read_metrics(path, metrics=["wti_spot"])) == 2
    assert len(db.read_metrics(path, start="2026-02-01")) == 1


def test_coverage_reports_range_per_metric(tmp_path):
    path = tmp_path / "test.db"
    db.init(path)
    with db.connect(path) as conn:
        db.upsert_observations(conn, rows(("2026-01-02", 70.0), ("2026-03-02", 71.0)))

    frame = db.coverage(path)
    assert frame["metric"].tolist() == ["wti_spot"]
    assert frame["rows"].iloc[0] == 2
    assert frame["first_date"].iloc[0] == "2026-01-02"
    assert frame["last_date"].iloc[0] == "2026-03-02"
