"""
scanner/cli.py — Typer CLI command definitions.

Commands: scan, report, stats

Wiring order in scan:
  1. load_config()
  2. get_connection() + init_schema()
  3. places.fetch_area() → DB writes (stubs + details)
  4. db.get_businesses_needing_check() → list[Business]
  5. asyncio.run(checker.run_checks()) → list[WebsiteCheckResult]
  6. Transaction: insert_website_check() + update_last_checked_at() per result
  7. reporter.print_stats()

All DB writes happen synchronously on the main thread (step 6 is wrapped
in a single BEGIN/commit so a partial failure doesn't leave stale state).

Milestone: M4-A
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

app = typer.Typer(
    name="scanner",
    help="LocalBusinessScanner — find web design leads in your area.",
    add_completion=False,
)

console = Console()


@app.command()
def scan(
    area: str = typer.Argument(
        ...,
        help='Location to scan, e.g. "Highland, UT" or "St. Petersburg, FL".',
    ),
    radius_km: float = typer.Option(
        1.0,
        "--radius-km",
        help="Search radius in kilometres. Default 1.0 km (POC-safe, ~$0.95 total).",
        min=0.1,
        max=50.0,
    ),
    max_results: int = typer.Option(
        50,
        "--max-results",
        help="Maximum businesses to fetch (1–60). Default 50.",
        min=1,
        max=60,
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip geocode confirmation prompt.",
    ),
    skip_check: bool = typer.Option(
        False,
        "--skip-check",
        help="Fetch Places data only; skip website quality checks.",
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="[NOT IMPLEMENTED v0] Re-fetch Place Details for businesses already in DB.",
    ),
    env_file: Optional[Path] = typer.Option(
        None,
        "--env-file",
        help="Path to .env file (default: .env in CWD).",
        exists=True,
    ),
) -> None:
    """Scan local businesses in AREA for web design leads.

    Geocodes AREA, runs a Google Nearby Search within RADIUS_KM, fetches
    Place Details (website + phone), checks website quality, and saves
    everything to SQLite.

    Cost estimate with defaults (50 businesses, 1 km): ~$0.95.
    Re-runs are idempotent — already-fetched businesses are skipped.
    """
    from . import db as db_mod
    from . import places as places_mod
    from . import checker as checker_mod
    from . import reporter as reporter_mod
    from .config import load_config

    if refresh:
        console.print("[red]--refresh is not implemented in v0.[/red]")
        raise typer.Exit(1)

    cfg = load_config(env_file)
    conn = db_mod.get_connection(cfg.db_path)
    db_mod.init_schema(conn)

    console.print(f"\n[bold cyan]LocalBusinessScanner[/bold cyan] — [bold]{area}[/bold]")
    console.print(f"  radius: {radius_km} km  |  max: {max_results} businesses\n")

    # Phase 1: Places API
    with console.status("[bold green]Fetching businesses from Google Places..."):
        counts = places_mod.fetch_area(cfg, conn, area, radius_km, max_results, yes)

    console.print(
        f"[green]✓[/green] Places: {counts['searched']} found, "
        f"{counts['new_stubs']} new, {counts['details_fetched']} details fetched, "
        f"{counts['skipped']} already in DB\n"
    )

    # Phase 2: Website checks
    if not skip_check:
        businesses = db_mod.get_businesses_needing_check(conn, cfg.staleness_days)
        if businesses:
            console.print(f"[bold]Checking {len(businesses)} websites...[/bold]")
            with console.status("Analyzing websites..."):
                results = asyncio.run(checker_mod.run_checks(cfg, businesses))

            # Wrap both writes in one transaction to keep last_checked_at in sync
            now = datetime.utcnow()
            conn.execute("BEGIN")
            for result in results:
                db_mod.insert_website_check(conn, result)
                db_mod.update_last_checked_at(conn, result.place_id, now)
            conn.commit()

            console.print(f"[green]✓[/green] Checked {len(results)} websites\n")
        else:
            console.print("[dim]No new websites to check (all fresh or no websites listed).[/dim]\n")

    # Summary
    reporter_mod.print_stats(conn)
    conn.close()


@app.command()
def report(
    report_type: str = typer.Argument(
        "all",
        help='"no-website", "poor-website", or "all".',
    ),
    output_dir: Optional[Path] = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Directory for CSV output (default: reports/).",
    ),
    score_threshold: int = typer.Option(
        40,
        "--threshold",
        help="Score below which a website is 'poor'. Default 40.",
        min=0,
        max=100,
    ),
    env_file: Optional[Path] = typer.Option(
        None,
        "--env-file",
        exists=True,
    ),
) -> None:
    """Generate CSV lead reports from the database.

    "no-website"   — businesses with no website listed on Google.
    "poor-website" — businesses with website quality score < THRESHOLD.
    "all"          — generate both reports (default).
    """
    from . import db as db_mod
    from . import reporter as reporter_mod
    from .config import load_config

    cfg = load_config(env_file)
    conn = db_mod.get_connection(cfg.db_path)
    db_mod.init_schema(conn)
    out = output_dir or cfg.reports_dir

    if report_type in ("no-website", "all"):
        path = reporter_mod.generate_no_website_report(conn, out)
        console.print(f"[green]✓[/green] No-website leads: [cyan]{path}[/cyan]")

    if report_type in ("poor-website", "all"):
        path = reporter_mod.generate_poor_website_report(conn, out, score_threshold)
        console.print(f"[green]✓[/green] Poor-website leads: [cyan]{path}[/cyan]")

    conn.close()


@app.command()
def stats(
    env_file: Optional[Path] = typer.Option(
        None,
        "--env-file",
        exists=True,
    ),
) -> None:
    """Print a summary of the database to the terminal."""
    from . import db as db_mod
    from . import reporter as reporter_mod
    from .config import load_config

    cfg = load_config(env_file)
    conn = db_mod.get_connection(cfg.db_path)
    db_mod.init_schema(conn)
    reporter_mod.print_stats(conn)
    conn.close()
