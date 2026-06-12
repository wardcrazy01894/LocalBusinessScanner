"""
scanner/free_sources.py — Free data source fallback (no Google API key required).

Used automatically when GOOGLE_MAPS_API_KEY is not set.

Sources combined:
  1. Nominatim (OpenStreetMap) — geocoding. Free, 1 req/s rate limit.
  2. Overpass API (OpenStreetMap) — business discovery. Completely free, no key.
     Coverage: good for established shops/restaurants/amenities; weaker for
     service businesses (plumbers, electricians) that aren't well-mapped.
  3. Yelp Fusion API — supplemental coverage. Free key, 500 calls/day.
     Strong for food/retail/beauty; has phone + address but NOT website URL
     in the search response (website field stays NULL for Yelp results).

place_id prefixes:
  osm_node_{id}   — Overpass node
  osm_way_{id}    — Overpass way (building outline, centroid used)
  yelp_{alias}    — Yelp business alias

These never collide with Google's ChIJ... place_ids.

Deduplication: after combining Overpass + Yelp results, businesses with the
same normalized name + location (within ~11 m) are deduplicated before DB write.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from rich.console import Console

from .config import Config
from .db import (
    Business,
    get_businesses_needing_details,
    upsert_business_stub,
)

_console = Console()

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_YELP_SEARCH_URL = "https://api.yelp.com/v3/businesses/search"

_HEADERS = {"User-Agent": "LocalBusinessScanner/0.1 (lead-gen research tool)"}


# ---------------------------------------------------------------------------
# Geocoding via Nominatim
# ---------------------------------------------------------------------------


def geocode_nominatim(area_string: str) -> tuple[float, float]:
    """Resolve a human-readable area string to (lat, lng) using Nominatim.

    Nominatim ToS: max 1 req/s, valid User-Agent required. This function
    sleeps 1 s after the call to respect that limit.

    Raises:
        SystemExit: If geocoding returns no results.
    """
    with httpx.Client(headers=_HEADERS, timeout=10.0) as client:
        resp = client.get(
            _NOMINATIM_URL,
            params={"q": area_string, "format": "json", "limit": 1, "countrycodes": "us"},
        )
        resp.raise_for_status()
        results = resp.json()

    time.sleep(1.0)  # Nominatim rate limit

    if not results:
        raise SystemExit(
            f"\nError: Nominatim could not geocode '{area_string}'.\n"
            "Try a more specific string like 'Highland, Utah, USA'.\n"
        )

    r = results[0]
    resolved = r.get("display_name", "")
    if area_string.lower().split(",")[0].strip() not in resolved.lower():
        _console.print(
            f"[yellow]Warning:[/yellow] '{area_string}' resolved to '{resolved}'. "
            "Ctrl-C and try a more specific name if this is wrong."
        )

    return float(r["lat"]), float(r["lon"])


# ---------------------------------------------------------------------------
# Overpass API (OpenStreetMap)
# ---------------------------------------------------------------------------


def search_overpass(
    lat: float,
    lng: float,
    radius_m: int,
    max_results: int,
    area_string: str,
) -> list[Business]:
    """Fetch businesses from OpenStreetMap via Overpass API.

    Queries nodes and ways tagged with shop, amenity, craft, or office
    within the given radius. Returns Business stubs (website populated
    if the OSM record has a website tag).
    """
    query = f"""
[out:json][timeout:60];
(
  node["name"]["shop"](around:{radius_m},{lat},{lng});
  node["name"]["amenity"](around:{radius_m},{lat},{lng});
  node["name"]["craft"](around:{radius_m},{lat},{lng});
  node["name"]["office"](around:{radius_m},{lat},{lng});
  way["name"]["shop"](around:{radius_m},{lat},{lng});
  way["name"]["amenity"](around:{radius_m},{lat},{lng});
  way["name"]["craft"](around:{radius_m},{lat},{lng});
  way["name"]["office"](around:{radius_m},{lat},{lng});
);
out body center qt {max_results};
""".strip()

    try:
        with httpx.Client(headers=_HEADERS, timeout=90.0) as client:
            resp = client.post(_OVERPASS_URL, data={"data": query})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        _console.print(f"[yellow]Overpass API error:[/yellow] {exc}")
        return []

    businesses = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for element in data.get("elements", []):
        tags = element.get("tags", {})
        name = tags.get("name", "").strip()
        if not name:
            continue

        # Coordinates: nodes have lat/lon directly; ways have center
        if element["type"] == "node":
            elat, elng = element.get("lat", 0.0), element.get("lon", 0.0)
        else:
            center = element.get("center", {})
            elat, elng = center.get("lat", 0.0), center.get("lon", 0.0)

        if not elat and not elng:
            continue

        address = _build_address(tags, area_string)
        osm_types = [
            tags[k]
            for k in ("amenity", "shop", "craft", "office", "tourism", "leisure")
            if k in tags
        ]

        place_id = f"osm_{element['type']}_{element['id']}"
        website = tags.get("website") or tags.get("contact:website")
        phone = tags.get("phone") or tags.get("contact:phone")

        businesses.append(
            Business(
                place_id=place_id,
                name=name,
                address=address,
                phone=_normalise_phone(phone),
                website=_normalise_url(website),
                lat=elat,
                lng=elng,
                types=osm_types,
                scan_area=area_string,
                first_seen_at=now,
                last_checked_at=None,
                details_fetched_at=now,  # OSM data comes fully enriched
            )
        )

    return businesses


# ---------------------------------------------------------------------------
# Yelp Fusion API
# ---------------------------------------------------------------------------


def search_yelp(
    yelp_api_key: str,
    lat: float,
    lng: float,
    radius_m: int,
    max_results: int,
    area_string: str,
) -> list[Business]:
    """Fetch businesses from Yelp Fusion search API.

    Note: Yelp's search endpoint does NOT return the business website URL.
    Businesses from this source will have website=None; they appear in the
    no-website leads report unless a matching OSM record already has the URL.
    Phone and address are included.

    Yelp radius max: 40 000 m. Free tier: 500 calls/day.
    """
    radius_m = min(radius_m, 40_000)
    limit = min(max_results, 50)

    try:
        with httpx.Client(
            headers={**_HEADERS, "Authorization": f"Bearer {yelp_api_key}"},
            timeout=15.0,
        ) as client:
            resp = client.get(
                _YELP_SEARCH_URL,
                params={
                    "latitude": lat,
                    "longitude": lng,
                    "radius": radius_m,
                    "limit": limit,
                    "sort_by": "distance",
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        _console.print(f"[yellow]Yelp API error:[/yellow] {exc}")
        return []

    businesses = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for biz in data.get("businesses", []):
        name = biz.get("name", "").strip()
        if not name:
            continue

        loc = biz.get("location", {})
        coords = biz.get("coordinates", {})
        addr_parts = [p for p in [
            loc.get("address1", ""),
            loc.get("city", ""),
            loc.get("state", ""),
        ] if p]
        address = ", ".join(addr_parts) or area_string

        categories = [c.get("alias", "") for c in biz.get("categories", [])]

        businesses.append(
            Business(
                place_id=f"yelp_{biz['id']}",
                name=name,
                address=address,
                phone=_normalise_phone(biz.get("phone") or biz.get("display_phone")),
                website=None,  # Not available in Yelp search response
                lat=coords.get("latitude", 0.0),
                lng=coords.get("longitude", 0.0),
                types=categories,
                scan_area=area_string,
                first_seen_at=now,
                last_checked_at=None,
                details_fetched_at=now,  # Yelp data is fully enriched (phone/address)
            )
        )

    return businesses


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def _dedup(businesses: list[Business]) -> list[Business]:
    """Remove duplicates by (normalised name, lat ±0.0001°, lng ±0.0001°).

    Prefers records that already have a website URL. Where sources agree,
    keeps the first occurrence (Overpass preferred over Yelp if Overpass
    ran first and has a website).
    """
    seen: dict[tuple[str, int, int], Business] = {}
    for b in businesses:
        key = (
            b.name.lower().strip(),
            round(b.lat * 10_000),
            round(b.lng * 10_000),
        )
        if key not in seen:
            seen[key] = b
        elif b.website and not seen[key].website:
            # Upgrade existing entry if this one has a website
            seen[key] = b
    return list(seen.values())


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def fetch_area_free(
    cfg: Config,
    conn,
    area_string: str,
    radius_km: float,
    max_results: int,
    yes: bool = False,
) -> dict[str, int]:
    """Free-source scan pipeline: Nominatim → Overpass + Yelp → DB writes.

    Returns dict with "searched", "new_stubs", "skipped".
    (No "details_fetched" phase — free sources return all data at once.)
    """
    import typer

    # Geocode
    _console.print("  Geocoding via Nominatim (OpenStreetMap)...")
    lat, lng = geocode_nominatim(area_string)
    _console.print(f"  Resolved to ({lat:.5f}, {lng:.5f})")

    if not yes:
        confirmed = typer.confirm(f"Scan within {radius_km} km of this location?", default=True)
        if not confirmed:
            raise SystemExit("Scan cancelled.")

    radius_m = int(radius_km * 1000)
    all_businesses: list[Business] = []

    # Overpass
    _console.print("  Querying Overpass API (OpenStreetMap)...")
    osm_results = search_overpass(lat, lng, radius_m, max_results, area_string)
    _console.print(f"  Overpass: {len(osm_results)} businesses found")
    all_businesses.extend(osm_results)

    # Yelp (optional)
    if cfg.yelp_api_key:
        _console.print("  Querying Yelp Fusion API...")
        yelp_results = search_yelp(cfg.yelp_api_key, lat, lng, radius_m, max_results, area_string)
        _console.print(f"  Yelp: {len(yelp_results)} businesses found")
        all_businesses.extend(yelp_results)
    else:
        _console.print("  [dim]Yelp skipped (YELP_API_KEY not set)[/dim]")

    # Deduplicate across sources
    before = len(all_businesses)
    all_businesses = _dedup(all_businesses)
    deduped = before - len(all_businesses)
    if deduped:
        _console.print(f"  Removed {deduped} duplicates across sources")

    # Limit to max_results
    all_businesses = all_businesses[:max_results]

    # Write to DB
    new_stubs = 0
    for biz in all_businesses:
        existing = conn.execute(
            "SELECT place_id FROM businesses WHERE place_id=?", (biz.place_id,)
        ).fetchone()
        if not existing:
            upsert_business_stub(conn, biz)
            new_stubs += 1
    conn.commit()

    return {
        "searched": len(all_businesses),
        "new_stubs": new_stubs,
        "details_fetched": 0,
        "skipped": len(all_businesses) - new_stubs,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_address(tags: dict, fallback: str) -> str:
    parts = []
    num = tags.get("addr:housenumber", "")
    street = tags.get("addr:street", "")
    if num and street:
        parts.append(f"{num} {street}")
    elif street:
        parts.append(street)
    if tags.get("addr:city"):
        parts.append(tags["addr:city"])
    if tags.get("addr:state"):
        parts.append(tags["addr:state"])
    return ", ".join(parts) if parts else fallback


def _normalise_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url or None


def _normalise_phone(phone: Optional[str]) -> Optional[str]:
    if not phone:
        return None
    return phone.strip() or None
