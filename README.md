# LocalBusinessScanner

A Python CLI that finds local businesses without websites (or with poor-quality ones) so you can reach out and offer web design services.

## How it works

1. **Discover** — searches for businesses within a given radius of a city
2. **Enrich** — fetches website URL and phone number for each business
3. **Analyze** — checks each website for 6 quality signals and scores it 0–100
4. **Report** — exports two CSV files: businesses with no website, and businesses with a weak website

Data is cached in SQLite — re-running a scan skips businesses already in the database.

## Data sources

The tool automatically picks based on which API keys you provide:

| Mode | Keys needed | Cost | Coverage |
|------|------------|------|----------|
| **Google Places** (recommended) | `GOOGLE_MAPS_API_KEY` | ~$0.95/POC scan¹ | Best — all business types |
| **Free mode** | None (+ optional `YELP_API_KEY`) | Free | Good for POC |

¹ Google gives $200/month in free credits — the POC scan fits comfortably within that. You need a billing account but likely never pay anything for this use case.

**Free mode** combines:
- **Overpass/OSM** — no key, no cost. Good for shops, restaurants, amenities. Weaker on service businesses (plumbers, electricians) that aren't well-mapped.
- **Yelp Fusion** (if `YELP_API_KEY` set) — free key, 500 calls/day. Adds food and retail coverage. Note: Yelp's free search API doesn't include business website URLs, so Yelp-sourced businesses show up in the no-website leads list.

## Setup

### 1. Clone and install

```bash
git clone git@github.com:wardcrazy01894/LocalBusinessScanner.git
cd LocalBusinessScanner
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — see options below
```

**Option A — Google Places (best data, ~free for this use case):**
1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project, enable the **Places API**
3. Create an API key under **APIs & Services → Credentials**
4. Set a $10/day budget alert to be safe
5. Add to `.env`: `GOOGLE_MAPS_API_KEY=your_key_here`

**Option B — Free mode (no billing required):**
1. Leave `GOOGLE_MAPS_API_KEY` blank
2. Optional: get a free Yelp key at [yelp.com/developers](https://www.yelp.com/developers) → add `YELP_API_KEY=your_key`
3. Overpass/OSM runs automatically with no key at all

## Usage

### Run a scan (mode chosen automatically from .env)

```bash
python -m scanner scan "Highland, Utah"
python -m scanner scan "St. Petersburg, Florida"
```

### Expand the radius or result count

```bash
python -m scanner scan "Highland, Utah" --radius-km 3 --max-results 100
```

### Skip website quality checks (discovery only)

```bash
python -m scanner scan "Highland, Utah" --skip-check
```

### Generate reports from existing data

```bash
python -m scanner report               # both reports
python -m scanner report no-website    # only businesses with no website
python -m scanner report poor-website  # only weak sites
python -m scanner report --threshold 60
```

### View database summary

```bash
python -m scanner stats
```

## Output

Reports are saved to `reports/` with timestamps:

| File | Contents |
|------|----------|
| `no_website_YYYYMMDD_HHMMSS.csv` | Businesses with no website found |
| `poor_website_YYYYMMDD_HHMMSS.csv` | Businesses with quality score < 40 |

CSV columns: `place_id, name, address, phone, website, score, scan_area`

## Website quality score (0–100)

| Signal | Points | Why it matters |
|--------|--------|----------------|
| Reachable (HTTP 2xx) | 35 | Non-working site is worse than no site |
| Has SSL (https://) | 20 | Google demotes HTTP; visitors notice the padlock |
| Mobile viewport | 20 | ~60% of local searches are on mobile |
| Load time < 2 s | 15 | Bounce rate doubles above 3 s |
| Has `<title>` tag | 5 | Basic SEO hygiene |
| Has `<meta description>` | 5 | Shows up in Google snippets |

**Score < 40 = poor website lead** — missing SSL + no mobile viewport alone gets you there.

## Cost estimates (Google mode)

| Scenario | Businesses | Approx. cost |
|----------|-----------|-------------|
| POC — 1 km radius, 50 places | ~50 | **~$0.95** |
| Small city full scan | ~300 | ~$6 |
| Large city (St. Pete) full grid | ~2,000 | ~$55 |

Google gives $200/month free — all of the above fits within the free tier.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_MAPS_API_KEY` | *(empty → free mode)* | Enables Google Places mode |
| `YELP_API_KEY` | *(empty → skip Yelp)* | Adds Yelp results in free mode |
| `DB_PATH` | `data/scanner.db` | SQLite database path |
| `REPORTS_DIR` | `reports` | CSV output directory |
| `CHECK_CONCURRENCY` | `10` | Max simultaneous website checks |
| `PLACE_DETAILS_DELAY_S` | `0.15` | Delay between Google Place Details calls |
| `WEBSITE_CHECK_TIMEOUT_S` | `10` | HTTP timeout per website check |
| `STALENESS_DAYS` | `7` | Re-check websites older than N days |

## Project structure

```
scanner/
  config.py        — env loading, Config dataclass, data source selection
  db.py            — SQLite schema + CRUD (Business, WebsiteCheckResult)
  places.py        — Google Places: geocode, Nearby Search, Place Details
  free_sources.py  — Free fallback: Nominatim geocoding, Overpass/OSM, Yelp
  checker.py       — Async httpx website quality checker
  reporter.py      — CSV export and rich stats table
  cli.py           — Typer CLI (scan, report, stats)
data/              — SQLite database (gitignored)
reports/           — CSV output (gitignored)
PLAN.md            — Architecture decisions and rationale
CLAUDE.md          — Claude Code context (for AI-assisted development)
```

## Roadmap (v1)

- Grid scan mode for full city coverage beyond the 60-result Nearby Search cap
- `--type` filter (e.g. `--type restaurant` to scan only one category)
- `--refresh` flag to re-fetch stale Place Details
- Yelp Business Details calls to retrieve actual website URLs in free mode
- `--db` option for per-city database isolation
