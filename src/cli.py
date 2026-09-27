"""Command line interface.

    python -m src.cli init
    python -m src.cli ingest [--only wti_spot] [--start 2019-01-01]
    python -m src.cli report
    python -m src.cli run                  ingest then report
    python -m src.cli status
    python -m src.cli discover --route petroleum/pnp/wiup --contains "PADD 2"
    python -m src.cli demo                 synthetic data, no network needed
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path

import pandas as pd

from . import db, report, transform
from .config import Config, ConfigError, load_dotenv
from .sources import alberta, apportionment, eia

LOG = logging.getLogger("ccd")

DRIVERS = ["cushing_stocks", "padd2_utilization", "mainline_apportionment", "brent_spot"]


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # urllib3 logs full request URLs at DEBUG, which would print the API key.
    # Never let -v leak a credential.
    logging.getLogger("urllib3").setLevel(logging.INFO)
    load_dotenv()

    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        LOG.error("%s", exc)
        return 2

    handlers = {
        "init": cmd_init,
        "ingest": cmd_ingest,
        "report": cmd_report,
        "run": cmd_run,
        "status": cmd_status,
        "discover": cmd_discover,
        "demo": cmd_demo,
    }
    try:
        return handlers[args.command](config, args)
    except ConfigError as exc:
        LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.warning("Interrupted")
        return 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccd", description="Canadian crude differential pipeline"
    )
    parser.add_argument("--config", help="Path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str):
        # -v works before or after the subcommand; argparse needs it on both.
        child = sub.add_parser(name, help=help_text)
        child.add_argument("-v", "--verbose", action="store_true")
        return child

    add("init", "Create the database and folders")

    ingest = add("ingest", "Fetch every configured source")
    ingest.add_argument("--only", action="append", help="Limit to these metric names")
    ingest.add_argument("--start", help="Override the start date")

    add("report", "Build the Excel, chart and recap from stored data")
    run = add("run", "Ingest then report")
    run.add_argument("--only", action="append")
    run.add_argument("--start")

    add("status", "Show what is in the database")

    discover = add("discover", "List EIA series ids for a route")
    discover.add_argument("--route", required=True)
    discover.add_argument("--contains", help="Filter on id or description")

    demo = add("demo", "Load clearly labelled synthetic data to test the pipeline")
    demo.add_argument("--years", type=int, default=5)

    return parser


# Commands ------------------------------------------------------------------


def cmd_init(config: Config, _args) -> int:
    db.init(config.database_path)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    (config.root / "data" / "manual").mkdir(parents=True, exist_ok=True)
    LOG.info("Initialised database at %s", config.database_path)
    return 0


def cmd_ingest(config: Config, args) -> int:
    db.init(config.database_path)
    run_id = uuid.uuid4().hex[:12]
    start = getattr(args, "start", None) or config.start_date
    wanted = set(getattr(args, "only", None) or [])
    failures = 0

    with db.connect(config.database_path) as conn:
        for spec in config.eia_series:
            if wanted and spec.metric not in wanted:
                continue
            failures += _ingest_one(
                conn,
                run_id,
                spec.metric,
                lambda spec=spec: (eia.fetch_series(config, spec, start), f"eia:{spec.id}"),
            )

        if not wanted or config.wcs.get("metric", "wcs_hardisty") in wanted:
            failures += _ingest_one(
                conn,
                run_id,
                config.wcs.get("metric", "wcs_hardisty"),
                lambda: alberta.fetch(config.wcs, config.root),
            )

        block = config.apportionment
        if block and (not wanted or block.get("metric") in wanted):
            failures += _ingest_one(
                conn,
                run_id,
                block.get("metric", "mainline_apportionment"),
                lambda: apportionment.fetch(block, config.root),
            )

    if failures:
        LOG.warning(
            "%s source(s) failed. The pipeline continues on what it has. "
            "Run `status` to see coverage.",
            failures,
        )
    return 0


def _ingest_one(conn, run_id: str, metric: str, loader) -> int:
    """Fetch one source, store it, log the outcome. Returns 1 on failure."""
    try:
        result = loader()
        rows, source = result if isinstance(result, tuple) else (result, metric)
    except Exception as exc:  # noqa: BLE001 - one bad feed must not kill the run
        LOG.error("%s failed: %s", metric, exc)
        db.log_ingest(conn, run_id, metric, "unknown", 0, 0, None, None, "failed", str(exc))
        return 1

    written = db.upsert_observations(conn, rows)
    dates = sorted(r["obs_date"] for r in rows) if rows else []
    db.log_ingest(
        conn,
        run_id,
        metric,
        str(source),
        len(rows),
        written,
        dates[0] if dates else None,
        dates[-1] if dates else None,
        "ok",
    )
    LOG.info(
        "%-24s %5d rows seen, %5d written, through %s (%s)",
        metric,
        len(rows),
        written,
        dates[-1] if dates else "n/a",
        source,
    )
    return 0


def cmd_report(config: Config, _args) -> int:
    start = (
        pd.Timestamp.today() - pd.Timedelta(days=config.lookback_days)
    ).strftime("%Y-%m-%d")
    long_frame = db.read_metrics(config.database_path, start=start)
    if long_frame.empty:
        LOG.error("No data in the database. Run `ingest` or `demo` first.")
        return 1

    wide = transform.long_to_wide(long_frame)
    frequency = config.wcs_frequency
    try:
        aligned = transform.build_differential(
            wide, frequency, config.max_wcs_staleness_days
        )
    except (KeyError, ValueError) as exc:
        LOG.error("%s", exc)
        return 1
    if aligned.empty:
        LOG.error("No overlapping WTI and WCS observations. Check `status`.")
        return 1

    periodic_frame = transform.periodic(aligned, frequency)
    snap = transform.snapshot(aligned, frequency)
    drivers = transform.correlate_drivers(aligned, wide, DRIVERS, frequency)
    regimes = transform.regime_comparison(aligned, config.regime_breaks)
    quality = transform.data_quality(aligned, wide, frequency)

    paths = report.build_all(
        aligned,
        periodic_frame,
        snap,
        drivers,
        regimes,
        quality,
        config.output_dir,
        wti_daily=wide.get(transform.WTI),
    )

    short, medium, long_label = snap.change_labels
    print()
    print(f"  As of {snap.as_of}  ({frequency} alignment, {snap.observations} periods)")
    print(f"  WTI  {snap.wti:>8.2f}")
    print(f"  WCS  {snap.wcs:>8.2f}")
    print(f"  Diff {snap.discount:>8.2f}   ({snap.wcs_minus_wti:+.2f} basis)")
    if snap.change_short is not None:
        print(f"  {short:<12} {snap.change_short:>+8.2f}")
    if snap.change_long is not None:
        print(f"  {long_label:<12} {snap.change_long:>+8.2f}")
    if not regimes.empty:
        print()
        for row in regimes.itertuples():
            print(f"  {row.period:<22} avg {row.mean:>7.2f}  ({row.observations} periods)")
    print()
    for label, path in paths.items():
        print(f"  {label:<10} {path}")
    print()
    print("  Fill in the commentary sections of the recap before showing anyone.")
    print()
    return 0


def cmd_run(config: Config, args) -> int:
    code = cmd_ingest(config, args)
    return code or cmd_report(config, args)


def cmd_status(config: Config, _args) -> int:
    if not Path(config.database_path).exists():
        LOG.error("No database yet. Run `init` then `ingest`.")
        return 1
    frame = db.coverage(config.database_path)
    if frame.empty:
        print("Database is empty.")
        return 0
    print()
    print(frame.to_string(index=False))
    print()
    return 0


def cmd_discover(config: Config, args) -> int:
    found = list(eia.discover_series(config, args.route, args.contains))
    if not found:
        print(f"No series matched on route {args.route}")
        return 1
    print()
    for series_id, name in found:
        print(f"  {series_id:<22} {name}")
    print(f"\n  {len(found)} series. Copy the id into config.yaml.\n")
    return 0


def cmd_demo(config: Config, args) -> int:
    """Load synthetic data so the pipeline can be tested without an API key.

    The numbers are generated, not real. They are stored under source
    'SYNTHETIC' so `status` shows what they are, and the report will say so.
    Never present demo output as analysis.
    """
    import numpy as np

    db.init(config.database_path)
    rng = np.random.default_rng(20261004)
    days = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=252 * args.years)

    wti = 70 + np.cumsum(rng.normal(0, 0.9, len(days)))
    wti = np.clip(wti, 35, 120)
    discount = 14 + 6 * np.sin(np.linspace(0, 9 * np.pi, len(days))) + np.cumsum(
        rng.normal(0, 0.18, len(days))
    )
    discount = np.clip(discount, 8, 32)

    rows = []
    monthly_seen: set[str] = set()
    for index, day in enumerate(days):
        stamp = day.strftime("%Y-%m-%d")
        rows.append(
            {
                "metric": "wti_spot",
                "obs_date": stamp,
                "value": round(float(wti[index]), 2),
                "units": "USD/bbl",
                "source": "SYNTHETIC",
            }
        )
        # WCS is published monthly, so the demo mirrors that shape rather than
        # inventing a daily print the real feed does not have.
        month_start = day.replace(day=1).strftime("%Y-%m-%d")
        if month_start not in monthly_seen:
            monthly_seen.add(month_start)
            window = slice(max(0, index - 10), index + 11)
            rows.append(
                {
                    "metric": "wcs_hardisty",
                    "obs_date": month_start,
                    "value": round(
                        float(np.mean(wti[window]) - np.mean(discount[window])), 2
                    ),
                    "units": "USD/bbl",
                    "source": "SYNTHETIC",
                }
            )
        if day.weekday() == 2:
            rows.append(
                {
                    "metric": "cushing_stocks",
                    "obs_date": stamp,
                    "value": round(
                        30000 + 4000 * float(np.sin(index / 40)) + rng.normal(0, 400), 0
                    ),
                    "units": "thousand barrels",
                    "source": "SYNTHETIC",
                }
            )

    with db.connect(config.database_path) as conn:
        written = db.upsert_observations(conn, rows)
        db.log_ingest(
            conn,
            uuid.uuid4().hex[:12],
            "demo",
            "SYNTHETIC",
            len(rows),
            written,
            days[0].strftime("%Y-%m-%d"),
            days[-1].strftime("%Y-%m-%d"),
            "ok",
            "Synthetic data for pipeline testing. Not real prices.",
        )

    LOG.warning("Loaded %s SYNTHETIC rows. These are not real prices.", written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
