# LocalBusinessScanner

A Python CLI that finds local businesses without websites (or with poor-quality ones) so you can reach out and offer web design services.

## How it works

1. **Discover** — searches Google Places for businesses within a given radius of a city
2. **Enrich** — fetches Place Details (website URL, phone number) for each business
3. **Analyze** — checks each website for 6 quality signals and scores it 0–100
4. **Report** — exports two CSV files: businesses with no website, and businesses with a weak website

Data is cached in SQLite — re-running a scan skips businesses already in the database.

## Setup

### 1. Clone and install

```bash
git clone git@github.com:wardcrazy01894/LocalBusinessScanner.git
cd LocalBusinessScanner
pip install -r requirements.txt
```

### 2. Get a Google Maps API key

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project and enable the **Places API**
3. Create an API key under **APIs & Services → Credentials**
4. Set a budget alert (recommended: $10/day) to avoid surprise charges

### 3. Configure

```bash
cp .env.example .env
# Edit .env — add your GOOGLE_MAPS_API_KEY
```

## Usage

### Run a proof-of-concept scan (safe defaults: 1 km radius, 50 businesses)

```bash
python -m scanner scan "Highland, Utah"
python -m scanner scan "St. Petersburg, Florida"
```

### Expand the radius or result count

```bash
python -m scanner scan "Highland, Utah" --radius-km 3 --max-results 60
```

### Skip website quality checks (Places data only)

```bash
python -m scanner scan "Highland, Utah" --skip-check
```

### Generate reports from existing data

```bash
python -m scanner report                    # both reports
python -m scanner report no-website        # only businesses with no website
python -m scanner report poor-website      # only weak sites
python -m scanner report --threshold 60    # flag anything below 60
```

### View database summary

```bash
python -m scanner stats
```

## Output

Reports are saved to `reports/` with timestamps:

| File | Contents |
|------|----------|
| `no_website_YYYYMMDD_HHMMSS.csv` | Businesses with no website on Google |
| `poor_website_YYYYMMDD_HHMMSS.csv` | Businesses with quality score < 40 |

CSV columns: `place_id, name, address, phone, website, score, scan_area`

## Website quality score (0–100)

| Signal | Points | Why it matters |
|--------|--------|----------------|
| Reachable (HTTP 2xx) | 35 | Non-working site = worse than no site |
| Has SSL (https://) | 20 | Google demotes HTTP; clients notice the padlock |
| Mobile viewport | 20 | ~60% of local searches are on mobile |
| Load time < 2 s | 15 | Bounce rate doubles above 3 s |
| Has `<title>` tag | 5 | Basic SEO hygiene |
| Has `<meta description>` | 5 | Shows up in Google snippets |

**Score < 40 = poor website lead** — missing SSL + no mobile viewport alone gets you there.

## Cost estimates

| Scenario | Businesses | Approx. cost |
|----------|-----------|-------------|
| POC — 1 km radius, 50 places | ~50 | **~$0.95** |
| Small city full scan | ~300 | ~$6 |
| Large city (St. Pete) full grid | ~2 000 | ~$55 |

Billing:
- Nearby Search: $0.032/request (20 results per request, max 3 pages = 60 max)
- Place Details (Contact tier): $0.017/place

The idempotency layer means a re-run after a partial failure only bills for the incremental difference.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_MAPS_API_KEY` | *(required)* | Google Maps Platform API key |
| `DB_PATH` | `data/scanner.db` | SQLite database path |
| `REPORTS_DIR` | `reports` | CSV output directory |
| `CHECK_CONCURRENCY` | `10` | Max simultaneous website checks |
| `PLACE_DETAILS_DELAY_S` | `0.15` | Delay between Place Details calls (rate limiting) |
| `WEBSITE_CHECK_TIMEOUT_S` | `10` | HTTP timeout per website check |
| `STALENESS_DAYS` | `7` | Re-check websites older than this many days |

## Project structure

```
scanner/
  config.py     — env var loading and Config dataclass
  db.py         — SQLite schema and CRUD (Business, WebsiteCheckResult dataclasses)
  places.py     — Google Places API: geocode, Nearby Search, Place Details
  checker.py    — Async httpx website quality checker
  reporter.py   — CSV export and rich stats table
  cli.py        — Typer CLI commands (scan, report, stats)
data/           — SQLite database (gitignored)
reports/        — CSV output (gitignored)
PLAN.md         — Architecture decisions and implementation notes
```

## Roadmap (v1)

- `--type` filter (e.g. `--type restaurant` to scan only one category)
- Grid scan mode for full city coverage beyond 60-result radius limit
- `--refresh` flag to re-fetch Place Details for existing businesses
- Per-area separate database files (`--db`)
