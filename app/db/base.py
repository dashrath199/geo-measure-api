"""Database engine, session management and initialization.

SQLite-focused defaults with generic SQLAlchemy plumbing: the engine and
session factory are created lazily from :class:`~app.config.Settings` and
cached per process, so configuration stays in one place.

Only metadata lives in this database — geometry objects are never stored;
they are processed on upload and their summaries persisted.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""


@lru_cache
def get_engine() -> Engine:
    """Return the process-wide cached SQLAlchemy engine.

    SQLite connections are created with ``check_same_thread=False`` so the
    engine can be shared across FastAPI's worker threads.

    Returns:
        The cached engine built from ``settings.database_url``.
    """
    settings = get_settings()
    connect_args: dict[str, object] = {}
    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(settings.database_url, connect_args=connect_args)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """Return the process-wide cached session factory.

    Returns:
        A ``sessionmaker`` bound to :func:`get_engine`.
    """
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """Provide a scoped database session as a FastAPI dependency.

    The session is closed when the request finishes, regardless of outcome.

    Yields:
        An active :class:`Session`.
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def init_db(engine: Engine | None = None) -> None:
    """Create database tables for all registered models (idempotent).

    Args:
        engine: Engine to initialize; the cached application engine is
            used when omitted.
    """
    target = engine or get_engine()
    # Importing the models module registers them on ``Base.metadata``.
    from app.db import models  # noqa: F401

    Base.metadata.create_all(bind=target)
    _ensure_added_columns(target)
    logger.info("Database initialized (%s)", target.url.render_as_string(hide_password=True))


def _ensure_added_columns(engine: Engine) -> None:
    """Apply additive column migrations for pre-existing SQLite tables.

    ``create_all`` never alters existing tables, so columns introduced
    after a table first shipped are added here (idempotent, SQLite only
    — fresh databases already contain them via ``create_all``).

    Args:
        engine: The initialized engine.
    """
    if engine.dialect.name != "sqlite":
        return
    additions = {
        "feature_measurements": {
            "properties_json": "TEXT",
        },
    }
    with engine.begin() as conn:
        for table, columns in additions.items():
            existing = {
                row[1]
                for row in conn.execute(
                    text(f"PRAGMA table_info({table})")
                )
            }
            if not existing:  # table absent → create_all just made it
                continue
            for column, ddl_type in columns.items():
                if column not in existing:
                    conn.execute(
                        text(
                            f"ALTER TABLE {table} "
                            f"ADD COLUMN {column} {ddl_type}"
                        )
                    )
                    logger.info(
                        "Migrated %s: added column %s", table, column
                    )


def reset_engine_cache() -> None:
    """Clear cached engine/session factories (used by tests)."""
    get_session_factory.cache_clear()
    get_engine.cache_clear()
