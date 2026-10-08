"""Shared pytest fixtures for the test suite."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db.base import get_session_factory, init_db, reset_engine_cache
from app.db.models import FeatureMeasurementRecord, FileRecord
from app.main import create_app
from sqlalchemy import delete


@pytest.fixture(scope="session", autouse=True)
def _test_database(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Point the app at a temporary SQLite file for the whole test session.

    Keeps tests from creating (or clobbering) a development database in
    the project root, and resets the settings/engine caches so the new
    URL is picked up.
    """
    previous = os.environ.get("DATABASE_URL")
    db_file = tmp_path_factory.mktemp("db") / "test_app.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_file.as_posix()}"
    get_settings.cache_clear()
    reset_engine_cache()
    yield db_file
    get_settings.cache_clear()
    reset_engine_cache()
    if previous is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = previous


@pytest.fixture(autouse=True)
def _clean_file_records() -> Iterator[None]:
    """Empty the shared tables before every test.

    The database file is session-scoped for speed, so tests must not
    observe each other's rows. ``init_db`` runs first because the schema
    is normally created at app startup, which has not happened yet for
    the very first test.
    """
    init_db()
    factory = get_session_factory()
    with factory() as session:
        session.execute(delete(FeatureMeasurementRecord))
        session.execute(delete(FileRecord))
        session.commit()
    yield


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Return isolated test settings (no reliance on a local ``.env``).

    Uploads are capped at 1 MB so the oversized-file test stays fast,
    and stored files land in a per-test temp directory.
    """
    return Settings(
        _env_file=None,
        app_name="Geo Measure API (test)",
        environment="development",
        debug=False,
        log_level="WARNING",
        max_upload_mb=1,
        storage_dir=str(tmp_path / "uploads"),
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """Provide a TestClient bound to a freshly built application."""
    app = create_app(settings=settings)
    with TestClient(app) as test_client:
        yield test_client
