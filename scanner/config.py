"""
scanner/config.py — Configuration loading and validation.

Data source selection (automatic based on which keys are set):
    Google mode  — set GOOGLE_MAPS_API_KEY (best coverage, ~$0.95/POC scan)
    Free mode    — omit Google key; uses Overpass/OSM + Yelp (if YELP_API_KEY set)

Env vars:
    GOOGLE_MAPS_API_KEY     Optional. Enables Google Places mode.
    YELP_API_KEY            Optional. Adds Yelp results in free mode (500 calls/day free).
    DB_PATH                 Default: data/scanner.db
    REPORTS_DIR             Default: reports
    LOG_LEVEL               Default: INFO
    CHECK_CONCURRENCY       Default: 10
    PLACE_DETAILS_DELAY_S   Default: 0.15  (Google mode only)
    WEBSITE_CHECK_TIMEOUT_S Default: 10
    STALENESS_DAYS          Default: 7

Milestone: M0-A
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    """Immutable runtime configuration derived from environment variables."""

    google_maps_api_key: str          # empty string = not set → use free mode
    yelp_api_key: str                 # empty string = not set → skip Yelp in free mode
    db_path: Path
    reports_dir: Path
    log_level: str
    check_concurrency: int
    place_details_delay_s: float
    website_check_timeout_s: float
    staleness_days: int

    @property
    def use_google(self) -> bool:
        return bool(self.google_maps_api_key)

    @property
    def data_source_label(self) -> str:
        if self.use_google:
            return "Google Places API"
        parts = ["Overpass/OSM"]
        if self.yelp_api_key:
            parts.append("Yelp")
        return " + ".join(parts) + " (free mode)"


def load_config(env_file: Path | None = None) -> Config:
    """Load configuration from environment / .env file.

    Never raises on missing API keys — callers decide what to do
    based on cfg.use_google and cfg.yelp_api_key.
    """
    from dotenv import load_dotenv

    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()

    project_root = Path.cwd()

    return Config(
        google_maps_api_key=os.getenv("GOOGLE_MAPS_API_KEY", "").strip(),
        yelp_api_key=os.getenv("YELP_API_KEY", "").strip(),
        db_path=_resolve_path(os.getenv("DB_PATH", "data/scanner.db"), project_root),
        reports_dir=_resolve_path(os.getenv("REPORTS_DIR", "reports"), project_root),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        check_concurrency=int(os.getenv("CHECK_CONCURRENCY", "10")),
        place_details_delay_s=float(os.getenv("PLACE_DETAILS_DELAY_S", "0.15")),
        website_check_timeout_s=float(os.getenv("WEBSITE_CHECK_TIMEOUT_S", "10")),
        staleness_days=int(os.getenv("STALENESS_DAYS", "7")),
    )


def _resolve_path(raw: str, project_root: Path) -> Path:
    p = Path(raw)
    if p.is_absolute():
        return p
    return (project_root / p).resolve()
