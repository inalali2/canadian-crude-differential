"""Western Canadian Select at Hardisty.

Source: the Government of Alberta Economic Data API, linked from the WCS oil
price page on the Alberta Economic Dashboard.

    https://api.economicdata.alberta.ca/data?table=OilPrices&Type=WCS

Two things about this feed shape the whole project:

1. **It is monthly, not daily.** These are monthly average assessments, not
   settlement prints. A daily WCS assessment is a commercial product (Argus,
   Platts, NE2) and is not free. So the differential here is a monthly average
   differential, and the code says so rather than pretending otherwise by
   smearing one monthly number across 21 trading days.

2. **The JSON has a quirk.** The type field is named `"Type "` with a trailing
   space, and the feed carries null placeholder rows back to 1986, from before
   WCS existed as a benchmark. Handling both is not an embarrassing workaround,
   it is the normal condition of public data feeds.

Strategies, in order: the API, then a manually maintained CSV.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd
import requests

LOG = logging.getLogger(__name__)

TIMEOUT = 45
HEADERS = {
    "User-Agent": (
        "canadian-crude-differential/1.0 "
        "(personal research project; contact via GitHub)"
    )
}

DATE_CANDIDATES = {"date", "period", "month", "day", "timestamp"}
VALUE_CANDIDATES = {"value", "price", "close"}


class SourceUnavailable(RuntimeError):
    """Raised when a strategy cannot produce rows."""


def fetch(config_block: dict[str, Any], project_root: Path) -> tuple[list[dict], str]:
    """Return (rows, strategy_used)."""
    metric = config_block.get("metric", "wcs_hardisty")
    units = config_block.get("units", "USD/bbl")
    errors: list[str] = []

    for strategy, loader in (("api", _from_api), ("manual_csv", _from_manual_csv)):
        try:
            frame = loader(config_block, project_root)
        except SourceUnavailable as exc:
            errors.append(f"{strategy}: {exc}")
            LOG.warning("WCS strategy '%s' unavailable: %s", strategy, exc)
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{strategy}: {exc}")
            LOG.warning("WCS strategy '%s' failed: %s", strategy, exc)
            continue

        rows = _to_rows(frame, metric, units, f"alberta:{strategy}")
        if rows:
            LOG.info(
                "WCS loaded via '%s': %s rows, %s to %s",
                strategy,
                len(rows),
                rows[0]["obs_date"],
                rows[-1]["obs_date"],
            )
            return rows, strategy
        errors.append(f"{strategy}: parsed but produced no usable rows")

    raise SourceUnavailable("Every WCS strategy failed:\n  " + "\n  ".join(errors))


# Strategies ----------------------------------------------------------------


def _from_api(block: dict[str, Any], _root: Path) -> pd.DataFrame:
    url = block.get("api_url")
    if not url:
        raise SourceUnavailable("no api_url configured")

    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json()

    records = payload
    if isinstance(payload, dict):
        for key in ("data", "results", "items", "rows"):
            if isinstance(payload.get(key), list):
                records = payload[key]
                break
    if not isinstance(records, list) or not records:
        raise SourceUnavailable("response was not a non-empty list of records")

    frame = pd.json_normalize(records)
    frame.columns = [str(c).strip() for c in frame.columns]

    # Guard against the endpoint returning WCS and WTI together. Without this
    # the two series interleave into one metric and every number is wrong.
    wanted = block.get("api_type", "WCS")
    if "Type" in frame.columns and wanted:
        matching = frame[frame["Type"].astype(str).str.strip() == wanted]
        if matching.empty:
            present = sorted(frame["Type"].astype(str).str.strip().unique())[:8]
            raise SourceUnavailable(
                f"no rows with Type='{wanted}'. Present: {present}"
            )
        frame = matching

    return frame


def _from_manual_csv(block: dict[str, Any], root: Path) -> pd.DataFrame:
    rel = block.get("manual_csv")
    if not rel:
        raise SourceUnavailable("no manual_csv configured")
    path = Path(rel)
    if not path.is_absolute():
        path = root / path
    if not path.exists():
        raise SourceUnavailable(f"{path} does not exist")
    frame = pd.read_csv(path)
    if frame.empty:
        raise SourceUnavailable(f"{path} is empty")
    return frame


# Shaping -------------------------------------------------------------------


def _pick_column(frame: pd.DataFrame, candidates: set[str]) -> str | None:
    for column in frame.columns:
        name = str(column).strip().lower()
        if any(word in name for word in candidates):
            return column
    return None


def _to_rows(
    frame: pd.DataFrame,
    metric: str,
    units: str,
    source: str,
) -> list[dict]:
    date_col = _pick_column(frame, DATE_CANDIDATES)
    value_col = _pick_column(frame, VALUE_CANDIDATES)
    if date_col is None or value_col is None:
        raise SourceUnavailable(
            f"could not identify date/value columns in {list(frame.columns)[:10]}"
        )

    shaped = frame[[date_col, value_col]].copy()
    shaped.columns = ["obs_date", "value"]
    shaped["obs_date"] = pd.to_datetime(shaped["obs_date"], errors="coerce")
    shaped["value"] = pd.to_numeric(
        shaped["value"].astype(str).str.replace(r"[^0-9.\-]", "", regex=True),
        errors="coerce",
    )
    shaped = shaped.dropna().sort_values("obs_date")
    shaped = shaped.drop_duplicates(subset="obs_date", keep="last")

    return [
        {
            "metric": metric,
            "obs_date": row.obs_date.strftime("%Y-%m-%d"),
            "value": float(row.value),
            "units": units,
            "source": source,
        }
        for row in shaped.itertuples()
    ]
