"""Logging setup for the application.

Configures a single root logger at startup so that every module (routes,
services, geospatial processing) emits consistently formatted records without
per-module boilerplate.
"""

from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    """Configure root logging for the process.

    Idempotent: calling it more than once replaces existing handlers
    instead of duplicating output lines.

    Args:
        level: Logging level name (``DEBUG``, ``INFO``, ``WARNING``,
            ``ERROR`` or ``CRITICAL``). Unknown names fall back to ``INFO``.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Remove any handlers attached by previous calls or by the
    # hosting environment, then attach a single formatted stream handler.
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setLevel(numeric_level)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root_logger.addHandler(handler)
