# LocalBusinessScanner — Claude Code Context

## What this project does

Lead-gen CLI for a web design business. Finds local businesses with no website (or a poor one) using Google Places API, checks website quality, and exports CSV reports of prospects to contact.

## Quick start for collaborators

```bash
pip install -r requirements.txt
cp .env.example .env          # add GOOGLE_MAPS_API_KEY
python -m scanner scan "Highland, Utah"          # POC scan (~$0.95)
python -m scanner scan "St. Petersburg, Florida" --radius-km 2
python -m scanner report
python -m scanner stats
```

## Architecture

```
CLI (cli.py)
  scan ──► places.py  (geocode → Nearby Search → Place Details)
              │
              ├── db.py  (upsert businesses, idempotency)
              │
              └── checker.py  (async httpx website quality)
  report ──► reporter.py  (query DB → CSV)
  stats  ──► reporter.py  (rich table)
```

- **`scanner/db.py`** — All SQLite work. Two tables: `businesses` and `website_checks`. `Business` and `WebsiteCheckResult` dataclasses live here.
- **`scanner/places.py`** — Google Places API. `fetch_area()` is the main entry point: geocodes, runs Nearby Search, fetches Place Details, writes to DB.
- **`scanner/checker.py`** — Async `httpx` checks with `asyncio.Semaphore(10)`. Returns results to caller; never writes DB directly (thread safety).
- **`scanner/cli.py`** — Typer app. `scan` command wires all three phases; DB writes from website checks happen in a single transaction after `asyncio.run()` returns.
- **`scanner/reporter.py`** — Read-only. Generates timestamped CSV files and rich stats table.
- **`scanner/config.py`** — `load_config()` reads `.env`, validates API key, returns frozen `Config` dataclass.

## Key constraints (don't change without good reason)

- **All DB writes on main thread only.** `checker.py` is async-internal but returns results; `cli.py` writes them synchronously. Passing `sqlite3.Connection` into async coroutines causes `ProgrammingError`.
- **`insert_website_check` + `update_last_checked_at` must be in one transaction.** If they split, staleness checks diverge.
- **`_paginate_nearby_search` sleeps 2.1 s between pages.** This is a Google API requirement — removing it returns `INVALID_REQUEST`.
- **Place Details calls are sequential** (not async) with `cfg.place_details_delay_s` between them to stay under 10 QPS. This is intentional for v0 simplicity.
- **Default radius 1 km, max_results 50.** These are POC-safe defaults. A full St. Pete scan costs ~$55 — don't change defaults without user confirmation.

## Database

SQLite at `data/scanner.db` (gitignored, auto-created).

```sql
-- Key queries
SELECT * FROM businesses WHERE website IS NULL AND details_fetched_at IS NOT NULL;  -- no-website leads
SELECT b.*, MAX(wc.score) FROM businesses b JOIN website_checks wc ON wc.place_id = b.place_id WHERE wc.score < 40;  -- poor sites
```

## Website quality scoring

| Signal | Points |
|--------|--------|
| Reachable (HTTP 2xx) | 35 |
| Has SSL | 20 |
| Mobile viewport | 20 |
| Load < 2 s | 15 |
| Has `<title>` | 5 |
| Has `<meta description>` | 5 |

Score < 40 = poor website lead (flagged in poor_website CSV).

## Adding new features

**New CLI command:** add `@app.command()` in `cli.py`, import locally to avoid circular imports.

**New website metric:** add constant in `checker.py`, update `compute_score()`, add column to `website_checks` schema in `db.py` (requires schema migration — add `ALTER TABLE` or recreate DB).

**Full city grid scan (v1):** implement in `places.py` as `fetch_area_grid()`. Use overlapping tiles (lat/lng increments), collect all place_ids, deduplicate by place_id (DB's UNIQUE constraint handles this), then fetch details for new ones.

## Cost guard

Always set a Google Cloud budget alert before running large scans:
- 50 businesses ≈ $0.95
- 300 businesses ≈ $6
- 2,000 businesses ≈ $55

## Open questions / v1 backlog

1. `--type` filter for Nearby Search (e.g. `restaurant` only)
2. Grid scan mode for full city coverage
3. `--refresh` flag to re-fetch stale Place Details
4. `--db` option to use a separate database per city
5. `businesses.last_checked_at` is a denormalized cache of `MAX(website_checks.checked_at)` — consider dropping it and always deriving from the checks table
