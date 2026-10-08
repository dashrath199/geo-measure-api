"""Database initialization CLI.

Usage::

    python scripts/init_db.py

Creates all tables in the configured ``DATABASE_URL`` (idempotent).
Normally unnecessary: the application creates tables automatically at
startup — this script exists for provisioning/CI and one-off resets.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running the script directly from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.base import get_engine, init_db  # noqa: E402


def main() -> int:
    """Initialize the database and report the target URL.

    Returns:
        Process exit code: ``0`` on success, ``1`` on failure.
    """
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports and exits
        print(f"Database initialization FAILED: {exc}", file=sys.stderr)
        return 1
    url = get_engine().url.render_as_string(hide_password=True)
    print(f"Database initialized: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
