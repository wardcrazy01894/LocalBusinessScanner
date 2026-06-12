"""
scanner/config.py — Configuration loading and validation.

Env vars (all except GOOGLE_MAPS_API_KEY are optional):
    GOOGLE_MAPS_API_KEY     Required. Google Maps Platform API key.
    DB_PATH                 Default: data/scanner.db
    REPORTS_DIR             Default: reports
    LOG_LEVEL               Default: INFO
    CHECK_CONCURRENCY       Default: 10
    PLACE_DETAILS_DELAY_S   Default: 0.15
    WEBSITE_CHECK_TIMEOUT_S Default: 10
    STALENESS_DAYS          Default: 7

Milestone: M0-A
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    """Immutable runtime configuration derived from environment variables."""

    google_maps_api_key: str
    db_path: Path
    reports_dir: Path
    log_level: str
    check_concurrency: int
    place_details_delay_s: float
    website_check_timeout_s: float
    staleness_days: int


def load_config(env_file: Path | None = None) -> Config:
    """Load and validate configuration from environment / .env file.

    Raises SystemExit with a human-readable message if GOOGLE_MAPS_API_KEY
    is not set.
    """
    from dotenv import load_dotenv

    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()

    key = _require_env("GOOGLE_MAPS_API_KEY")
    project_root = Path.cwd()

    return Config(
        google_maps_api_key=key,
        db_path=_resolve_path(os.getenv("DB_PATH", "data/scanner.db"), project_root),
        reports_dir=_resolve_path(os.getenv("REPORTS_DIR", "reports"), project_root),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        check_concurrency=int(os.getenv("CHECK_CONCURRENCY", "10")),
        place_details_delay_s=float(os.getenv("PLACE_DETAILS_DELAY_S", "0.15")),
        website_check_timeout_s=float(os.getenv("WEBSITE_CHECK_TIMEOUT_S", "10")),
        staleness_days=int(os.getenv("STALENESS_DAYS", "7")),
    )


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(
            f"\nError: {name} is not set.\n"
            "Copy .env.example to .env and add your Google Maps API key.\n"
            "Get a key at: https://console.cloud.google.com/apis/credentials\n"
        )
    return value


def _resolve_path(raw: str, project_root: Path) -> Path:
    p = Path(raw)
    if p.is_absolute():
        return p
    return (project_root / p).resolve()
