"""Report output.

Produces three artefacts per run:

    output/crude_differential_<date>.xlsx   the deliverable
    output/discount_history.png             chart for the README
    output/recap_<date>.md                  a short written recap

The Excel file uses a native Excel chart rather than a pasted image, so it stays
live when someone sorts or extends the sheet. That is the difference between a
file a desk keeps and a file a desk opens once.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from openpyxl import Workbook  # noqa: E402
from openpyxl.chart import LineChart, Reference  # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

from .transform import MONTHLY, Snapshot  # noqa: E402

LOG = logging.getLogger(__name__)

FONT = "Arial"
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(name=FONT, bold=True, color="FFFFFF", size=10)
TITLE_FONT = Font(name=FONT, bold=True, size=14)
LABEL_FONT = Font(name=FONT, bold=True, size=10)
BODY_FONT = Font(name=FONT, size=10)
NOTE_FONT = Font(name=FONT, size=9, italic=True, color="595959")
THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

MONEY = '$#,##0.00;($#,##0.00);"-"'
PCT = '0.0"%"'


def build_all(
    aligned: pd.DataFrame,
    periodic_frame: pd.DataFrame,
    snap: Snapshot,
    drivers: pd.DataFrame,
    regimes: pd.DataFrame,
    quality: dict[str, Any],
    output_dir: Path,
    wti_daily: pd.Series | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()

    chart_path = output_dir / "discount_history.png"
    _write_chart(aligned, snap, chart_path, wti_daily)

    xlsx_path = output_dir / f"crude_differential_{stamp}.xlsx"
    _write_workbook(
        aligned, periodic_frame, snap, drivers, regimes, quality, xlsx_path
    )

    recap_path = output_dir / f"recap_{stamp}.md"
    _write_recap(snap, drivers, regimes, quality, recap_path)

    return {"workbook": xlsx_path, "chart": chart_path, "recap": recap_path}


# Chart ---------------------------------------------------------------------


def _write_chart(
    aligned: pd.DataFrame,
    snap: Snapshot,
    path: Path,
    wti_daily: pd.Series | None = None,
) -> None:
    if aligned.empty:
        LOG.warning("No data to chart")
        return

    grain = "Monthly average" if snap.frequency == MONTHLY else "Daily"

    fig, (top, bottom) = plt.subplots(
        2, 1, figsize=(11, 7), sharex=True, height_ratios=[2, 1]
    )

    if wti_daily is not None and not wti_daily.empty:
        window = wti_daily[wti_daily.index >= aligned.index[0]]
        top.plot(
            window.index,
            window.values,
            linewidth=0.6,
            alpha=0.35,
            label="WTI daily",
        )
    top.plot(aligned.index, aligned["wti_spot"], linewidth=1.3, label="WTI (monthly avg)")
    top.plot(aligned.index, aligned["wcs_filled"], linewidth=1.3, label="WCS Hardisty")
    top.set_ylabel("USD per barrel")
    top.set_title(f"WTI and WCS Hardisty, and the discount ({grain.lower()})")
    top.legend(frameon=False, loc="upper left", fontsize=9)
    top.grid(alpha=0.25)

    bottom.fill_between(aligned.index, aligned["discount"], alpha=0.3)
    bottom.plot(aligned.index, aligned["discount"], linewidth=1.1, label="Discount")
    if aligned["discount_ma_long"].notna().any():
        bottom.plot(
            aligned.index,
            aligned["discount_ma_long"],
            linewidth=1.3,
            linestyle="--",
            label="Long average",
        )
    bottom.set_ylabel("WTI less WCS (USD)")
    bottom.legend(frameon=False, loc="upper left", fontsize=9)
    bottom.grid(alpha=0.25)

    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    LOG.info("Wrote %s", path)


# Workbook ------------------------------------------------------------------


def _write_workbook(
    aligned: pd.DataFrame,
    periodic_frame: pd.DataFrame,
    snap: Snapshot,
    drivers: pd.DataFrame,
    regimes: pd.DataFrame,
    quality: dict[str, Any],
    path: Path,
) -> None:
    book = Workbook()
    _summary_sheet(book.active, snap, drivers, regimes, quality)
    _series_sheet(book.create_sheet("Series"), aligned, snap)
    _change_sheet(book.create_sheet("Changes"), periodic_frame)
    book.save(path)
    LOG.info("Wrote %s", path)


def _summary_sheet(sheet, snap, drivers, regimes, quality) -> None:
    sheet.title = "Summary"
    sheet.sheet_view.showGridLines = False
    for column, width in (("A", 34), ("B", 16), ("C", 14), ("D", 46)):
        sheet.column_dimensions[column].width = width

    sheet["A1"] = "Canadian Crude Differential Recap"
    sheet["A1"].font = TITLE_FONT
    grain = "monthly averages" if snap.frequency == MONTHLY else "daily prints"
    sheet["A2"] = f"As of {snap.as_of}. Built from {grain}."
    sheet["A2"].font = NOTE_FONT

    short, medium, long = snap.change_labels
    rows = [
        ("WTI Cushing", snap.wti, MONEY, "EIA series RWTC"),
        ("WCS Hardisty", snap.wcs, MONEY, "Alberta Economic Data API"),
        ("Discount (WTI less WCS)", snap.discount, MONEY, "Computed"),
        ("Quoted basis (WCS less WTI)", snap.wcs_minus_wti, MONEY, "Computed"),
        ("Discount as share of WTI", snap.discount_pct_of_wti, PCT, "Computed"),
        (f"Change, {short}", snap.change_short, MONEY, "Computed"),
        (f"Change, {medium}", snap.change_medium, MONEY, "Computed"),
        (f"Change, {long}", snap.change_long, MONEY, "Computed"),
        (
            "Percentile",
            snap.percentile_recent,
            PCT,
            f"Rank of latest within {snap.recent_window}",
        ),
        ("Average", snap.mean_recent, MONEY, snap.recent_window),
        ("Narrowest", snap.min_recent, MONEY, snap.recent_window),
        ("Widest", snap.max_recent, MONEY, snap.recent_window),
    ]

    row = 4
    for label, value, fmt, note in rows:
        sheet.cell(row=row, column=1, value=label).font = LABEL_FONT
        cell = sheet.cell(row=row, column=2, value=value)
        cell.number_format = fmt
        cell.font = BODY_FONT
        cell.border = BOX
        cell.alignment = Alignment(horizontal="right")
        sheet.cell(row=row, column=4, value=note).font = NOTE_FONT
        row += 1

    row = _section(sheet, row + 1, "Regimes", [
        "A single average spanning a capacity change is a number with no meaning.",
    ])
    row = _table(
        sheet,
        row,
        ["Period", "Periods", "Average", "Narrowest", "Widest"],
        [] if regimes.empty else [
            [r.period, r.observations, r.mean, r.min, r.max]
            for r in regimes.itertuples()
        ],
        formats=[None, None, MONEY, MONEY, MONEY],
        empty_note="No regime breaks configured.",
    )

    row = _section(sheet, row + 1, "Driver correlations", [
        "Correlation of period-over-period changes, not levels, so a shared",
        "trend does not create a false signal.",
    ])
    row = _table(
        sheet,
        row,
        ["Driver", "Periods", "Correlation"],
        [] if drivers.empty else [
            [r.driver, r.observations, r.correlation] for r in drivers.itertuples()
        ],
        formats=[None, None, "0.00"],
        empty_note="No driver series available. Add one in config.yaml.",
    )

    row = _section(sheet, row + 1, "Data quality", [])
    for key, value in quality.items():
        sheet.cell(row=row, column=1, value=key.replace("_", " ")).font = LABEL_FONT
        sheet.cell(
            row=row,
            column=2,
            value=", ".join(value) if isinstance(value, list) else value,
        ).font = BODY_FONT
        row += 1


def _section(sheet, row: int, title: str, notes: list[str]) -> int:
    sheet.cell(row=row, column=1, value=title).font = TITLE_FONT
    row += 1
    for note in notes:
        sheet.cell(row=row, column=1, value=note).font = NOTE_FONT
        row += 1
    return row + 1


def _table(sheet, row, headers, records, formats, empty_note) -> int:
    for index, header in enumerate(headers, start=1):
        cell = sheet.cell(row=row, column=index, value=header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
    row += 1

    if not records:
        sheet.cell(row=row, column=1, value=empty_note).font = NOTE_FONT
        return row + 1

    for record in records:
        for index, value in enumerate(record, start=1):
            cell = sheet.cell(row=row, column=index, value=value)
            cell.font = BODY_FONT
            fmt = formats[index - 1] if index - 1 < len(formats) else None
            if fmt:
                cell.number_format = fmt
        row += 1
    return row


def _series_sheet(sheet, aligned: pd.DataFrame, snap: Snapshot) -> None:
    columns = [
        ("obs_date", "Period", "yyyy-mm-dd"),
        ("wti_spot", "WTI", MONEY),
        ("wcs_filled", "WCS", MONEY),
        ("discount", "Discount", MONEY),
        ("wcs_minus_wti", "Basis", MONEY),
        ("discount_pct_of_wti", "Discount % of WTI", PCT),
        ("discount_ma_short", "Short avg", MONEY),
        ("discount_ma_long", "Long avg", MONEY),
        ("wti_days_in_month", "WTI days averaged", None),
        ("wcs_carried", "WCS carried", None),
    ]
    _write_table(sheet, aligned.reset_index(), columns)

    if len(aligned) > 2:
        chart = LineChart()
        chart.title = "WTI less WCS (USD per barrel)"
        chart.y_axis.title = "USD"
        chart.x_axis.title = "Period"
        chart.height = 9
        chart.width = 24
        data = Reference(sheet, min_col=4, min_row=1, max_row=len(aligned) + 1)
        cats = Reference(sheet, min_col=1, min_row=2, max_row=len(aligned) + 1)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        sheet.add_chart(chart, f"{get_column_letter(len(columns) + 2)}2")


def _change_sheet(sheet, periodic_frame: pd.DataFrame) -> None:
    columns = [
        ("obs_date", "Period", "yyyy-mm-dd"),
        ("wti_spot", "WTI", MONEY),
        ("wcs_filled", "WCS", MONEY),
        ("discount", "Discount", MONEY),
        ("discount_change", "Change vs prior", MONEY),
    ]
    _write_table(sheet, periodic_frame.reset_index(), columns)


def _write_table(sheet, frame: pd.DataFrame, columns: list[tuple]) -> None:
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "A2"

    present = [c for c in columns if c[0] in frame.columns]
    for index, (_, header, _) in enumerate(present, start=1):
        cell = sheet.cell(row=1, column=index, value=header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        sheet.column_dimensions[get_column_letter(index)].width = max(12, len(header) + 4)

    for row_index, record in enumerate(frame.itertuples(index=False), start=2):
        values = dict(zip(frame.columns, record))
        for col_index, (key, _, fmt) in enumerate(present, start=1):
            value = values.get(key)
            if isinstance(value, pd.Timestamp):
                value = value.to_pydatetime()
            elif not isinstance(value, (bool, str)) and pd.isna(value):
                value = None
            cell = sheet.cell(row=row_index, column=col_index, value=value)
            cell.font = BODY_FONT
            if fmt:
                cell.number_format = fmt


# Written recap -------------------------------------------------------------


def _write_recap(snap, drivers, regimes, quality, path: Path) -> None:
    short, medium, long = snap.change_labels
    grain = "monthly average" if snap.frequency == MONTHLY else "daily"

    driver_lines = (
        "\n".join(
            f"- {row.driver}: correlation {row.correlation:+.2f} over "
            f"{row.observations} periods"
            for row in drivers.itertuples()
        )
        or "- No driver series available yet."
    )
    regime_lines = (
        "\n".join(
            f"- {row.period}: averaged ${row.mean:.2f}, range ${row.min:.2f} to "
            f"${row.max:.2f} over {row.observations} periods"
            for row in regimes.itertuples()
        )
        or "- No regime breaks configured."
    )

    content = f"""# Canadian crude recap, {snap.as_of}

All figures are {grain} assessments. WCS Hardisty is published monthly by the
Government of Alberta; daily WCS is a commercial product.

## Where the differential sits

WTI averaged ${snap.wti:.2f} and WCS Hardisty ${snap.wcs:.2f}, putting the
discount at ${snap.discount:.2f} per barrel, or {snap.discount_pct_of_wti:.1f}
percent of WTI. Quoted the way a sheet shows it, WCS is {snap.wcs_minus_wti:+.2f}
to WTI.

The discount {_direction(snap.change_short)} over {short} and
{_direction(snap.change_long)} over {long}. It sits in the
{_ordinal(snap.percentile_recent)} percentile of the {snap.recent_window},
against an average of {_money(snap.mean_recent)} and a range of
{_money(snap.min_recent)} to {_money(snap.max_recent)}.

## Regimes

{regime_lines}

## Drivers

{driver_lines}

<!--
Commentary goes here. Two or three sentences on why the differential is where
it is. Everything above is generated: those are inputs, not analysis, and the
pipeline deliberately does not attempt this part.

Worth checking before writing:
  - Mainline apportionment for the current and coming month
  - Cushing and Alberta inventory builds or draws
  - PADD 2 refinery turnarounds
  - TMX nominations and utilization
  - Heavy sour supply outside Canada, which sets the competing barrel
-->

## What to watch

<!--
Two or three forward looking points. Specific and falsifiable: name the number
being watched and what would change the view.
-->

---

Generated from {quality.get('rows', 0)} aligned periods
({quality.get('first_date', 'n/a')} to {quality.get('last_date', 'n/a')}),
alignment mode {quality.get('alignment', 'n/a')}.
"""
    path.write_text(content, encoding="utf-8")
    LOG.info("Wrote %s", path)


def _direction(change: float | None) -> str:
    if change is None:
        return "has no comparable prior reading"
    if change > 0.10:
        return f"widened by ${change:.2f}"
    if change < -0.10:
        return f"narrowed by ${abs(change):.2f}"
    return "was broadly unchanged"


def _money(value: float | None) -> str:
    return f"${value:.2f}" if value is not None else "n/a"


def _ordinal(value: float | None) -> str:
    return f"{value:.0f}th" if value is not None else "n/a"
