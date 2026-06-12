"""
scanner/db.py — SQLite persistence layer.

All functions are synchronous; call only from the main thread.
Never pass a sqlite3.Connection into async coroutines.

Milestone: M0-B
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Business:
    """One row from the ``businesses`` table."""

    place_id: str
    name: str
    address: str
    phone: Optional[str]
    website: Optional[str]
    lat: float
    lng: float
    types: list[str]
    scan_area: str
    first_seen_at: datetime
    last_checked_at: Optional[datetime]
    details_fetched_at: Optional[datetime]


@dataclass
class WebsiteCheckResult:
    """One row from the ``website_checks`` table."""

    place_id: str
    checked_at: datetime
    reachable: bool
    has_ssl: bool
    has_viewport: bool
    load_time_ms: Optional[int]
    has_title: bool
    has_meta_desc: bool
    score: int
    http_status: Optional[int]
    error_msg: Optional[str]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    place_id             TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    address              TEXT,
    phone                TEXT,
    website              TEXT,
    lat                  REAL,
    lng                  REAL,
    types                TEXT DEFAULT '[]',
    scan_area            TEXT,
    first_seen_at        TEXT NOT NULL,
    last_checked_at      TEXT,
    details_fetched_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_biz_scan_area        ON businesses(scan_area);
CREATE INDEX IF NOT EXISTS idx_biz_details_fetched  ON businesses(details_fetched_at);
CREATE INDEX IF NOT EXISTS idx_biz_website          ON businesses(website);

CREATE TABLE IF NOT EXISTS website_checks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    place_id     TEXT    NOT NULL REFERENCES businesses(place_id),
    checked_at   TEXT    NOT NULL,
    reachable    INTEGER NOT NULL,
    has_ssl      INTEGER NOT NULL,
    has_viewport INTEGER NOT NULL,
    load_time_ms INTEGER,
    has_title    INTEGER NOT NULL,
    has_meta_desc INTEGER NOT NULL,
    score        INTEGER NOT NULL,
    http_status  INTEGER,
    error_msg    TEXT
);

CREATE INDEX IF NOT EXISTS idx_wc_place_id   ON website_checks(place_id);
CREATE INDEX IF NOT EXISTS idx_wc_checked_at ON website_checks(checked_at);
"""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Open (or create) the SQLite database at *db_path*."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if they do not already exist (idempotent)."""
    conn.executescript(_SCHEMA)


# ---------------------------------------------------------------------------
# Business CRUD
# ---------------------------------------------------------------------------


def upsert_business_stub(conn: sqlite3.Connection, business: Business) -> None:
    """Insert *business* if its place_id is not already in the DB (INSERT OR IGNORE)."""
    conn.execute(
        """
        INSERT OR IGNORE INTO businesses
            (place_id, name, address, phone, website, lat, lng, types,
             scan_area, first_seen_at, last_checked_at, details_fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            business.place_id,
            business.name,
            business.address,
            business.phone,
            business.website,
            business.lat,
            business.lng,
            json.dumps(business.types),
            business.scan_area,
            business.first_seen_at.isoformat(),
            business.last_checked_at.isoformat() if business.last_checked_at else None,
            business.details_fetched_at.isoformat() if business.details_fetched_at else None,
        ),
    )


def update_business_details(
    conn: sqlite3.Connection,
    place_id: str,
    phone: Optional[str],
    website: Optional[str],
    details_fetched_at: datetime,
    force: bool = False,
) -> None:
    """Write Place Details fields for an existing business row.

    In v0 ``force`` is accepted but ignored (always behaves as False).
    In v1 ``force=True`` will bypass the ``details_fetched_at IS NULL`` guard.
    """
    if force:
        conn.execute(
            """UPDATE businesses SET phone=?, website=?, details_fetched_at=?
               WHERE place_id=?""",
            (phone, website, details_fetched_at.isoformat(), place_id),
        )
    else:
        conn.execute(
            """UPDATE businesses SET phone=?, website=?, details_fetched_at=?
               WHERE place_id=? AND details_fetched_at IS NULL""",
            (phone, website, details_fetched_at.isoformat(), place_id),
        )


def get_businesses_needing_details(conn: sqlite3.Connection) -> list[Business]:
    """Return businesses that have not yet had Place Details fetched."""
    rows = conn.execute(
        "SELECT * FROM businesses WHERE details_fetched_at IS NULL"
    ).fetchall()
    return [_row_to_business(r) for r in rows]


def get_businesses_needing_check(
    conn: sqlite3.Connection, staleness_days: int
) -> list[Business]:
    """Return businesses whose website check is absent or stale.

    Staleness is derived from MAX(website_checks.checked_at) via JOIN,
    not from businesses.last_checked_at, so the two tables stay consistent
    even if update_last_checked_at is missed in a partial failure.
    """
    rows = conn.execute(
        """
        SELECT b.* FROM businesses b
        WHERE b.website IS NOT NULL
          AND (
            NOT EXISTS (
                SELECT 1 FROM website_checks wc WHERE wc.place_id = b.place_id
            )
            OR (
                SELECT MAX(wc2.checked_at) FROM website_checks wc2
                WHERE wc2.place_id = b.place_id
            ) < datetime('now', ? || ' days')
          )
        """,
        (f"-{staleness_days}",),
    ).fetchall()
    return [_row_to_business(r) for r in rows]


def get_all_businesses(conn: sqlite3.Connection) -> list[Business]:
    """Return every business row, ordered by name."""
    rows = conn.execute("SELECT * FROM businesses ORDER BY name ASC").fetchall()
    return [_row_to_business(r) for r in rows]


def update_last_checked_at(
    conn: sqlite3.Connection, place_id: str, checked_at: datetime
) -> None:
    """Stamp ``last_checked_at`` on a business row after a website check."""
    conn.execute(
        "UPDATE businesses SET last_checked_at=? WHERE place_id=?",
        (checked_at.isoformat(), place_id),
    )


# ---------------------------------------------------------------------------
# Website check persistence
# ---------------------------------------------------------------------------


def insert_website_check(conn: sqlite3.Connection, result: WebsiteCheckResult) -> None:
    """Append a new website check row (history is preserved)."""
    conn.execute(
        """
        INSERT INTO website_checks
            (place_id, checked_at, reachable, has_ssl, has_viewport, load_time_ms,
             has_title, has_meta_desc, score, http_status, error_msg)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result.place_id,
            result.checked_at.isoformat(),
            int(result.reachable),
            int(result.has_ssl),
            int(result.has_viewport),
            result.load_time_ms,
            int(result.has_title),
            int(result.has_meta_desc),
            result.score,
            result.http_status,
            result.error_msg,
        ),
    )


def get_latest_check(
    conn: sqlite3.Connection, place_id: str
) -> Optional[WebsiteCheckResult]:
    """Return the most recent website check for *place_id*, or None."""
    row = conn.execute(
        "SELECT * FROM website_checks WHERE place_id=? ORDER BY checked_at DESC LIMIT 1",
        (place_id,),
    ).fetchone()
    if not row:
        return None
    return WebsiteCheckResult(
        place_id=row["place_id"],
        checked_at=datetime.fromisoformat(row["checked_at"]),
        reachable=bool(row["reachable"]),
        has_ssl=bool(row["has_ssl"]),
        has_viewport=bool(row["has_viewport"]),
        load_time_ms=row["load_time_ms"],
        has_title=bool(row["has_title"]),
        has_meta_desc=bool(row["has_meta_desc"]),
        score=row["score"],
        http_status=row["http_status"],
        error_msg=row["error_msg"],
    )


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def get_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Return aggregate counts used by reporter.print_stats()."""
    total = conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
    no_website = conn.execute(
        "SELECT COUNT(*) FROM businesses WHERE website IS NULL AND details_fetched_at IS NOT NULL"
    ).fetchone()[0]
    website_present = conn.execute(
        "SELECT COUNT(*) FROM businesses WHERE website IS NOT NULL"
    ).fetchone()[0]
    details_fetched = conn.execute(
        "SELECT COUNT(*) FROM businesses WHERE details_fetched_at IS NOT NULL"
    ).fetchone()[0]
    checked = conn.execute(
        "SELECT COUNT(DISTINCT place_id) FROM website_checks"
    ).fetchone()[0]
    poor = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT place_id, MAX(checked_at) AS latest FROM website_checks GROUP BY place_id
        ) t
        JOIN website_checks wc ON wc.place_id = t.place_id AND wc.checked_at = t.latest
        WHERE wc.score < 40
        """
    ).fetchone()[0]
    good = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT place_id, MAX(checked_at) AS latest FROM website_checks GROUP BY place_id
        ) t
        JOIN website_checks wc ON wc.place_id = t.place_id AND wc.checked_at = t.latest
        WHERE wc.score >= 40
        """
    ).fetchone()[0]

    return {
        "total_businesses": total,
        "no_website": no_website,
        "website_present": website_present,
        "details_fetched": details_fetched,
        "checked": checked,
        "poor_website": poor,
        "good_website": good,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_business(row: sqlite3.Row) -> Business:
    return Business(
        place_id=row["place_id"],
        name=row["name"],
        address=row["address"] or "",
        phone=row["phone"],
        website=row["website"],
        lat=row["lat"] or 0.0,
        lng=row["lng"] or 0.0,
        types=json.loads(row["types"]) if row["types"] else [],
        scan_area=row["scan_area"] or "",
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        last_checked_at=datetime.fromisoformat(row["last_checked_at"]) if row["last_checked_at"] else None,
        details_fetched_at=datetime.fromisoformat(row["details_fetched_at"]) if row["details_fetched_at"] else None,
    )
