"""Enbridge Mainline apportionment.

Apportionment is the percentage of nominated barrels the pipeline cannot accept
in a given month. When the Mainline is apportioned, barrels back up in Alberta
and the WCS differential widens. It is the most useful non-price series in this
project, which is why it is worth maintaining by hand.

Enbridge publishes monthly notices as web pages and PDFs and moves the page
periodically. There is no stable feed and no documented API, so scraping it
would be a liability dressed up as automation. This module reads a CSV you
maintain. Set `apportionment.index_url` in config.yaml if you later find a
stable source and want to extend this.

CSV format (data/manual/apportionment.csv):

    obs_date,value
    2026-08-01,12
    2026-09-01,18

One row per month, stamped on the first of the month, value as a percentage.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

LOG = logging.getLogger(__name__)


class SourceUnavailable(RuntimeError):
    pass


def fetch(config_block: dict[str, Any], project_root: Path) -> tuple[list[dict], str]:
    metric = config_block.get("metric", "mainline_apportionment")
    units = config_block.get("units", "percent")

    rel = config_block.get("manual_csv")
    if not rel:
        raise SourceUnavailable("no manual_csv configured")

    path = Path(rel)
    if not path.is_absolute():
        path = project_root / path
    if not path.exists():
        raise SourceUnavailable(
            f"{path} does not exist. Apportionment is optional: create the file "
            "with columns obs_date,value to add it to the analysis."
        )

    frame = pd.read_csv(path)
    expected = {"obs_date", "value"}
    if not expected.issubset(frame.columns):
        raise SourceUnavailable(
            f"{path} needs columns {sorted(expected)}, found {list(frame.columns)}"
        )

    frame["obs_date"] = pd.to_datetime(frame["obs_date"], errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame = frame.dropna(subset=["obs_date", "value"]).sort_values("obs_date")
    frame = frame.drop_duplicates(subset="obs_date", keep="last")

    if frame.empty:
        raise SourceUnavailable(f"{path} has no usable rows")

    out_of_range = frame[(frame["value"] < 0) | (frame["value"] > 100)]
    if not out_of_range.empty:
        raise SourceUnavailable(
            f"{path} has {len(out_of_range)} value(s) outside 0 to 100. "
            "Apportionment is a percentage."
        )

    LOG.info("Apportionment loaded from %s (%s rows)", path.name, len(frame))
    return [
        {
            "metric": metric,
            "obs_date": row.obs_date.strftime("%Y-%m-%d"),
            "value": float(row.value),
            "units": units,
            "source": "enbridge:manual",
        }
        for row in frame.itertuples()
    ], "manual_csv"
