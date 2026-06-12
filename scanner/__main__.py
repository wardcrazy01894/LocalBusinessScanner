"""
scanner/__main__.py — Entry point for ``python -m scanner``.

Allows running the CLI as:
    python -m scanner scan "Highland, UT"
    python -m scanner report
    python -m scanner stats

Milestone: M5-A
"""

from .cli import app

if __name__ == "__main__":
    app()
