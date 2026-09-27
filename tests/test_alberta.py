"""Tests for the WCS source.

The fixture below mirrors the real Alberta Economic Data API response, quirks
included: a field named "Type " with a trailing space, null placeholder rows
from before WCS existed as a benchmark, and both WCS and WTI in one payload
when the caller does not filter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.sources import alberta

RESPONSE = [
    {"Date": "1986-01-01T00:00:00", "Type ": "WCS", "Unit": "$US/bbl", "Value": None},
    {"Date": "2004-12-01T00:00:00", "Type ": "WCS", "Unit": "$US/bbl", "Value": None},
    {"Date": "2005-01-01T00:00:00", "Type ": "WCS", "Unit": "$US/bbl", "Value": 29.42},
    {"Date": "2005-02-01T00:00:00", "Type ": "WCS", "Unit": "$US/bbl", "Value": 28.44},
    {"Date": "2005-01-01T00:00:00", "Type ": "WTI", "Unit": "$US/bbl", "Value": 46.84},
    {"Date": "2005-02-01T00:00:00", "Type ": "WTI", "Unit": "$US/bbl", "Value": 48.15},
]

BLOCK = {
    "metric": "wcs_hardisty",
    "units": "USD/bbl",
    "api_url": "https://example.invalid/data",
    "api_type": "WCS",
}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture
def stub_api(monkeypatch):
    def install(payload):
        monkeypatch.setattr(
            alberta.requests, "get", lambda *a, **k: FakeResponse(payload)
        )

    return install


def test_null_placeholder_rows_are_dropped(stub_api):
    stub_api(RESPONSE)
    rows, strategy = alberta.fetch(BLOCK, Path("/tmp"))

    assert strategy == "api"
    assert [r["obs_date"] for r in rows] == ["2005-01-01", "2005-02-01"]
    assert [r["value"] for r in rows] == [29.42, 28.44]


def test_wti_rows_are_filtered_out_so_series_do_not_interleave(stub_api):
    stub_api(RESPONSE)
    rows, _ = alberta.fetch(BLOCK, Path("/tmp"))

    # Two dates only, and the WCS value not the WTI one.
    assert len(rows) == 2
    assert rows[0]["value"] == 29.42
    assert all(r["metric"] == "wcs_hardisty" for r in rows)


def test_trailing_space_in_the_type_field_is_handled(stub_api):
    """The live feed really does name the column 'Type ' with a trailing space."""
    stub_api([dict(row) for row in RESPONSE])
    rows, _ = alberta.fetch(BLOCK, Path("/tmp"))
    assert len(rows) == 2


def test_missing_requested_type_is_an_error_not_a_silent_empty(stub_api):
    stub_api([r for r in RESPONSE if r["Type "] == "WTI"])
    with pytest.raises(alberta.SourceUnavailable, match="Every WCS strategy failed"):
        alberta.fetch(BLOCK, Path("/tmp"))


def test_falls_back_to_manual_csv_when_the_api_fails(stub_api, tmp_path):
    def boom(*_args, **_kwargs):
        raise ConnectionError("network down")

    stub_api(RESPONSE)
    alberta.requests.get = boom  # type: ignore[assignment]

    csv = tmp_path / "wcs.csv"
    csv.write_text("date,price\n2026-01-01,47.22\n2026-02-01,50.33\n", encoding="utf-8")

    block = dict(BLOCK, manual_csv=str(csv))
    rows, strategy = alberta.fetch(block, tmp_path)

    assert strategy == "manual_csv"
    assert [r["value"] for r in rows] == [47.22, 50.33]


def test_duplicate_dates_keep_the_last_value(stub_api):
    stub_api(
        [
            {"Date": "2026-01-01T00:00:00", "Type ": "WCS", "Value": 45.00},
            {"Date": "2026-01-01T00:00:00", "Type ": "WCS", "Value": 47.22},
        ]
    )
    rows, _ = alberta.fetch(BLOCK, Path("/tmp"))
    assert len(rows) == 1
    assert rows[0]["value"] == 47.22
