"""
scanner — LocalBusinessScanner package.

Public re-exports for convenience.  Implementors of individual modules
should import directly from the submodule (e.g. ``from scanner.db import Business``).
This file exposes the most-used types at the top level so callers can write
``from scanner import Business, WebsiteCheckResult``.

Milestone: M5-B
"""

from .db import Business, WebsiteCheckResult

__all__ = [
    "Business",
    "WebsiteCheckResult",
]
