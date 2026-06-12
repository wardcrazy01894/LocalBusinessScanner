"""
scanner/cli.py — Typer CLI command definitions.

Commands: scan, report, stats

Data source is chosen automatically:
  - GOOGLE_MAPS_API_KEY set  → Google Places (best coverage, ~$0.95/POC)
  - GOOGLE_MAPS_API_KEY unset → Overpass/OSM + Yelp (free, less coverage)

Wiring order in scan:
  1. load_config() — determines data source
  2. get_connection() + init_schema()
  3. places.fetch_area() OR free_sources.fetch_area_free() → DB writes
  4. db.get_businesses_needing_check() → list[Business]
  5. asyncio.run(checker.run_checks()) → list[WebsiteCheckResult]
  6. Transaction: insert_website_check() + update_last_checked_at() per result
  7. reporter.print_stats()

All DB writes happen synchronously on the main thread. Step 6 is wrapped
in a single BEGIN/commit so a partial failure doesn't leave stale state.

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
        help="Search radius in kilometres. Default 1.0 km (POC-safe).",
        min=0.1,
        max=50.0,
    ),
    max_results: int = typer.Option(
        50,
        "--max-results",
        help="Maximum businesses to fetch. Default 50. Google mode: max 60.",
        min=1,
        max=500,
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
        help="Fetch business data only; skip website quality checks.",
    ),
    include_chains: bool = typer.Option(
        False,
        "--include-chains",
        help="Include chain businesses and non-leads (churches, schools) in stats.",
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="[NOT IMPLEMENTED v0] Re-fetch details for businesses already in DB.",
    ),
    env_file: Optional[Path] = typer.Option(
        None,
        "--env-file",
        help="Path to .env file (default: .env in CWD).",
        exists=True,
    ),
) -> None:
    """Scan local businesses in AREA for web design leads.

    Auto-selects data source based on which API keys are in .env:
      - Google Maps key set  → Google Places (best coverage, ~$0.95/50 biz)
      - No Google key        → Overpass/OSM + Yelp (free, good for POC)

    Results are saved to SQLite. Re-runs skip already-fetched businesses.
    """
    from . import db as db_mod
    from . import checker as checker_mod
    from . import reporter as reporter_mod
    from .config import load_config

    if refresh:
        console.print("[red]--refresh is not implemented in v0.[/red]")
        raise typer.Exit(1)

    cfg = load_config(env_file)
    conn = db_mod.get_connection(cfg.db_path)
    db_mod.init_schema(conn)

    # Show which data source will be used
    source_style = "green" if cfg.use_google else "yellow"
    console.print(f"\n[bold cyan]LocalBusinessScanner[/bold cyan] — [bold]{area}[/bold]")
    console.print(f"  data source: [{source_style}]{cfg.data_source_label}[/{source_style}]")
    console.print(f"  radius: {radius_km} km  |  max: {max_results} businesses\n")

    # Phase 1: Discover businesses
    if cfg.use_google:
        # Enforce Google's 60-result cap
        google_max = min(max_results, 60)
        if max_results > 60:
            console.print("[dim]  Note: Google Nearby Search is capped at 60 results. Use grid scan (v1) for more.[/dim]")
        from . import places as places_mod
        with console.status("[bold green]Fetching from Google Places..."):
            counts = places_mod.fetch_area(cfg, conn, area, radius_km, google_max, yes)
        console.print(
            f"[green]✓[/green] Google: {counts['searched']} found, "
            f"{counts['new_stubs']} new, {counts['details_fetched']} details fetched, "
            f"{counts['skipped']} already in DB\n"
        )
    else:
        from . import free_sources as fs_mod
        with console.status("[bold yellow]Fetching from free sources..."):
            counts = fs_mod.fetch_area_free(cfg, conn, area, radius_km, max_results, yes)
        console.print(
            f"[green]✓[/green] Free sources: {counts['searched']} found, "
            f"{counts['new_stubs']} new, {counts['skipped']} already in DB\n"
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
            console.print("[dim]No new websites to check (all fresh or none listed).[/dim]\n")

    # Summary
    reporter_mod.print_stats(conn, filter_chains=not include_chains)
    conn.close()


@app.command()
def report(
    report_type: str = typer.Argument(
        "all",
        help='"no-website", "poor-website", "social-only", or "all".',
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
    include_chains: bool = typer.Option(
        False,
        "--include-chains",
        help="Include chain businesses and non-leads (churches, schools) in the CSV.",
    ),
    env_file: Optional[Path] = typer.Option(
        None,
        "--env-file",
        exists=True,
    ),
) -> None:
    """Generate CSV lead reports from the database.

    "no-website"   — businesses with no website found.
    "poor-website" — businesses with website quality score < THRESHOLD.
    "social-only"  — businesses using Facebook/Instagram/Yelp as their 'website'.
    "all"          — generate all three reports (default).

    Chains and non-leads (churches, schools, government) are filtered out
    by default. Pass --include-chains to keep them.
    """
    from . import db as db_mod
    from . import reporter as reporter_mod
    from .config import load_config

    cfg = load_config(env_file)
    conn = db_mod.get_connection(cfg.db_path)
    db_mod.init_schema(conn)
    out = output_dir or cfg.reports_dir
    fc = not include_chains

    if report_type in ("no-website", "all"):
        path, kept, skipped = reporter_mod.generate_no_website_report(conn, out, filter_chains=fc)
        note = f" ({skipped} chains/non-leads excluded)" if skipped else ""
        console.print(f"[green]✓[/green] No-website leads: [cyan]{path}[/cyan] — {kept} businesses{note}")

    if report_type in ("social-only", "all"):
        path, kept, skipped = reporter_mod.generate_social_only_report(conn, out, filter_chains=fc)
        note = f" ({skipped} chains/non-leads excluded)" if skipped else ""
        console.print(f"[green]✓[/green] Social-only leads: [cyan]{path}[/cyan] — {kept} businesses{note}")

    if report_type in ("poor-website", "all"):
        path, kept, skipped = reporter_mod.generate_poor_website_report(conn, out, score_threshold, filter_chains=fc)
        note = f" ({skipped} chains/non-leads excluded)" if skipped else ""
        console.print(f"[green]✓[/green] Poor-website leads: [cyan]{path}[/cyan] — {kept} businesses{note}")

    conn.close()


@app.command()
def stats(
    include_chains: bool = typer.Option(
        False,
        "--include-chains",
        help="Include chain businesses and non-leads in counts.",
    ),
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
    reporter_mod.print_stats(conn, filter_chains=not include_chains)
    conn.close()
