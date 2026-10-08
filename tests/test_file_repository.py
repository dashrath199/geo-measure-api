"""Tests for database persistence of :class:`FileRecord` metadata."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import FileRecord, FileStatus
from app.db.repository import FileRepository
from app.exceptions import FileRecordNotFoundError

import app.db.models  # noqa: F401 - registers models on Base.metadata


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Provide an isolated SQLite engine per test."""
    test_engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(bind=test_engine)
    yield test_engine
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Provide a session bound to the isolated engine."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db_session = factory()
    yield db_session
    db_session.close()


@pytest.fixture
def repository(session: Session) -> FileRepository:
    """Provide a repository bound to the test session."""
    return FileRepository(session)


def _create(repository: FileRepository, filename: str = "sample.zip") -> FileRecord:
    """Insert a sample record with sensible defaults.

    Args:
        repository: Repository under test.
        filename: Original filename to record.

    Returns:
        The persisted record.
    """
    return repository.create(
        original_filename=filename,
        stored_path=f"/tmp/uploads/{filename}",
        file_type="shapefile_zip",
    )


def test_create_persists_record_with_defaults(repository: FileRepository) -> None:
    """A new record gets a UUID, UPLOADED status and timestamps."""
    record = _create(repository)

    assert len(record.id) == 36  # UUID4 string form
    assert record.status == FileStatus.UPLOADED.value
    assert record.original_filename == "sample.zip"
    assert record.stored_path == "/tmp/uploads/sample.zip"
    assert record.file_type == "shapefile_zip"
    assert record.created_at is not None
    assert record.updated_at is not None
    # Nullable result columns stay empty until processing.
    assert record.feature_count is None
    assert record.crs is None
    assert record.error_message is None


def test_get_roundtrip(repository: FileRepository) -> None:
    """Records are retrievable by id after a session round-trip."""
    created = _create(repository, "map.kml")

    fetched = repository.get(created.id)

    assert fetched.id == created.id
    assert fetched.original_filename == "map.kml"
    assert fetched.file_type == "shapefile_zip"


def test_get_unknown_id_raises(repository: FileRepository) -> None:
    """Looking up a missing id raises the domain not-found error."""
    with pytest.raises(FileRecordNotFoundError) as excinfo:
        repository.get("00000000-0000-0000-0000-000000000000")

    assert "00000000-0000-0000-0000-000000000000" in str(excinfo.value)


def test_status_transitions_persist(repository: FileRepository) -> None:
    """UPLOADED -> PROCESSING -> COMPLETED flows survive re-fetching."""
    record = _create(repository)

    repository.set_status(record, FileStatus.PROCESSING)
    assert repository.get(record.id).status == "PROCESSING"

    repository.set_status(
        record,
        FileStatus.COMPLETED,
        feature_count=12,
        crs="EPSG:4326",
    )
    fetched = repository.get(record.id)
    assert fetched.status == "COMPLETED"
    assert fetched.feature_count == 12
    assert fetched.crs == "EPSG:4326"
    assert fetched.error_message is None


def test_failed_status_stores_error_message(repository: FileRepository) -> None:
    """FAILURE transitions persist the explanatory message."""
    record = _create(repository)

    repository.set_status(
        record,
        FileStatus.FAILED,
        error_message="corrupt shapefile: missing .shx",
    )
    fetched = repository.get(record.id)

    assert fetched.status == "FAILED"
    assert fetched.error_message == "corrupt shapefile: missing .shx"
    assert fetched.feature_count is None


def test_updated_at_moves_forward(repository: FileRepository) -> None:
    """Updating a record refreshes ``updated_at``."""
    record = _create(repository)
    original_updated = record.updated_at

    repository.set_status(record, FileStatus.PROCESSING)

    assert record.updated_at >= original_updated


def test_list_orders_newest_first(
    repository: FileRepository, session: Session
) -> None:
    """``list`` returns records ordered by creation time, descending.

    Timestamps are set explicitly because coarse clock resolution on
    some platforms could otherwise produce identical ``created_at``
    values for two rapid inserts.
    """
    first = _create(repository, "one.zip")
    second = _create(repository, "two.zip")

    first.created_at = datetime(2026, 1, 1, 12, 0, 0)
    second.created_at = datetime(2026, 1, 2, 12, 0, 0)
    session.flush()

    records = repository.list()

    assert [r.id for r in records] == [second.id, first.id]


def test_table_holds_metadata_only(engine: Engine) -> None:
    """The schema stores no geometry columns — metadata only."""
    columns = {col["name"] for col in inspect(engine).get_columns("files")}

    assert columns == {
        "id",
        "original_filename",
        "stored_path",
        "file_type",
        "status",
        "feature_count",
        "crs",
        "error_message",
        "created_at",
        "updated_at",
    }


def test_status_is_indexed(engine: Engine) -> None:
    """Status lookups are indexed for efficient filtering."""
    indexes = {ix["name"] for ix in inspect(engine).get_indexes("files")}

    assert "ix_files_status" in indexes
