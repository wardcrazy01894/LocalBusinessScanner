"""
scanner/checker.py — Async website quality checker.

Returns results to cli.py; never writes to the database directly.

Scoring (sum = 100):
    Reachable (HTTP 200-3xx)   35 pts
    Has SSL                    20 pts
    Mobile viewport meta tag   20 pts
    Load time < 2 000 ms       15 pts
    Has <title> with content    5 pts
    Has <meta description>      5 pts

Score < 40 = poor website lead.

Milestone: M2-A
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Sequence

import httpx
from bs4 import BeautifulSoup

from .config import Config
from .db import Business, WebsiteCheckResult

SCORE_REACHABLE: int = 35
SCORE_SSL: int = 20
SCORE_VIEWPORT: int = 20
SCORE_LOAD_TIME: int = 15
SCORE_TITLE: int = 5
SCORE_META_DESC: int = 5
LOAD_TIME_THRESHOLD_MS: int = 2_000
POOR_WEBSITE_THRESHOLD: int = 40

_USER_AGENT = "LocalBusinessScanner/0.1"

# Domains that are social media / directory pages, not real business websites.
# A business "website" pointing here means they have no real site of their own.
_SOCIAL_DOMAINS: frozenset[str] = frozenset({
    "facebook.com", "www.facebook.com", "m.facebook.com",
    "instagram.com", "www.instagram.com",
    "twitter.com", "www.twitter.com", "x.com", "www.x.com",
    "tiktok.com", "www.tiktok.com",
    "yelp.com", "www.yelp.com",
    "tripadvisor.com", "www.tripadvisor.com",
    "google.com", "www.google.com", "maps.google.com",
    "maps.app.goo.gl",
    "goo.gl",
    "linktr.ee",
    "linktree.com",
    "nextdoor.com", "www.nextdoor.com",
    "foursquare.com", "www.foursquare.com",
    "yellowpages.com", "www.yellowpages.com",
    "bbb.org", "www.bbb.org",
    "angieslist.com", "www.angieslist.com",
    "houzz.com", "www.houzz.com",
    "thumbtack.com", "www.thumbtack.com",
})


def is_social_url(url: str) -> bool:
    """Return True if the URL points to a social media or directory page.

    These are not real business websites — the business has no site of their own.
    """
    try:
        from urllib.parse import urlparse
        host = urlparse(url.lower()).netloc.lstrip("www.")
        return host in {d.lstrip("www.") for d in _SOCIAL_DOMAINS}
    except Exception:
        return False


async def run_checks(
    cfg: Config,
    businesses: Sequence[Business],
) -> list[WebsiteCheckResult]:
    """Check all business websites concurrently and return results.

    Skips businesses with no website. DB writes are the caller's responsibility.
    """
    to_check = [b for b in businesses if b.website]
    if not to_check:
        return []

    semaphore = asyncio.Semaphore(cfg.check_concurrency)

    async with httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=5,
        timeout=cfg.website_check_timeout_s,
        headers={"User-Agent": _USER_AGENT},
        verify=True,
    ) as client:
        tasks = [
            check_website(b.website, b.place_id, cfg.website_check_timeout_s, semaphore, client)
            for b in to_check
        ]
        return list(await asyncio.gather(*tasks))


async def check_website(
    url: str,
    place_id: str,
    timeout_s: float,
    semaphore: asyncio.Semaphore,
    client: httpx.AsyncClient,
) -> WebsiteCheckResult:
    """Check one URL and return its quality metrics.

    Step 0: Normalize URL — prepend https:// if no scheme present.
    Steps 1-7: Acquire semaphore, GET, parse HTML, extract metrics, score.
    """
    # Step 0: normalize
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Step 0a: check if URL is a social media / directory page before fetching
    social = is_social_url(url)
    if social:
        return WebsiteCheckResult(
            place_id=place_id,
            checked_at=datetime.utcnow(),
            reachable=True,   # it exists, just not a real website
            has_ssl=url.startswith("https://"),
            has_viewport=False,
            load_time_ms=None,
            has_title=False,
            has_meta_desc=False,
            score=0,          # treated same as no website for lead purposes
            http_status=None,
            error_msg="Social/directory page — not a real website",
            is_social=True,
        )

    async with semaphore:
        t0 = time.monotonic()
        try:
            resp = await client.get(url)
            load_ms = int((time.monotonic() - t0) * 1000)

            final_url = str(resp.url)
            has_ssl = final_url.startswith("https://")
            reachable = 200 <= resp.status_code < 400

            # Check if we were redirected to a social page
            final_social = is_social_url(final_url)
            if final_social:
                return WebsiteCheckResult(
                    place_id=place_id,
                    checked_at=datetime.utcnow(),
                    reachable=True,
                    has_ssl=has_ssl,
                    has_viewport=False,
                    load_time_ms=load_ms,
                    has_title=False,
                    has_meta_desc=False,
                    score=0,
                    http_status=resp.status_code,
                    error_msg=f"Redirects to social/directory page ({final_url})",
                    is_social=True,
                )

            metrics = _extract_metrics(resp.text) if reachable else {}

            score = compute_score(
                reachable=reachable,
                has_ssl=has_ssl,
                has_viewport=metrics.get("has_viewport", False),
                load_time_ms=load_ms,
                has_title=metrics.get("has_title", False),
                has_meta_desc=metrics.get("has_meta_desc", False),
            )

            return WebsiteCheckResult(
                place_id=place_id,
                checked_at=datetime.utcnow(),
                reachable=reachable,
                has_ssl=has_ssl,
                has_viewport=metrics.get("has_viewport", False),
                load_time_ms=load_ms,
                has_title=metrics.get("has_title", False),
                has_meta_desc=metrics.get("has_meta_desc", False),
                score=score,
                http_status=resp.status_code,
                error_msg=None,
                is_social=False,
            )

        except httpx.ConnectError as exc:
            return _error_result(place_id, f"ConnectError: {exc}")
        except httpx.TimeoutException:
            return _error_result(place_id, "Timeout")
        except Exception as exc:
            return _error_result(place_id, str(exc))


def _extract_metrics(html: str) -> dict[str, bool]:
    """Parse HTML and return boolean quality flags."""
    try:
        soup = BeautifulSoup(html, "lxml")

        viewport = soup.find("meta", attrs={"name": lambda x: x and x.lower() == "viewport"})
        title = soup.find("title")
        meta_desc = soup.find("meta", attrs={"name": lambda x: x and x.lower() == "description"})

        return {
            "has_viewport": viewport is not None,
            "has_title": bool(title and title.string and title.string.strip()),
            "has_meta_desc": bool(meta_desc and meta_desc.get("content", "").strip()),
        }
    except Exception:
        return {"has_viewport": False, "has_title": False, "has_meta_desc": False}


def compute_score(
    reachable: bool,
    has_ssl: bool,
    has_viewport: bool,
    load_time_ms: int | None,
    has_title: bool,
    has_meta_desc: bool,
) -> int:
    """Compute the 0-100 website quality score."""
    if not reachable:
        return 0
    score = SCORE_REACHABLE
    if has_ssl:
        score += SCORE_SSL
    if has_viewport:
        score += SCORE_VIEWPORT
    if load_time_ms is not None and load_time_ms < LOAD_TIME_THRESHOLD_MS:
        score += SCORE_LOAD_TIME
    if has_title:
        score += SCORE_TITLE
    if has_meta_desc:
        score += SCORE_META_DESC
    return min(score, 100)


def is_poor_website(score: int) -> bool:
    return score < POOR_WEBSITE_THRESHOLD


def _error_result(place_id: str, error_msg: str) -> WebsiteCheckResult:
    return WebsiteCheckResult(
        place_id=place_id,
        checked_at=datetime.utcnow(),
        reachable=False,
        has_ssl=False,
        has_viewport=False,
        load_time_ms=None,
        has_title=False,
        has_meta_desc=False,
        score=0,
        http_status=None,
        error_msg=error_msg,
    )
