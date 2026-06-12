# LocalBusinessScanner — Implementation Plan

## Purpose

Lead-gen CLI for a web design business. Scans local businesses via Google Places API,
checks website quality, and surfaces businesses with no site or a poor one as sales
prospects. v0 is a POC scoped to small radius / low result counts so the user can
validate the flow before incurring meaningful API cost.

---

## Cost Estimates

### Highland, UT POC (50 businesses, 1 km radius)

| Step              | Unit cost   | Quantity | Subtotal |
|-------------------|-------------|----------|----------|
| Nearby Search     | $0.032/req  | 3 pages  | $0.096   |
| Place Details     | $0.017/place| 50 places| $0.850   |
| **POC total**     |             |          | **~$0.95** |

### Full St. Pete grid scan (~2 000 businesses)

Grid covering St. Petersburg (~200 km²) at 1 km radius with ~30% overlap:
~220 Nearby Search tiles × 3 pages each = 660 requests.

| Step              | Unit cost   | Quantity  | Subtotal  |
|-------------------|-------------|-----------|-----------|
| Nearby Search     | $0.032/req  | 660 reqs  | $21.12    |
| Place Details     | $0.017/place| 2 000     | $34.00    |
| **Full total**    |             |           | **~$55**  |

The idempotency layer (skip place_ids already in DB) means a re-run after a
partial failure costs only the incremental difference, not the full amount again.

---

## Architecture Overview

```
CLI (typer)
  │
  ├── scan ──► places.py  (geocode → Nearby Search → Place Details)
  │               │
  │               ├── db.py  (upsert businesses, idempotency checks)
  │               │
  │               └── checker.py  (async website quality scoring)
  │
  ├── report ──► reporter.py  (query DB → write CSV)
  │
  └── stats ──► reporter.py  (aggregate counts, print rich table)
```

All DB writes happen synchronously on the main thread (see §SQLite thread safety).
`checker.py` is async-internal but returns results to the caller before any DB write.

---

## Data Model

### Table: `businesses`

| Column              | Type    | Notes                                    |
|---------------------|---------|------------------------------------------|
| place_id            | TEXT PK | Google Places unique ID                  |
| name                | TEXT    |                                          |
| address             | TEXT    |                                          |
| phone               | TEXT    | formatted_phone_number from Place Details|
| website             | TEXT    | NULL if not listed                       |
| lat                 | REAL    |                                          |
| lng                 | REAL    |                                          |
| types               | TEXT    | JSON array (e.g. ["restaurant","food"])  |
| scan_area           | TEXT    | "city, state" string used to find it     |
| first_seen_at       | TEXT    | ISO-8601 UTC                             |
| last_checked_at     | TEXT    | ISO-8601 UTC; NULL until checked         |
| details_fetched_at  | TEXT    | ISO-8601 UTC; NULL until details fetched |

### Table: `website_checks`

| Column          | Type    | Notes                                  |
|-----------------|---------|----------------------------------------|
| id              | INTEGER | PK autoincrement                       |
| place_id        | TEXT    | FK → businesses.place_id               |
| checked_at      | TEXT    | ISO-8601 UTC                           |
| reachable       | INTEGER | 0/1                                    |
| has_ssl         | INTEGER | 0/1                                    |
| has_viewport    | INTEGER | 0/1                                    |
| load_time_ms    | INTEGER | NULL if unreachable                    |
| has_title       | INTEGER | 0/1                                    |
| has_meta_desc   | INTEGER | 0/1                                    |
| score           | INTEGER | 0–100                                  |
| http_status     | INTEGER | NULL if no response                    |
| error_msg       | TEXT    | NULL if no error                       |

---

## Website Quality Scoring

Scores are additive; max = 100.

| Metric                    | Weight | Rationale                                      |
|---------------------------|--------|------------------------------------------------|
| Reachable (HTTP 200)      | 35 pts | Non-working site is worse than no site         |
| Has SSL (https://)        | 20 pts | Google demotes HTTP; clients notice padlock     |
| Mobile viewport meta tag  | 20 pts | ~60% of local searches are mobile              |
| Page load < 2 s           | 15 pts | Bounce rate doubles above 3 s                  |
| Has `<title>` with content| 5 pts  | Basic SEO hygiene                              |
| Has `<meta description>`  | 5 pts  | Shows up in search snippets                    |

**Score < 40 = "poor website lead"** (missing SSL + viewport alone gets you there).
Score = 0 implicitly for businesses with no website at all (never run through checker).

---

## Milestones

### M0 — Scaffold (parallel, no dependencies)

- **T0-A** `scanner/config.py` — load env vars, dataclasses for settings
- **T0-B** `scanner/db.py` — schema creation, upsert helpers
- **T0-C** `requirements.txt`, `.env.example`, `.gitignore`

### M1 — Places integration (depends on M0)

- **T1-A** `scanner/places.py` — geocode(), nearby_search(), place_details(), fetch_area()
  - Inputs: config.Config, location string, radius_km, max_results
  - Outputs: list[Business] written to DB
  - Dependencies: T0-A, T0-B

### M2 — Website checker (depends on M0, can run parallel with M1)

- **T2-A** `scanner/checker.py` — async check_website(), run_checks()
  - Inputs: list of website URLs + place_ids
  - Outputs: list[WebsiteCheckResult] returned to caller (no direct DB writes)
  - Dependencies: T0-A

### M3 — Reporter (depends on M0)

- **T3-A** `scanner/reporter.py` — generate_no_website_report(), generate_poor_website_report(), print_stats()
  - Inputs: db path, output dir
  - Outputs: CSV files, rich console output

### M4 — CLI wiring (depends on M1, M2, M3)

- **T4-A** `scanner/cli.py` — scan, report, stats commands
  - Wires M1 → M2 → DB writes → M3
  - Dependencies: T1-A, T2-A, T3-A

### M5 — Entry point + packaging (depends on M4)

- **T5-A** `scanner/__main__.py` — `python -m scanner` entry
- **T5-B** `scanner/__init__.py` — package exports

---

## Edge Cases and Failure Modes

### Rate Limiting on Place Details (10 QPS)

The Places API allows 10 QPS on Place Details. With 50 calls fired concurrently,
you will get `OVER_QUERY_LIMIT` responses.

Mitigation: `places.py` fetches Place Details **sequentially with a 0.15-second
inter-call sleep** for v0. This means 50 calls ≈ 8 seconds — acceptable for a CLI.
A semaphore-based async approach is noted as a v1 upgrade path.

### `next_page_token` Delay

Google's Nearby Search API requires a **2-second delay** before using a
`next_page_token` to fetch the next page of 20 results. Firing the next request
immediately returns `INVALID_REQUEST`.

Mitigation: `places.py` calls `time.sleep(2.1)` after receiving a page token before
making the next page request. This is documented in the `_paginate_nearby_search`
helper.

### City/Area Geocoding Ambiguity

`geocode("Highland")` returns results for Highland, IL; Highland, CA; Highland, UT
and others. Taking index [0] blindly is unreliable.

Mitigation: `config.py` stores the raw `scan_area` string and `places.py` logs a
`WARNING` if the geocoded result's `formatted_address` does not contain the input
string (case-insensitive). The user is shown the resolved address before any search
begins and prompted to confirm (or pass `--yes` to skip). The geocode call uses
`components={"country": "US"}` by default to narrow results.

### SQLite Thread Safety

Python's `sqlite3` module uses `check_same_thread=True` by default. If an async
coroutine writes to the DB from a worker thread (e.g. via `asyncio.to_thread`), it
will raise `ProgrammingError`.

Mitigation: All DB reads and writes happen **on the main thread only**. `checker.py`
is purely async-internal: it returns a list of `WebsiteCheckResult` objects to the
caller in `cli.py`, which then writes them to the DB synchronously. No DB handles
are passed into async functions.

### Website Check Concurrency vs. Politeness

Checking 50 different business websites concurrently is generally fine (they are
independent servers, not the same host). However, if many businesses share a
hosting provider (e.g. Squarespace, Wix), too many simultaneous connections to
the same upstream could trigger rate limiting or temporary IP bans.

Mitigation: `checker.py` uses `asyncio.Semaphore(10)` — maximum 10 concurrent
website checks. This is configurable via `config.Config.check_concurrency`
(default 10, max recommended 20).

### Stale Data / Re-checks

A business in the DB from a previous scan may have since built a website or
improved it. The idempotency logic skips re-fetching Place Details for businesses
already present, and skips re-checking websites checked within the last N days.

`--refresh` flag: v0 accepts the flag on `scan` but raises `NotImplementedError`
with milestone reference M-REFRESH. The plan for v1 is:
- `--refresh` clears `details_fetched_at` for businesses in the scan area, forcing
  Place Details re-fetch.
- `--recheck` (separate flag) clears `last_checked_at`, forcing website re-check.

### httpx SSL Errors

Some small-business websites have expired or self-signed certificates. A strict
`httpx` call will raise `SSLError` and we'd mark the site unreachable.

Mitigation: `checker.py` uses `verify=False` on the httpx client **only for the
reachability check**, so we can still detect the site is up. The `has_ssl` metric
then scores based on whether the URL scheme is `https://` **and** the certificate
was valid (tracked via a `ssl_valid` boolean, distinct from `has_ssl`).

Actually, for v0 simplicity: `verify=True` (default), `has_ssl` is True if scheme
is `https://` and no SSL exception was raised. If SSLError is raised: `reachable=False`,
`has_ssl=False`, `error_msg="SSLError"`. This is documented so a future reviewer
can decide to add `verify=False` fallback.

### API Key Not Set

If `GOOGLE_MAPS_API_KEY` is not in `.env` / environment, the tool should fail fast
with a clear message, not a cryptic `googlemaps` exception deep in the stack.

Mitigation: `config.py` validates the key at startup and raises `SystemExit` with
a human-readable message.

### Partial Scan Failure Recovery

If a scan run is interrupted mid-way (e.g. keyboard interrupt, network timeout),
the businesses already written to the DB are preserved. A re-run will skip those
place_ids (idempotency) and continue from where it left off.

The website-check phase is similarly idempotent: businesses with a `last_checked_at`
within the staleness window are skipped.

### `--max-results` Guardrail

Nearby Search returns results in pages of 20. `--max-results 50` means we fetch
at most 3 pages (60 raw results) and truncate to 50 before Place Details calls.
`--max-results 20` fetches one page only (no page tokens, no 2-second delay).

---

## v1 Upgrade Notes (out of scope for v0, documented for context)

- Grid scanning for full city coverage (lat/lng grid tiles)
- `--refresh` and `--recheck` flags (stubs present in v0)
- Async Place Details with semaphore for speed
- Email draft generation per lead
- `types` filter (e.g. `--type restaurant` to scan only restaurants)
- Confidence scoring on geocode result (use `geometry.location_type == "ROOFTOP"`)

---

## Security Boundaries

- The Google Maps API key is read from environment / `.env` only. Never logged,
  never included in CSV output.
- `httpx` calls to business websites use a descriptive `User-Agent` header
  (`LocalBusinessScanner/0.1`). No cookies, no session state, no form submission.
- SQLite database is local-only. No network-accessible DB in v0.
- `.gitignore` excludes `.env`, `data/*.db`, and `reports/*.csv`.
