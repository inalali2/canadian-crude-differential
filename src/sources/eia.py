"""EIA Open Data API v2 client.

The v2 API is paginated (5000 rows per call) and returns a JSON envelope. This
module handles paging, retries, and translates the response into the flat row
shape the storage layer expects.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import requests

from ..config import Config, SeriesSpec, eia_api_key

LOG = logging.getLogger(__name__)

PAGE_SIZE = 5000
MAX_RETRIES = 4
BACKOFF_SECONDS = 2.0
TIMEOUT = 45


class EIAError(RuntimeError):
    pass


def _redact(params: dict[str, Any]) -> dict[str, Any]:
    """Copy of params with the API key masked, for logging.

    Never log the raw params. requests' own DEBUG logging prints the full URL,
    which is why urllib3 debug logging is silenced in the CLI.
    """
    safe = dict(params)
    if "api_key" in safe:
        safe["api_key"] = "***redacted***"
    return safe


def _get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET with retry on transient failures."""
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, params=params, timeout=TIMEOUT)
            if response.status_code == 403:
                raise EIAError(
                    "EIA returned 403. The API key is missing or invalid."
                )
            if response.status_code == 404:
                raise EIAError(
                    f"EIA returned 404 for {url}. The route is probably wrong. "
                    "Check config.yaml against https://www.eia.gov/opendata/browser/"
                )
            if response.status_code >= 500 or response.status_code == 429:
                raise requests.HTTPError(f"status {response.status_code}")
            response.raise_for_status()
            return response.json()
        except EIAError:
            raise
        except Exception as exc:  # noqa: BLE001 - retry anything transient
            last_error = exc
            if attempt == MAX_RETRIES:
                break
            sleep_for = BACKOFF_SECONDS * attempt
            LOG.warning(
                "EIA request failed (attempt %s/%s): %s. Params: %s. Retrying in %.0fs",
                attempt,
                MAX_RETRIES,
                exc,
                _redact(params),
                sleep_for,
            )
            time.sleep(sleep_for)
    raise EIAError(f"EIA request failed after {MAX_RETRIES} attempts: {last_error}")


def fetch_series(
    config: Config,
    spec: SeriesSpec,
    start: str,
    end: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch one series and return rows ready for the storage layer."""
    url = f"{config.eia_base_url}/{spec.route}/data/"
    base_params: dict[str, Any] = {
        "api_key": eia_api_key(),
        "frequency": spec.frequency,
        "data[0]": "value",
        "facets[series][]": spec.id,
        "start": start,
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
        "length": PAGE_SIZE,
    }
    if end:
        base_params["end"] = end

    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = dict(base_params, offset=offset)
        payload = _get(url, params)
        block = payload.get("response", {}) or {}
        data = block.get("data", []) or []

        for record in data:
            period = record.get("period")
            value = record.get("value")
            if period is None:
                continue
            rows.append(
                {
                    "metric": spec.metric,
                    "obs_date": _normalise_period(period),
                    "value": _coerce_float(value),
                    "units": spec.units or record.get("units", ""),
                    "source": f"eia:{spec.id}",
                }
            )

        if len(data) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    if not rows:
        raise EIAError(
            f"EIA returned no rows for series '{spec.id}' on route "
            f"'{spec.route}'. The id may be retired. Run: "
            f"python -m src.cli discover --route {spec.route}"
        )
    return rows


def _normalise_period(period: str) -> str:
    """EIA periods are YYYY, YYYY-MM or YYYY-MM-DD. Normalise to a date."""
    period = str(period)
    if len(period) == 4:
        return f"{period}-01-01"
    if len(period) == 7:
        return f"{period}-01"
    return period[:10]


def _coerce_float(value: Any) -> float | None:
    if value in (None, "", "NA", "--"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def discover_series(
    config: Config,
    route: str,
    contains: str | None = None,
) -> Iterator[tuple[str, str]]:
    """Yield (series_id, description) for a route.

    Series ids change and get retired. Rather than hardcoding guesses, ask the
    API what exists. This is how you fill in the commented-out entries in
    config.yaml.
    """
    url = f"{config.eia_base_url}/{route.strip('/')}/facet/series/"
    payload = _get(url, {"api_key": eia_api_key()})
    facets = payload.get("response", {}).get("facets", []) or []
    needle = contains.lower() if contains else None
    for facet in facets:
        series_id = str(facet.get("id", ""))
        name = str(facet.get("name", ""))
        if needle and needle not in name.lower() and needle not in series_id.lower():
            continue
        yield series_id, name
