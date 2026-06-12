"""
scanner/reporter.py — Report generation and stats display.

No network I/O; no DB writes. All functions are synchronous.

Chain filtering (on by default):
  Businesses matching scanner/chains.py are excluded from report output.
  Pass filter_chains=False to include them.

Milestone: M3-A
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


def generate_no_website_report(
    conn: sqlite3.Connection,
    output_dir: Path,
    filter_chains: bool = True,
) -> tuple[Path, int, int]:
    """Write a CSV of businesses with no website.

    Returns (path, total_rows, filtered_out) so the caller can report
    how many chains were excluded.
    """
    from .chains import is_chain

    output_dir.mkdir(parents=True, exist_ok=True)

    rows = conn.execute(
        """
        SELECT place_id, name, address, phone, types, scan_area
        FROM businesses
        WHERE website IS NULL AND details_fetched_at IS NOT NULL
        ORDER BY name ASC
        """
    ).fetchall()

    kept, skipped = [], 0
    for r in rows:
        types = json.loads(r["types"]) if r["types"] else []
        if filter_chains and is_chain(r["name"], types):
            skipped += 1
        else:
            kept.append(dict(r))

    path = output_dir / _timestamped_filename("no_website")
    _write_csv(path, kept, ["place_id", "name", "address", "phone", "scan_area"])
    return path, len(kept), skipped


def generate_poor_website_report(
    conn: sqlite3.Connection,
    output_dir: Path,
    score_threshold: int = 40,
    filter_chains: bool = True,
) -> tuple[Path, int, int]:
    """Write a CSV of businesses whose latest website check scored below threshold.

    Returns (path, total_rows, filtered_out).
    """
    from .chains import is_chain

    output_dir.mkdir(parents=True, exist_ok=True)

    rows = conn.execute(
        """
        SELECT b.place_id, b.name, b.address, b.phone, b.website,
               b.types, wc.score, b.scan_area
        FROM businesses b
        JOIN website_checks wc ON wc.place_id = b.place_id
        WHERE wc.checked_at = (
            SELECT MAX(wc2.checked_at) FROM website_checks wc2 WHERE wc2.place_id = b.place_id
        )
        AND wc.score < ?
        ORDER BY wc.score ASC
        """,
        (score_threshold,),
    ).fetchall()

    kept, skipped = [], 0
    for r in rows:
        types = json.loads(r["types"]) if r["types"] else []
        if filter_chains and is_chain(r["name"], types):
            skipped += 1
        else:
            kept.append(dict(r))

    path = output_dir / _timestamped_filename("poor_website")
    _write_csv(path, kept, ["place_id", "name", "address", "phone", "website", "score", "scan_area"])
    return path, len(kept), skipped


def generate_social_only_report(
    conn: sqlite3.Connection,
    output_dir: Path,
    filter_chains: bool = True,
) -> tuple[Path, int, int]:
    """Write a CSV of businesses whose only 'website' is a social/directory page.

    These are promising leads: they have SOME web presence (Facebook etc.)
    but no real website — warm leads for a web design pitch.
    Returns (path, total_rows, filtered_out).
    """
    from .chains import is_chain

    output_dir.mkdir(parents=True, exist_ok=True)

    rows = conn.execute(
        """
        SELECT b.place_id, b.name, b.address, b.phone, b.website,
               b.types, wc.error_msg, b.scan_area
        FROM businesses b
        JOIN website_checks wc ON wc.place_id = b.place_id
        WHERE wc.checked_at = (
            SELECT MAX(wc2.checked_at) FROM website_checks wc2 WHERE wc2.place_id = b.place_id
        )
        AND wc.is_social = 1
        ORDER BY b.name ASC
        """
    ).fetchall()

    kept, skipped = [], 0
    for r in rows:
        types = json.loads(r["types"]) if r["types"] else []
        if filter_chains and is_chain(r["name"], types):
            skipped += 1
        else:
            kept.append(dict(r))

    path = output_dir / _timestamped_filename("social_only")
    _write_csv(path, kept, ["place_id", "name", "address", "phone", "website", "scan_area"])
    return path, len(kept), skipped


def print_stats(conn: sqlite3.Connection, filter_chains: bool = True) -> None:
    """Print a rich summary table of database contents to stdout."""
    from rich.console import Console
    from rich.table import Table

    from .chains import is_chain
    from .db import get_stats

    stats = get_stats(conn)
    console = Console()

    # Count chain-filtered leads
    if filter_chains:
        raw_rows = conn.execute(
            """SELECT name, types FROM businesses
               WHERE website IS NULL AND details_fetched_at IS NOT NULL"""
        ).fetchall()
        chains_in_leads = sum(
            1 for r in raw_rows
            if is_chain(r["name"], json.loads(r["types"]) if r["types"] else [])
        )
        real_leads = stats["no_website"] - chains_in_leads
        chain_note = f" ({chains_in_leads} chains excluded)"
    else:
        real_leads = stats["no_website"]
        chain_note = ""

    # Count social-only businesses
    social_count = conn.execute(
        """SELECT COUNT(DISTINCT b.place_id) FROM businesses b
           JOIN website_checks wc ON wc.place_id = b.place_id
           WHERE wc.checked_at = (SELECT MAX(wc2.checked_at) FROM website_checks wc2 WHERE wc2.place_id = b.place_id)
           AND wc.is_social = 1"""
    ).fetchone()[0]

    table = Table(title="LocalBusinessScanner — Database Summary", show_header=True)
    table.add_column("Metric", style="cyan", min_width=34)
    table.add_column("Count", justify="right", style="bold")

    table.add_row("Total businesses", str(stats["total_businesses"]))
    table.add_row("Details fetched", str(stats["details_fetched"]))
    table.add_section()
    table.add_row(
        "[bold green]No website (hot leads)[/bold green]",
        f"[bold green]{real_leads}[/bold green]{chain_note}",
    )
    table.add_row(
        "[green]Social media only (warm leads)[/green]",
        f"[green]{social_count}[/green]",
    )
    table.add_row("Real website present", str(stats["website_present"] - social_count))
    table.add_section()
    table.add_row("Websites checked", str(stats["checked"]))
    table.add_row("[yellow]Poor website (<40)[/yellow]", f"[yellow]{stats['poor_website']}[/yellow]")
    table.add_row("[green]Good website (≥40)[/green]", f"[green]{stats['good_website']}[/green]")

    console.print(table)


def _write_csv(output_path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _timestamped_filename(prefix: str, ext: str = "csv") -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}.{ext}"
