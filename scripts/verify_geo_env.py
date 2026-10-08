"""Geospatial environment verification CLI.

Usage::

    python scripts/verify_geo_env.py

Exits ``0`` when the environment is fully usable and ``1`` otherwise,
printing a detailed report either way. Suitable for CI gates and for
confirming an installation before starting the service.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running the script directly from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.geo.environment import check_geospatial_environment  # noqa: E402


def main() -> int:
    """Run the verification and print the report.

    Returns:
        Process exit code: ``0`` if usable, ``1`` otherwise.
    """
    report = check_geospatial_environment()
    print(report.summary())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
