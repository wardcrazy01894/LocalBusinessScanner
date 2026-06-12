# LocalBusinessScanner — Claude Code Context

## What this project does

Lead-gen CLI for a web design business. Finds local businesses with no website (or a poor one), checks website quality, and exports CSV reports of prospects to contact.

## Quick start for collaborators

```bash
pip install -r requirements.txt
cp .env.example .env          # see options below
python -m scanner scan "Highland, Utah"          # auto-detects data source
python -m scanner scan "St. Petersburg, Florida" --radius-km 2
python -m scanner report
python -m scanner stats
```

**Free mode (no billing):** leave `GOOGLE_MAPS_API_KEY` blank — uses Overpass/OSM automatically. Add `YELP_API_KEY` (free at yelp.com/developers) for better coverage.

**Google mode:** set `GOOGLE_MAPS_API_KEY` — best coverage, ~$0.95/POC scan, fits within Google's $200/month free tier.

## Architecture

```
CLI (cli.py)
  scan ──► cfg.use_google?
             YES → places.py  (geocode → Nearby Search → Place Details → DB)
             NO  → free_sources.py  (Nominatim → Overpass + Yelp → DB)
           ↓
           checker.py  (async httpx quality checks → DB)
  report ──► reporter.py  (query DB → CSV)
  stats  ──► reporter.py  (rich table)
```

**Key files:**
- `scanner/config.py` — `Config` dataclass; `cfg.use_google` property drives routing in `cli.py`
- `scanner/db.py` — SQLite CRUD; `Business` + `WebsiteCheckResult` dataclasses
- `scanner/places.py` — Google Places: geocode (googlemaps lib), Nearby Search, Place Details
- `scanner/free_sources.py` — Free fallback: Nominatim geocoding, Overpass/OSM, Yelp Fusion
- `scanner/checker.py` — Async `httpx` checks with `asyncio.Semaphore(10)`; never writes DB
- `scanner/reporter.py` — Read-only; timestamped CSV files + rich stats table
- `scanner/cli.py` — Typer app; routes to Google or free sources based on config

## Data source details

### Google Places (places.py)
- `geocode_area()` → `_paginate_nearby_search()` (3 pages × 20, 2.1 s sleep between pages) → `_fetch_place_details()` sequential with delay
- Max 60 results per scan (Google API limit); grid scan for more is v1
- `Business.details_fetched_at` is set after Place Details call

### Free sources (free_sources.py)
- Nominatim geocoding (1 req/s rate limit — `time.sleep(1)` enforced)
- Overpass query covers: shop, amenity, craft, office nodes+ways
- Yelp search result doesn't include business website URL (website=NULL for Yelp results)
- OSM `website` tag IS included when present
- Dedup by (name.lower(), lat ±0.0001°, lng ±0.0001°) before DB write
- `Business.details_fetched_at` is set immediately (no separate enrichment phase for free mode)

## Key constraints (do not change without good reason)

- **All DB writes on main thread only.** `checker.py` returns results; `cli.py` writes them. Passing `sqlite3.Connection` into async coroutines causes `ProgrammingError`.
- **`insert_website_check` + `update_last_checked_at` must be in one transaction** to prevent staleness divergence.
- **Google `_paginate_nearby_search` sleeps 2.1 s between pages** — Google API requirement; removing it gives `INVALID_REQUEST`.
- **Place Details calls are sequential** to stay under 10 QPS. Intentional for v0 simplicity.
- **Defaults are POC-safe**: 1 km radius, 50 businesses. Full St. Pete costs ~$55 with Google — never expand defaults without user confirmation.
- **Nominatim 1 req/s limit** — `time.sleep(1)` after geocode call in `free_sources.py` is required by ToS.

## Database

SQLite at `data/scanner.db` (gitignored, auto-created). Two tables:

```sql
-- Key queries
SELECT * FROM businesses WHERE website IS NULL AND details_fetched_at IS NOT NULL;
SELECT b.*, wc.score FROM businesses b JOIN website_checks wc ON wc.place_id = b.place_id
  WHERE wc.checked_at = (SELECT MAX(wc2.checked_at) FROM website_checks wc2 WHERE wc2.place_id = b.place_id)
  AND wc.score < 40;
```

place_id prefixes: Google = `ChIJ...`, Overpass = `osm_node_*` / `osm_way_*`, Yelp = `yelp_*`

## Website quality scoring

| Signal | Points |
|--------|--------|
| Reachable (HTTP 2xx) | 35 |
| Has SSL | 20 |
| Mobile viewport | 20 |
| Load < 2 s | 15 |
| Has `<title>` | 5 |
| Has `<meta description>` | 5 |

Score < 40 = poor website lead.

## Adding new features

**New CLI command:** add `@app.command()` in `cli.py`, import locally to avoid circular imports.

**New website metric:** add constant + logic in `checker.py:compute_score()`. Also add column to `website_checks` in `db.py` schema.

**Yelp website URLs (v1):** call `/v3/businesses/{id}` per Yelp result to get `website` field. Costs 1 extra Yelp API call per business but stays within 500/day for POC scale.

**Full city grid scan (v1):** implement `fetch_area_grid()` in `places.py` — tile the city bounding box with overlapping 1 km circles, collect all place_ids, let DB dedup handle overlap.

## v1 backlog

1. `--type` filter for Nearby Search (e.g. `restaurant` only)
2. Grid scan for full city coverage
3. `--refresh` to re-fetch stale Place Details / Overpass data
4. `--db` option for per-city database isolation
5. Yelp Business Details calls to get actual website URLs
