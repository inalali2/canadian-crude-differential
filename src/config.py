"""Configuration loading.

Keeps every tunable value in config.yaml so the code has no embedded URLs,
series ids or date ranges.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed."""


@dataclass(frozen=True)
class SeriesSpec:
    """One EIA series mapped to an internal metric name."""

    id: str
    route: str
    frequency: str
    metric: str
    units: str
    label: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SeriesSpec":
        missing = {"id", "route", "frequency", "metric"} - set(raw)
        if missing:
            raise ConfigError(f"EIA series entry is missing keys: {sorted(missing)}")
        return cls(
            id=raw["id"],
            route=raw["route"].strip("/"),
            frequency=raw["frequency"],
            metric=raw["metric"],
            units=raw.get("units", ""),
            label=raw.get("label", raw["id"]),
        )


@dataclass(frozen=True)
class Config:
    raw: dict[str, Any]
    path: Path
    root: Path = field(default=PROJECT_ROOT)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not cfg_path.exists():
            raise ConfigError(f"Config file not found: {cfg_path}")
        with cfg_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        return cls(raw=raw, path=cfg_path)

    # Paths -----------------------------------------------------------------

    def resolve(self, relative: str | Path) -> Path:
        candidate = Path(relative)
        return candidate if candidate.is_absolute() else self.root / candidate

    @property
    def database_path(self) -> Path:
        return self.resolve(self.raw.get("database", "data/crude.db"))

    @property
    def output_dir(self) -> Path:
        return self.resolve(self.raw.get("report", {}).get("output_dir", "output"))

    # Values ----------------------------------------------------------------

    @property
    def start_date(self) -> str:
        return str(self.raw.get("start_date", "2019-01-01"))

    @property
    def eia_base_url(self) -> str:
        return self.raw.get("eia", {}).get("base_url", "https://api.eia.gov/v2")

    @property
    def eia_series(self) -> list[SeriesSpec]:
        entries = self.raw.get("eia", {}).get("series", []) or []
        return [SeriesSpec.from_dict(entry) for entry in entries]

    @property
    def wcs(self) -> dict[str, Any]:
        return self.raw.get("wcs", {})

    @property
    def apportionment(self) -> dict[str, Any]:
        return self.raw.get("apportionment", {})

    @property
    def max_wcs_staleness_days(self) -> int:
        return int(self.raw.get("transform", {}).get("max_wcs_staleness_days", 3))

    @property
    def wcs_frequency(self) -> str:
        return str(self.raw.get("transform", {}).get("wcs_frequency", "monthly"))

    @property
    def regime_breaks(self) -> dict[str, str]:
        return dict(self.raw.get("report", {}).get("regime_breaks", {}) or {})

    @property
    def lookback_days(self) -> int:
        return int(self.raw.get("report", {}).get("lookback_days", 7700))


def eia_api_key() -> str:
    """Return the EIA API key from the environment.

    Deliberately not read from config.yaml so a key can never be committed.
    """
    key = os.environ.get("EIA_API_KEY", "").strip()
    if not key:
        raise ConfigError(
            "EIA_API_KEY is not set. Register for a free key at "
            "https://www.eia.gov/opendata/register.php then either export it or "
            "put it in a .env file (see .env.example)."
        )
    return key


def load_dotenv(path: str | Path | None = None) -> None:
    """Minimal .env loader so there is no extra dependency.

    Existing environment variables always win.
    """
    env_path = Path(path) if path else PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip("'\""))
