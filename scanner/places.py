"""
scanner/places.py — Google Places API integration.

Flow:
  1. geocode_area(client, area) → (lat, lng)
  2. _paginate_nearby_search(client, lat, lng, radius_m, max_results)
  3. _fetch_place_details(client, place_id, delay_s)
  4. fetch_area(cfg, conn, area, radius_km, max_results, yes) → counts

Rate-limiting constraints enforced here:
  - next_page_token: 2.1 s sleep between Nearby Search pages (Google requirement).
  - Place Details: sequential with cfg.place_details_delay_s between calls (~6.7 QPS).

Milestone: M1-A
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import googlemaps
from rich.console import Console

from .config import Config
from .db import (
    Business,
    get_businesses_needing_details,
    update_business_details,
    upsert_business_stub,
)

_console = Console()


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------


def geocode_area(client: googlemaps.Client, area_string: str) -> tuple[float, float]:
    """Resolve a human-readable area string to (lat, lng).

    Uses components={"country": "US"} to narrow results.
    Logs a warning if the resolved address doesn't contain area_string,
    indicating possible geocoding ambiguity (e.g. "Highland" → Illinois).

    Raises:
        SystemExit: If geocoding returns no results.
    """
    results = client.geocode(area_string, components={"country": "US"})
    if not results:
        raise SystemExit(
            f"\nError: Could not geocode '{area_string}'.\n"
            "Try a more specific string like 'Highland, Utah, USA'.\n"
        )

    result = results[0]
    resolved = result.get("formatted_address", "")

    if area_string.lower() not in resolved.lower():
        _console.print(
            f"[yellow]Warning:[/yellow] '{area_string}' resolved to '{resolved}'. "
            "If this is the wrong location, Ctrl-C and use a more specific name."
        )

    loc = result["geometry"]["location"]
    return loc["lat"], loc["lng"]


# ---------------------------------------------------------------------------
# Nearby Search (with pagination)
# ---------------------------------------------------------------------------


def _paginate_nearby_search(
    client: googlemaps.Client,
    lat: float,
    lng: float,
    radius_m: int,
    max_results: int,
) -> list[dict[str, Any]]:
    """Fetch up to max_results places from Nearby Search, paginating as needed.

    Google requires a ~2 s delay before using next_page_token.
    Returns at most min(available, max_results, 60) results.
    """
    collected: list[dict[str, Any]] = []

    response = client.places_nearby(location=(lat, lng), radius=radius_m)

    while True:
        page = response.get("results", [])
        collected.extend(page)

        if len(collected) >= max_results:
            break

        token = response.get("next_page_token")
        if not token:
            break

        time.sleep(2.1)  # Required by Google — INVALID_REQUEST if omitted
        response = client.places_nearby(page_token=token)

    return collected[:max_results]


# ---------------------------------------------------------------------------
# Place Details
# ---------------------------------------------------------------------------


def _fetch_place_details(
    client: googlemaps.Client,
    place_id: str,
    delay_s: float,
) -> dict[str, Any]:
    """Fetch Place Details for one place_id and sleep afterward.

    Fields requested: name, formatted_phone_number, website, geometry,
    formatted_address, types.  The ``fields`` parameter controls billing
    tier — contact fields cost $0.017/place on the Contact SKU.

    Returns the ``result`` dict, or {} on error.
    """
    try:
        response = client.place(
            place_id,
            fields=[
                "name",
                "formatted_phone_number",
                "website",
                "geometry",
                "formatted_address",
                "types",
            ],
        )
        return response.get("result", {})
    except Exception as exc:
        _console.print(f"[dim]  Warning: Place Details failed for {place_id}: {exc}[/dim]")
        return {}
    finally:
        time.sleep(delay_s)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def fetch_area(
    cfg: Config,
    conn: sqlite3.Connection,
    area_string: str,
    radius_km: float,
    max_results: int,
    yes: bool = False,
) -> dict[str, int]:
    """Full scan pipeline: geocode → Nearby Search → Place Details → DB writes.

    Steps:
    1. Geocode area_string to (lat, lng); print resolved address.
    2. Prompt for confirmation unless yes=True.
    3. Nearby Search (paginated) → list of raw place dicts.
    4. Upsert Business stubs (INSERT OR IGNORE).
    5. Intersect this scan's place_ids with get_businesses_needing_details()
       and fetch Place Details only for that intersection.
    6. Commit all writes.

    Returns dict with "searched", "new_stubs", "details_fetched", "skipped".
    """
    import typer

    client = googlemaps.Client(key=cfg.google_maps_api_key)

    # Step 1: Geocode
    lat, lng = geocode_area(client, area_string)
    _console.print(f"  Resolved to ({lat:.5f}, {lng:.5f})")

    # Step 2: Confirm
    if not yes:
        confirmed = typer.confirm(f"Scan within {radius_km} km of this location?", default=True)
        if not confirmed:
            raise SystemExit("Scan cancelled.")

    # Step 3: Nearby Search
    radius_m = int(radius_km * 1000)
    _console.print(f"  Searching radius {radius_m} m, max {max_results} results...")
    raw_results = _paginate_nearby_search(client, lat, lng, radius_m, max_results)
    _console.print(f"  Found {len(raw_results)} places")

    # Step 4: Upsert stubs
    new_stubs = 0
    scan_place_ids: set[str] = set()
    for raw in raw_results:
        pid = raw.get("place_id", "")
        if not pid:
            continue
        scan_place_ids.add(pid)
        stub = _raw_result_to_business_stub(raw, area_string)
        existing = conn.execute(
            "SELECT place_id FROM businesses WHERE place_id=?", (pid,)
        ).fetchone()
        if not existing:
            upsert_business_stub(conn, stub)
            new_stubs += 1
    conn.commit()

    # Step 5: Place Details — only for THIS scan's place_ids that need details
    needing = {b.place_id for b in get_businesses_needing_details(conn)}
    to_fetch = list(scan_place_ids & needing)

    details_fetched = 0
    if to_fetch:
        _console.print(f"  Fetching Place Details for {len(to_fetch)} businesses...")
        for i, pid in enumerate(to_fetch, 1):
            details = _fetch_place_details(client, pid, cfg.place_details_delay_s)
            if details:
                update_business_details(
                    conn,
                    pid,
                    details.get("formatted_phone_number"),
                    details.get("website"),
                    datetime.now(timezone.utc).replace(tzinfo=None),
                )
                details_fetched += 1
            if i % 10 == 0:
                _console.print(f"  ... {i}/{len(to_fetch)}")

    # Step 6: Commit
    conn.commit()

    return {
        "searched": len(raw_results),
        "new_stubs": new_stubs,
        "details_fetched": details_fetched,
        "skipped": len(raw_results) - new_stubs,
    }


def _raw_result_to_business_stub(
    result: dict[str, Any],
    scan_area: str,
) -> Business:
    """Convert a raw Nearby Search result to a Business stub (no details yet)."""
    loc = result.get("geometry", {}).get("location", {})
    return Business(
        place_id=result["place_id"],
        name=result.get("name", ""),
        address=result.get("vicinity") or result.get("formatted_address", ""),
        phone=None,
        website=None,
        lat=loc.get("lat", 0.0),
        lng=loc.get("lng", 0.0),
        types=result.get("types", []),
        scan_area=scan_area,
        first_seen_at=datetime.now(timezone.utc).replace(tzinfo=None),
        last_checked_at=None,
        details_fetched_at=None,
    )
