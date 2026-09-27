"""Analysis layer.

Two conventions are used throughout and it matters that they are kept straight,
because they have opposite signs and both are used on desks:

    wcs_minus_wti   WCS less WTI. Normally negative. This is how a quote sheet
                    shows it: "WCS Hardisty at -13.50 to WTI".
    discount        WTI less WCS. Normally positive. This is how people talk:
                    "the diff blew out to 19 dollars".

Every function that returns one of these names it explicitly. There is no
column called just "differential", because that is how sign errors ship.

Two alignment modes:

    monthly  The default, and what the free data supports. WCS from Alberta is
             a monthly average. Daily WTI is averaged to the same month before
             the spread is taken, so both legs are averages over the same
             window. Partial months are dropped.
    daily    For a paid or scraped daily WCS assessment. WTI is the reference
             calendar and the last WCS print is carried forward for a few
             sessions, with the carry recorded.

The wrong answer, and the common one, is to take a monthly WCS number, forward
fill it across every trading day, and difference it against daily WTI. That
produces a sawtooth that is an artefact of the fill, not a market move.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

WTI = "wti_spot"
WCS = "wcs_hardisty"

MONTHLY = "monthly"
DAILY = "daily"

# Minimum daily WTI observations before a month's average is trusted. Stops the
# current, incomplete month from entering the series as a real data point.
MIN_DAYS_PER_MONTH = 12

_RESAMPLE_RULE = {MONTHLY: "MS", DAILY: "W-FRI"}

# (1 period, medium, long) lookbacks used by the snapshot, per mode.
_LOOKBACKS = {
    MONTHLY: (1, 3, 12),
    DAILY: (1, 5, 21),
}
_LOOKBACK_LABELS = {
    MONTHLY: ("1 month", "3 months", "12 months"),
    DAILY: ("1 day", "1 week", "1 month"),
}


def long_to_wide(frame: pd.DataFrame) -> pd.DataFrame:
    """Pivot the long observations table into one column per metric."""
    if frame.empty:
        return pd.DataFrame()
    wide = frame.pivot_table(
        index="obs_date", columns="metric", values="value", aggfunc="last"
    )
    wide.index = pd.to_datetime(wide.index)
    return wide.sort_index()


def build_differential(
    wide: pd.DataFrame,
    frequency: str = MONTHLY,
    max_staleness_days: int = 3,
) -> pd.DataFrame:
    """Align the two legs and compute the differential."""
    for required in (WTI, WCS):
        if required not in wide.columns:
            raise KeyError(
                f"Column '{required}' is missing. Available: {list(wide.columns)}. "
                "Run the ingest step first."
            )

    if frequency == MONTHLY:
        frame = _align_monthly(wide)
    elif frequency == DAILY:
        frame = _align_daily(wide, max_staleness_days)
    else:
        raise ValueError(f"frequency must be '{MONTHLY}' or '{DAILY}', got {frequency!r}")

    if frame.empty:
        return _empty_differential()

    frame["wcs_minus_wti"] = frame["wcs_filled"] - frame[WTI]
    frame["discount"] = frame[WTI] - frame["wcs_filled"]
    frame["discount_pct_of_wti"] = np.where(
        frame[WTI] > 0, frame["discount"] / frame[WTI] * 100.0, np.nan
    )

    short, long = (3, 12) if frequency == MONTHLY else (20, 60)
    frame["discount_ma_short"] = (
        frame["discount"].rolling(short, min_periods=max(2, short // 3)).mean()
    )
    frame["discount_ma_long"] = (
        frame["discount"].rolling(long, min_periods=max(3, long // 3)).mean()
    )

    frame.index.name = "obs_date"
    frame.attrs["frequency"] = frequency
    return frame


def _align_monthly(wide: pd.DataFrame) -> pd.DataFrame:
    """Average daily WTI to month start and join the monthly WCS assessment."""
    wti_daily = wide[WTI].dropna()
    if wti_daily.empty:
        return pd.DataFrame()

    monthly = wti_daily.resample("MS")
    wti_monthly = monthly.mean()
    observation_count = monthly.count()
    # Drop the partial current month, and any month the feed under-covered.
    wti_monthly = wti_monthly[observation_count >= MIN_DAYS_PER_MONTH]

    wcs = wide[WCS].dropna()
    if wcs.empty:
        return pd.DataFrame()
    # Alberta stamps each monthly average on the first of the month. Normalise
    # anyway so a source that stamps month end still lines up.
    wcs.index = wcs.index.to_period("M").to_timestamp()
    wcs = wcs.groupby(level=0).last()

    frame = pd.DataFrame({WTI: wti_monthly}).join(
        wcs.rename("wcs_filled"), how="inner"
    )
    frame[WCS] = frame["wcs_filled"]
    frame["wti_days_in_month"] = observation_count.reindex(frame.index)
    frame["wcs_stale_days"] = 0
    frame["wcs_carried"] = False
    return frame.dropna(subset=[WTI, "wcs_filled"])


def _align_daily(wide: pd.DataFrame, max_staleness_days: int) -> pd.DataFrame:
    """WTI is the reference calendar; carry WCS forward a few sessions."""
    frame = wide.copy()
    frame = frame[frame[WTI].notna()]
    if frame.empty:
        return frame

    wcs = frame[WCS]
    observed = wcs.notna()
    group = observed.cumsum()
    stale = observed.groupby(group).cumcount()
    stale = stale.where(group > 0, other=np.nan)

    frame["wcs_filled"] = wcs.ffill()
    frame["wcs_stale_days"] = stale
    frame["wcs_carried"] = ~observed & frame["wcs_filled"].notna()

    return frame[
        frame["wcs_filled"].notna() & (frame["wcs_stale_days"] <= max_staleness_days)
    ]


def _empty_differential() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            WTI,
            WCS,
            "wcs_filled",
            "wcs_stale_days",
            "wcs_carried",
            "wcs_minus_wti",
            "discount",
            "discount_pct_of_wti",
            "discount_ma_short",
            "discount_ma_long",
        ]
    )


def periodic(frame: pd.DataFrame, frequency: str = MONTHLY) -> pd.DataFrame:
    """A period-over-period view.

    Monthly data is already at its natural grain, so this adds the change
    column. Daily data is averaged to weeks first, which is the right grain to
    compare against EIA inventory data.
    """
    if frame.empty:
        return frame

    columns = [c for c in (WTI, "wcs_filled", "discount") if c in frame.columns]
    if frequency == MONTHLY:
        result = frame[columns].copy()
    else:
        result = frame[columns].resample(_RESAMPLE_RULE[DAILY]).mean()

    result["discount_change"] = result["discount"].diff()
    return result.dropna(subset=["discount"])


@dataclass
class Snapshot:
    """Headline numbers for the top of the report."""

    as_of: str
    frequency: str
    wti: float
    wcs: float
    discount: float
    wcs_minus_wti: float
    discount_pct_of_wti: float
    change_short: float | None
    change_medium: float | None
    change_long: float | None
    change_labels: tuple[str, str, str]
    percentile_recent: float | None
    mean_recent: float | None
    min_recent: float | None
    max_recent: float | None
    recent_window: str
    wcs_carried: bool
    observations: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def snapshot(frame: pd.DataFrame, frequency: str = MONTHLY) -> Snapshot:
    """Summarise the latest observation against its own recent history."""
    if frame.empty:
        raise ValueError("Cannot build a snapshot from an empty frame.")

    latest = frame.iloc[-1]
    discount = frame["discount"]
    cutoff = discount.index[-1] - pd.Timedelta(days=365)
    recent = discount[discount.index >= cutoff]
    enough = len(recent) >= (6 if frequency == MONTHLY else 20)

    short, medium, long = _LOOKBACKS[frequency]

    return Snapshot(
        as_of=frame.index[-1].strftime("%Y-%m-%d"),
        frequency=frequency,
        wti=round(float(latest[WTI]), 2),
        wcs=round(float(latest["wcs_filled"]), 2),
        discount=round(float(latest["discount"]), 2),
        wcs_minus_wti=round(float(latest["wcs_minus_wti"]), 2),
        discount_pct_of_wti=round(float(latest["discount_pct_of_wti"]), 1),
        change_short=_change(discount, short),
        change_medium=_change(discount, medium),
        change_long=_change(discount, long),
        change_labels=_LOOKBACK_LABELS[frequency],
        percentile_recent=(
            round(float((recent <= discount.iloc[-1]).mean() * 100), 1)
            if enough
            else None
        ),
        mean_recent=round(float(recent.mean()), 2) if enough else None,
        min_recent=round(float(recent.min()), 2) if enough else None,
        max_recent=round(float(recent.max()), 2) if enough else None,
        recent_window="trailing 12 months",
        wcs_carried=bool(latest.get("wcs_carried", False)),
        observations=int(len(frame)),
    )


def _change(series: pd.Series, periods: int) -> float | None:
    if len(series) <= periods:
        return None
    return round(float(series.iloc[-1] - series.iloc[-1 - periods]), 2)


def regime_comparison(frame: pd.DataFrame, breaks: dict[str, str]) -> pd.DataFrame:
    """Average discount before and after named structural breaks.

    The obvious one is Trans Mountain Expansion entering service in May 2024.
    A full-period average that spans a capacity change is a number with no
    meaning, so the report shows the regimes separately.
    """
    if frame.empty or not breaks:
        return pd.DataFrame(columns=["period", "observations", "mean", "min", "max"])

    edges = sorted((pd.Timestamp(v), k) for k, v in breaks.items())

    # Half-open intervals [begin, end). A closed interval on both sides counts
    # the boundary period twice, which inflates the earlier regime's average.
    windows: list[tuple[str, pd.Timestamp, pd.Timestamp]] = [
        ("Before " + edges[0][1], frame.index[0], edges[0][0])
    ]
    for index, (stamp, name) in enumerate(edges):
        end = (
            edges[index + 1][0]
            if index + 1 < len(edges)
            else frame.index[-1] + pd.Timedelta(days=1)
        )
        windows.append((name + " onward", stamp, end))

    rows = []
    for label, begin, end in windows:
        window = frame.loc[(frame.index >= begin) & (frame.index < end), "discount"]
        if window.empty:
            continue
        rows.append(
            {
                "period": label,
                "observations": int(len(window)),
                "mean": round(float(window.mean()), 2),
                "min": round(float(window.min()), 2),
                "max": round(float(window.max()), 2),
            }
        )
    return pd.DataFrame(rows)


def correlate_drivers(
    frame: pd.DataFrame,
    wide: pd.DataFrame,
    drivers: list[str],
    frequency: str = MONTHLY,
    window_days: int = 1095,
) -> pd.DataFrame:
    """Correlation of period-over-period changes against driver changes.

    Deliberately on changes rather than levels. Two trending series correlate
    with each other and with the calendar, which tells you nothing. Differences
    are the honest version of the question.
    """
    empty = pd.DataFrame(columns=["driver", "observations", "correlation"])
    if frame.empty:
        return empty

    rule = _RESAMPLE_RULE[frequency]
    cutoff = frame.index[-1] - pd.Timedelta(days=window_days)
    discount_changes = (
        frame.loc[frame.index >= cutoff, "discount"].resample(rule).mean().diff()
    )

    results = []
    for driver in drivers:
        if driver not in wide.columns:
            continue
        series = wide.loc[wide.index >= cutoff, driver].dropna()
        if series.empty:
            continue
        driver_changes = series.resample(rule).mean().diff()
        joined = pd.concat(
            [discount_changes, driver_changes], axis=1, keys=["discount", driver]
        ).dropna()
        if len(joined) < 8:
            continue
        results.append(
            {
                "driver": driver,
                "observations": int(len(joined)),
                "correlation": round(float(joined["discount"].corr(joined[driver])), 3),
            }
        )

    if not results:
        return empty
    return pd.DataFrame(results).sort_values(
        "correlation", key=lambda s: s.abs(), ascending=False
    )


def data_quality(frame: pd.DataFrame, wide: pd.DataFrame, frequency: str) -> dict[str, Any]:
    """Facts a reader should know before trusting the numbers."""
    if frame.empty:
        return {"rows": 0}
    carried = int(frame["wcs_carried"].sum()) if "wcs_carried" in frame else 0
    quality: dict[str, Any] = {
        "alignment": frequency,
        "rows": int(len(frame)),
        "first_date": frame.index[0].strftime("%Y-%m-%d"),
        "last_date": frame.index[-1].strftime("%Y-%m-%d"),
        "metrics_available": sorted(wide.columns.tolist()),
    }
    if frequency == DAILY:
        quality["carried_wcs_prints"] = carried
        quality["carried_share_pct"] = round(carried / len(frame) * 100, 1)
    else:
        quality["wti_days_averaged_min"] = (
            int(frame["wti_days_in_month"].min())
            if "wti_days_in_month" in frame
            else None
        )
    return quality
