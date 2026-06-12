"""
scanner/reporter.py — Report generation and stats display.

No network I/O; no DB writes. All functions are synchronous.

Milestone: M3-A
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


def generate_no_website_report(
    conn: sqlite3.Connection,
    output_dir: Path,
) -> Path:
    """Write a CSV of businesses with no website (confirmed via Place Details fetch)."""
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = conn.execute(
        """
        SELECT place_id, name, address, phone, scan_area
        FROM businesses
        WHERE website IS NULL AND details_fetched_at IS NOT NULL
        ORDER BY name ASC
        """
    ).fetchall()

    path = output_dir / _timestamped_filename("no_website")
    _write_csv(path, [dict(r) for r in rows], ["place_id", "name", "address", "phone", "scan_area"])
    return path


def generate_poor_website_report(
    conn: sqlite3.Connection,
    output_dir: Path,
    score_threshold: int = 40,
) -> Path:
    """Write a CSV of businesses whose latest website check scored below threshold."""
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = conn.execute(
        """
        SELECT b.place_id, b.name, b.address, b.phone, b.website, wc.score, b.scan_area
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

    path = output_dir / _timestamped_filename("poor_website")
    _write_csv(
        path,
        [dict(r) for r in rows],
        ["place_id", "name", "address", "phone", "website", "score", "scan_area"],
    )
    return path


def print_stats(conn: sqlite3.Connection) -> None:
    """Print a rich summary table of database contents to stdout."""
    from rich.console import Console
    from rich.table import Table

    from .db import get_stats

    stats = get_stats(conn)
    console = Console()

    table = Table(title="LocalBusinessScanner — Database Summary", show_header=True)
    table.add_column("Metric", style="cyan", min_width=28)
    table.add_column("Count", justify="right", style="bold")

    table.add_row("Total businesses", str(stats["total_businesses"]))
    table.add_row("Details fetched", str(stats["details_fetched"]))
    table.add_section()
    table.add_row("[green]No website (leads)[/green]", f"[green]{stats['no_website']}[/green]")
    table.add_row("Website present", str(stats["website_present"]))
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
