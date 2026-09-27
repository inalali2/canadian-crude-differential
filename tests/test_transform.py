"""Tests for the analysis layer.

The interesting cases are the ones that bite in production: a monthly leg
joined against a daily leg, a partial current month, a quote that does not
print every day, and the sign convention on the spread.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import transform
from src.transform import DAILY, MONTHLY


def daily_wti(start: str, periods: int, value: float = 70.0) -> pd.Series:
    return pd.Series(value, index=pd.bdate_range(start, periods=periods), name=transform.WTI)


def monthly_wcs(pairs: dict[str, float]) -> pd.Series:
    return pd.Series(
        list(pairs.values()),
        index=pd.to_datetime(list(pairs)),
        name=transform.WCS,
    )


def test_long_to_wide_pivots_metrics():
    long_frame = pd.DataFrame(
        {
            "metric": ["wti_spot", "wcs_hardisty", "wti_spot"],
            "obs_date": pd.to_datetime(["2026-01-02", "2026-01-01", "2026-01-05"]),
            "value": [70.0, 56.0, 71.0],
        }
    )
    wide = transform.long_to_wide(long_frame)
    assert list(wide.columns) == ["wcs_hardisty", "wti_spot"]
    assert wide.loc["2026-01-02", "wti_spot"] == 70.0


# Monthly alignment ---------------------------------------------------------


def test_monthly_averages_daily_wti_before_spreading():
    # Two full months of WTI at a known average, one monthly WCS print each.
    index = pd.bdate_range("2026-01-01", "2026-02-27")
    wti = pd.Series(
        np.where(index.month == 1, 80.0, 60.0), index=index, name=transform.WTI
    )
    wcs = monthly_wcs({"2026-01-01": 65.0, "2026-02-01": 50.0})

    frame = transform.build_differential(
        pd.DataFrame({transform.WTI: wti}).join(wcs, how="outer"), MONTHLY
    )

    assert len(frame) == 2
    assert frame[transform.WTI].tolist() == pytest.approx([80.0, 60.0])
    # Spread is against the month's average WTI, not the last daily print.
    assert frame["discount"].tolist() == pytest.approx([15.0, 10.0])
    assert frame["wcs_minus_wti"].tolist() == pytest.approx([-15.0, -10.0])


def test_partial_current_month_is_dropped():
    # January complete, February only three trading days in.
    index = pd.bdate_range("2026-01-01", "2026-01-30").append(
        pd.bdate_range("2026-02-02", periods=3)
    )
    wti = pd.Series(70.0, index=index, name=transform.WTI)
    wcs = monthly_wcs({"2026-01-01": 56.0, "2026-02-01": 55.0})

    frame = transform.build_differential(
        pd.DataFrame({transform.WTI: wti}).join(wcs, how="outer"), MONTHLY
    )

    assert len(frame) == 1
    assert frame.index[0] == pd.Timestamp("2026-01-01")


def test_monthly_never_marks_a_value_as_carried():
    index = pd.bdate_range("2026-01-01", "2026-03-31")
    wti = pd.Series(70.0, index=index, name=transform.WTI)
    wcs = monthly_wcs(
        {"2026-01-01": 56.0, "2026-02-01": 55.0, "2026-03-01": 54.0}
    )
    frame = transform.build_differential(
        pd.DataFrame({transform.WTI: wti}).join(wcs, how="outer"), MONTHLY
    )
    assert not frame["wcs_carried"].any()
    assert (frame["wti_days_in_month"] >= transform.MIN_DAYS_PER_MONTH).all()


def test_month_end_stamped_source_still_aligns():
    """A source stamping the month end must land on the same row as month start."""
    index = pd.bdate_range("2026-01-01", "2026-01-30")
    wti = pd.Series(70.0, index=index, name=transform.WTI)
    wcs = monthly_wcs({"2026-01-31": 56.0})

    frame = transform.build_differential(
        pd.DataFrame({transform.WTI: wti}).join(wcs, how="outer"), MONTHLY
    )
    assert len(frame) == 1
    assert frame["discount"].iloc[0] == pytest.approx(14.0)


# Daily alignment -----------------------------------------------------------


def test_daily_sign_convention():
    wide = pd.DataFrame(
        {transform.WTI: [70.0], transform.WCS: [56.0]},
        index=pd.to_datetime(["2026-01-02"]),
    )
    frame = transform.build_differential(wide, DAILY)

    assert frame["discount"].iloc[0] == pytest.approx(14.0)
    assert frame["wcs_minus_wti"].iloc[0] == pytest.approx(-14.0)
    assert frame["discount_pct_of_wti"].iloc[0] == pytest.approx(20.0)


def test_daily_missing_wcs_is_carried_and_flagged():
    index = pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"])
    wide = pd.DataFrame(
        {transform.WTI: [70.0, 72.0, 71.0], transform.WCS: [56.0, np.nan, np.nan]},
        index=index,
    )
    frame = transform.build_differential(wide, DAILY, max_staleness_days=3)

    assert frame["wcs_filled"].tolist() == [56.0, 56.0, 56.0]
    assert frame["wcs_carried"].tolist() == [False, True, True]
    assert frame["wcs_stale_days"].tolist() == [0, 1, 2]


def test_daily_stale_wcs_beyond_threshold_is_dropped_not_invented():
    days = pd.bdate_range("2026-01-05", periods=10)
    wide = pd.DataFrame(
        {transform.WTI: np.linspace(70, 75, 10), transform.WCS: [56.0] + [np.nan] * 9},
        index=days,
    )
    frame = transform.build_differential(wide, DAILY, max_staleness_days=2)

    assert len(frame) == 3
    assert frame.index[-1] == days[2]


def test_rows_before_the_first_wcs_print_are_excluded():
    index = pd.to_datetime(["2026-01-02", "2026-01-05"])
    wide = pd.DataFrame(
        {transform.WTI: [70.0, 72.0], transform.WCS: [np.nan, 58.0]}, index=index
    )
    frame = transform.build_differential(wide, DAILY)
    assert len(frame) == 1
    assert frame.index[0] == pd.Timestamp("2026-01-05")


# Errors --------------------------------------------------------------------


def test_missing_column_raises_a_useful_error():
    wide = pd.DataFrame({transform.WTI: [70.0]}, index=pd.to_datetime(["2026-01-02"]))
    with pytest.raises(KeyError, match="wcs_hardisty"):
        transform.build_differential(wide)


def test_unknown_frequency_is_rejected():
    index = pd.bdate_range("2026-01-01", periods=20)
    wide = pd.DataFrame(
        {transform.WTI: 70.0, transform.WCS: 56.0}, index=index
    )
    with pytest.raises(ValueError, match="frequency"):
        transform.build_differential(wide, "hourly")


# Summary -------------------------------------------------------------------


def test_snapshot_reports_changes_and_percentile():
    index = pd.bdate_range("2023-01-01", "2026-08-31")
    wti = pd.Series(75.0, index=index, name=transform.WTI)
    months = pd.date_range("2023-01-01", "2026-08-01", freq="MS")
    # Discount widens steadily month over month.
    wcs = pd.Series(
        75.0 - np.linspace(10, 20, len(months)), index=months, name=transform.WCS
    )
    frame = transform.build_differential(
        pd.DataFrame({transform.WTI: wti}).join(wcs, how="outer"), MONTHLY
    )
    snap = transform.snapshot(frame, MONTHLY)

    assert snap.frequency == MONTHLY
    assert snap.change_labels == ("1 month", "3 months", "12 months")
    assert snap.wti == pytest.approx(75.0)
    assert snap.change_short is not None and snap.change_short > 0
    assert snap.change_long is not None and snap.change_long > snap.change_short
    assert snap.percentile_recent is not None and snap.percentile_recent > 90


def test_periodic_adds_change_column_without_resampling_monthly():
    index = pd.bdate_range("2026-01-01", "2026-03-31")
    wti = pd.Series(70.0, index=index, name=transform.WTI)
    wcs = monthly_wcs({"2026-01-01": 56.0, "2026-02-01": 54.0, "2026-03-01": 52.0})
    frame = transform.build_differential(
        pd.DataFrame({transform.WTI: wti}).join(wcs, how="outer"), MONTHLY
    )

    result = transform.periodic(frame, MONTHLY)
    assert len(result) == 3
    assert result["discount"].tolist() == pytest.approx([14.0, 16.0, 18.0])
    assert result["discount_change"].iloc[-1] == pytest.approx(2.0)


def test_regime_comparison_splits_at_the_break():
    index = pd.bdate_range("2023-01-01", "2025-12-31")
    wti = pd.Series(75.0, index=index, name=transform.WTI)
    months = pd.date_range("2023-01-01", "2025-12-01", freq="MS")
    # Wide before the break, narrow after.
    discount = np.where(months < pd.Timestamp("2024-05-01"), 20.0, 12.0)
    wcs = pd.Series(75.0 - discount, index=months, name=transform.WCS)
    frame = transform.build_differential(
        pd.DataFrame({transform.WTI: wti}).join(wcs, how="outer"), MONTHLY
    )

    regimes = transform.regime_comparison(frame, {"TMX in service": "2024-05-01"})
    assert len(regimes) == 2
    assert regimes["mean"].tolist() == pytest.approx([20.0, 12.0])
    assert "TMX in service" in regimes["period"].iloc[1]


def test_correlation_uses_changes_so_a_shared_trend_does_not_fake_a_signal():
    index = pd.bdate_range("2021-01-01", "2026-08-31")
    wti = pd.Series(75.0, index=index, name=transform.WTI)
    months = pd.date_range("2021-01-01", "2026-08-01", freq="MS")
    rng = np.random.default_rng(3)

    # Discount and the driver both trend up, but their month over month changes
    # are independent noise. A level correlation would be near 1.
    discount = np.linspace(10, 20, len(months)) + rng.normal(0, 1.5, len(months))
    driver = pd.Series(
        np.linspace(100, 200, len(months)) + rng.normal(0, 12, len(months)),
        index=months,
    )
    wcs = pd.Series(75.0 - discount, index=months, name=transform.WCS)

    wide = pd.DataFrame({transform.WTI: wti}).join(wcs, how="outer")
    wide["cushing_stocks"] = driver
    frame = transform.build_differential(wide, MONTHLY)

    result = transform.correlate_drivers(
        frame, wide, ["cushing_stocks"], MONTHLY
    )
    assert len(result) == 1
    assert abs(result["correlation"].iloc[0]) < 0.5


def test_data_quality_reports_alignment_mode():
    index = pd.bdate_range("2026-01-01", "2026-03-31")
    wti = pd.Series(70.0, index=index, name=transform.WTI)
    wcs = monthly_wcs({"2026-01-01": 56.0, "2026-02-01": 55.0, "2026-03-01": 54.0})
    wide = pd.DataFrame({transform.WTI: wti}).join(wcs, how="outer")
    frame = transform.build_differential(wide, MONTHLY)

    quality = transform.data_quality(frame, wide, MONTHLY)
    assert quality["alignment"] == MONTHLY
    assert quality["rows"] == 3
    assert quality["wti_days_averaged_min"] >= transform.MIN_DAYS_PER_MONTH
