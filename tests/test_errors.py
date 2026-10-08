"""Tests for the API-wide error handling contract (Phase 15).

Every error response — domain, validation, routing or unexpected —
uses one JSON envelope::

    {"error": {"code": "UNSUPPORTED_FILE_TYPE", "message": "..."}}

``code`` is machine-readable and stable; ``message`` is client-safe
(redacted of server paths, never a stack trace). Covers all required
scenarios: unsupported file type, malformed KML, malformed ZIP, invalid
Shapefile, missing Shapefile components, missing CRS, invalid CRS,
invalid geometry, empty geometry, unsupported geometry, file not found,
processing failure, oversized upload and storage failure — plus
routing errors (404/405/422) and the 500 catch-all (generic response,
full traceback logged).
"""

from __future__ import annotations

import io
import logging
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.config import Settings
from app.db.base import get_session_factory
from app.db.models import FileRecord
from app.exceptions import (
    CRSError,
    DatasetTooLargeError,
    EmptyGeometryError,
    FileRecordNotFoundError,
    FileTooLargeError,
    GeoAppError,
    GeospatialDependencyError,
    GeometryProcessingError,
    InvalidArchiveError,
    InvalidCRSError,
    InvalidFileError,
    InvalidGeometryError,
    MissingCRSError,
    StorageError,
    UnsupportedFormatError,
    UnsupportedGeometryError,
    redact_paths,
)
from app.main import create_app
from sqlalchemy import select

# ----------------------------------------------------------------- helpers

KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document><Placemark><Point><coordinates>0,0</coordinates></Point></Placemark></Document>
</kml>
"""


def _assert_envelope(
    response: Any, status_code: int, code: str
) -> dict[str, str]:
    """Assert the standard error envelope and return the error object.

    Args:
        response: TestClient response.
        status_code: Expected HTTP status.
        code: Expected machine-readable error code.

    Returns:
        The inner ``{"code", "message"}`` object.
    """
    assert response.status_code == status_code
    body = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message"}, error
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    # Clients never see stack traces.
    assert "Traceback" not in error["message"]
    return error


def _post(
    client: TestClient, filename: str, content: bytes
) -> object:
    """Upload bytes under a filename via ``POST /api/files/``.

    Args:
        client: Test client.
        filename: Name to send.
        content: File bytes.

    Returns:
        The HTTP response.
    """
    return client.post(
        "/api/files/",
        files={"file": (filename, content, "application/octet-stream")},
    )


def _zip_with_members(members: dict[str, bytes]) -> bytes:
    """Build an in-memory ZIP archive.

    Args:
        members: Member name → content.

    Returns:
        The archive as bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _fake_shp_zip(*, with_shx: bool = True) -> bytes:
    """Build a structurally valid archive whose dataset is garbage.

    Args:
        with_shx: Whether to include the ``.shx`` component.

    Returns:
        The archive as bytes.
    """
    members = {
        "sample.shp": b"this is not a shapefile",
        "sample.dbf": b"nor is this a dbf",
    }
    if with_shx:
        members["sample.shx"] = b"nor is this an shx"
    return _zip_with_members(members)


def _traversal_zip() -> bytes:
    """Build an otherwise valid archive carrying a ``..`` member.

    Returns:
        The archive as bytes.
    """
    members = _fake_shp_zip_members()
    members["../../evil.shp"] = b"evil payload"
    return _zip_with_members(members)


def _fake_shp_zip_members() -> dict[str, bytes]:
    """Return the member map of :func:`_fake_shp_zip`.

    Returns:
        Member name → content for a structurally complete fake dataset.
    """
    return {
        "sample.shp": b"this is not a shapefile",
        "sample.shx": b"nor is this an shx",
        "sample.dbf": b"nor is this a dbf",
    }


def _all_records() -> list[FileRecord]:
    """Fetch every file record via a fresh session."""
    factory = get_session_factory()
    with factory() as session:
        return list(session.scalars(select(FileRecord)))


# ------------------------------------------------------- upload-time errors


def test_unsupported_file_type(client: TestClient) -> None:
    """Non-geospatial extensions → 415 with ``UNSUPPORTED_FILE_TYPE``."""
    error = _assert_envelope(
        _post(client, "notes.txt", b"hello world"),
        415,
        "UNSUPPORTED_FILE_TYPE",
    )
    assert ".txt" in error["message"]


def test_malformed_kml(client: TestClient) -> None:
    """KML content without a ``<kml`` root → 400 ``MALFORMED_KML``."""
    error = _assert_envelope(
        _post(client, "broken.kml", b"<not-really-kml"),
        400,
        "MALFORMED_KML",
    )
    assert "KML" in error["message"]


def test_malformed_zip(client: TestClient) -> None:
    """Corrupt archives → 400 ``MALFORMED_ZIP``; nothing stored."""
    error = _assert_envelope(
        _post(client, "broken.zip", b"PK\x03\x04" + b"garbage" * 50),
        400,
        "MALFORMED_ZIP",
    )
    assert "ZIP" in error["message"]
    assert _all_records() == []


def test_unsafe_archive(client: TestClient) -> None:
    """Traversal members → 400 ``UNSAFE_ARCHIVE``; nothing stored."""
    error = _assert_envelope(
        _post(client, "evil.zip", _traversal_zip()),
        400,
        "UNSAFE_ARCHIVE",
    )
    assert "traversal" in error["message"]
    assert _all_records() == []


def test_missing_shapefile_components(client: TestClient) -> None:
    """Archives lacking ``.shx`` → 400 ``MISSING_SHAPEFILE_COMPONENT``."""
    error = _assert_envelope(
        _post(client, "incomplete.zip", _fake_shp_zip(with_shx=False)),
        400,
        "MISSING_SHAPEFILE_COMPONENT",
    )
    assert ".shx" in error["message"]
    assert _all_records() == []


def test_invalid_shapefile_archive(client: TestClient) -> None:
    """ZIPs without any ``.shp`` → 400 ``INVALID_SHAPEFILE``."""
    error = _assert_envelope(
        _post(client, "notashape.zip", _zip_with_members({"readme.txt": b"hi"})),
        400,
        "INVALID_SHAPEFILE",
    )
    assert _all_records() == []


def test_oversized_upload(
    client: TestClient, settings: Settings
) -> None:
    """Uploads above ``max_upload_mb`` → 413 ``FILE_TOO_LARGE``."""
    payload = KML + b"a" * (settings.max_upload_mb * 1024 * 1024 + 1)
    error = _assert_envelope(
        _post(client, "huge.kml", payload), 413, "FILE_TOO_LARGE"
    )
    assert str(settings.max_upload_mb) in error["message"]
    assert _all_records() == []


def test_file_not_found(client: TestClient) -> None:
    """Unknown (well-formed) ids → 404 ``FILE_NOT_FOUND``."""
    _assert_envelope(
        client.get("/api/files/00000000-0000-4000-8000-000000000000/"),
        404,
        "FILE_NOT_FOUND",
    )


# ------------------------------------------------------------ routing errors


def test_unknown_route(client: TestClient) -> None:
    """Unknown paths → 404 ``NOT_FOUND`` in the standard envelope."""
    _assert_envelope(client.get("/does-not-exist"), 404, "NOT_FOUND")


def test_method_not_allowed(client: TestClient) -> None:
    """Wrong verbs → 405 ``METHOD_NOT_ALLOWED`` in the envelope."""
    _assert_envelope(client.get("/api/files/"), 405, "METHOD_NOT_ALLOWED")


def test_malformed_id_is_validation_error(client: TestClient) -> None:
    """Non-UUID path parameters → 422 ``VALIDATION_ERROR`` (envelope)."""
    error = _assert_envelope(
        client.get("/api/files/not-a-uuid/"), 422, "VALIDATION_ERROR"
    )
    # Actionable, but echoing only field locations and rule messages.
    assert "file_id" in error["message"]


def test_missing_file_part_is_validation_error(client: TestClient) -> None:
    """Uploads without a file part → 422 ``VALIDATION_ERROR``."""
    _assert_envelope(client.post("/api/files/"), 422, "VALIDATION_ERROR")


# ------------------------------------- domain error mapping (HTTP, table)


def _domain_error_cases() -> list[
    tuple[str, Callable[[], Exception], int, str]
]:
    """Build ``(id, factory, status, code)`` cases for every domain error.

    Returns:
        One case per domain exception / scenario code combination.
    """
    return [
        ("unsupported-format", lambda: UnsupportedFormatError("x"), 415,
         "UNSUPPORTED_FILE_TYPE"),
        ("invalid-file", lambda: InvalidFileError("x"), 400, "INVALID_FILE"),
        ("malformed-kml",
         lambda: InvalidFileError("x", code="MALFORMED_KML"), 400,
         "MALFORMED_KML"),
        ("unsafe-content",
         lambda: InvalidFileError("x", code="UNSAFE_CONTENT"), 400,
         "UNSAFE_CONTENT"),
        ("malformed-zip", lambda: InvalidArchiveError("x"), 400,
         "MALFORMED_ZIP"),
        ("unsafe-archive",
         lambda: InvalidArchiveError("x", code="UNSAFE_ARCHIVE"), 400,
         "UNSAFE_ARCHIVE"),
        ("missing-component",
         lambda: InvalidArchiveError(
             "x", code="MISSING_SHAPEFILE_COMPONENT"
         ), 400, "MISSING_SHAPEFILE_COMPONENT"),
        ("invalid-shapefile",
         lambda: InvalidArchiveError("x", code="INVALID_SHAPEFILE"), 400,
         "INVALID_SHAPEFILE"),
        ("file-too-large", lambda: FileTooLargeError(1024), 413,
         "FILE_TOO_LARGE"),
        ("storage-failure", lambda: StorageError("x"), 500,
         "STORAGE_FAILURE"),
        ("file-not-found", lambda: FileRecordNotFoundError("id"), 404,
         "FILE_NOT_FOUND"),
        ("missing-crs", lambda: MissingCRSError("x"), 400, "MISSING_CRS"),
        ("invalid-crs", lambda: InvalidCRSError("x"), 400, "INVALID_CRS"),
        ("empty-geometry", lambda: EmptyGeometryError("x"), 400,
         "EMPTY_GEOMETRY"),
        ("processing-failure", lambda: GeometryProcessingError("x"), 400,
         "PROCESSING_FAILURE"),
        ("invalid-geometry", lambda: InvalidGeometryError("x"), 400,
         "INVALID_GEOMETRY"),
        ("unsupported-geometry", lambda: UnsupportedGeometryError("x"), 400,
         "UNSUPPORTED_GEOMETRY"),
        ("geospatial-unavailable", lambda: GeospatialDependencyError("x"),
         503, "GEOSPATIAL_UNAVAILABLE"),
        ("too-many-features", lambda: DatasetTooLargeError("x"), 400,
         "TOO_MANY_FEATURES"),
    ]


def test_every_domain_error_maps_to_envelope(settings: Settings) -> None:
    """Each domain exception → its status and code in the envelope.

    Exercises the real exception handlers via throwaway routes; a new
    domain error that forgets ``code``/``status_code`` fails here.
    """
    app = create_app(settings=settings)
    cases = _domain_error_cases()
    for index, (_case_id, factory, _status, _code) in enumerate(cases):
        def endpoint(f: Callable[[], Exception] = factory) -> None:
            raise f()

        app.add_api_route(f"/_raise/{index}", endpoint, methods=["GET"])

    with TestClient(app) as table_client:
        for index, (case_id, _factory, status, code) in enumerate(cases):
            response = table_client.get(f"/_raise/{index}")
            try:
                _assert_envelope(response, status, code)
            except AssertionError as exc:  # pragma: no cover - failure path
                raise AssertionError(f"case {case_id!r}: {exc}") from exc


def test_domain_error_messages_are_redacted(settings: Settings) -> None:
    """Messages are path-redacted on the read side, whatever the writer."""
    app = create_app(settings=settings)

    def leak() -> None:
        raise InvalidFileError("Cannot read /srv/geo/secret.kml")

    app.add_api_route("/_leak", leak, methods=["GET"])

    with TestClient(app) as redaction_client:
        error = _assert_envelope(
            redaction_client.get("/_leak"), 400, "INVALID_FILE"
        )
    assert "<path>/secret.kml" in error["message"]
    assert "/srv/geo" not in error["message"]
    # Redaction is idempotent (writers already redact too).
    assert redact_paths("<path>/secret.kml") == "<path>/secret.kml"


def test_exception_taxonomy() -> None:
    """Codes, statuses and hierarchy guarantees of the exception tree."""
    for exception, code, status in [
        (UnsupportedFormatError("x"), "UNSUPPORTED_FILE_TYPE", 415),
        (InvalidFileError("x"), "INVALID_FILE", 400),
        (InvalidArchiveError("x"), "MALFORMED_ZIP", 400),
        (FileTooLargeError(1024), "FILE_TOO_LARGE", 413),
        (StorageError("x"), "STORAGE_FAILURE", 500),
        (FileRecordNotFoundError("id"), "FILE_NOT_FOUND", 404),
        (MissingCRSError("x"), "MISSING_CRS", 400),
        (InvalidCRSError("x"), "INVALID_CRS", 400),
        (EmptyGeometryError("x"), "EMPTY_GEOMETRY", 400),
        (GeometryProcessingError("x"), "PROCESSING_FAILURE", 400),
        (InvalidGeometryError("x"), "INVALID_GEOMETRY", 400),
        (UnsupportedGeometryError("x"), "UNSUPPORTED_GEOMETRY", 400),
        (GeospatialDependencyError("x"), "GEOSPATIAL_UNAVAILABLE", 503),
        (DatasetTooLargeError("x"), "TOO_MANY_FEATURES", 400),
        (GeoAppError("x"), "INTERNAL_ERROR", 400),
    ]:
        assert exception.code == code
        assert exception.status_code == status
        assert isinstance(exception, GeoAppError)

    # Existing catch sites keep working: subclasses stay in-family.
    assert issubclass(EmptyGeometryError, CRSError)
    assert issubclass(InvalidGeometryError, GeometryProcessingError)
    assert issubclass(UnsupportedGeometryError, GeometryProcessingError)
    # Per-instance code override for classes covering several scenarios.
    assert InvalidFileError("x", code="MALFORMED_KML").code == "MALFORMED_KML"


# ------------------------------------------------------- storage failure


def test_storage_failure_returns_500(tmp_path: Path) -> None:
    """A broken storage root → 500 ``STORAGE_FAILURE``, nothing stored."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_bytes(b"occupied")
    settings = Settings(
        _env_file=None,
        environment="development",
        debug=False,
        log_level="WARNING",
        max_upload_mb=1,
        storage_dir=str(blocker),
    )
    app = create_app(settings=settings)

    with TestClient(app) as storage_client:
        response = _post(storage_client, "survey.kml", KML)

    error = _assert_envelope(response, 500, "STORAGE_FAILURE")
    # Server paths never leak, even in storage errors.
    assert str(tmp_path) not in error["message"]
    assert "<path>/" in error["message"]
    assert _all_records() == []


# ------------------------------------------------------- pipeline failures


def test_invalid_shapefile_fails_processing_safely(
    client: TestClient,
) -> None:
    """An unreadable dataset fails *processing*: 201 + FAILED status.

    Pipeline failures are not HTTP errors (acceptance already
    happened); the stored record carries a safe, redacted message.
    """
    response = _post(client, "bad.zip", _fake_shp_zip())

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "FAILED"

    info = client.get(f"/api/files/{body['id']}/")
    assert info.status_code == 200
    record = info.json()
    assert record["status"] == "FAILED"
    message = record["error_message"]
    assert message
    assert "Traceback" not in message
    assert "site-packages" not in message


# ------------------------------------------------------- 500 catch-all


class _LogCollector(logging.Handler):
    """Root-logger handler that stores formatted records in memory."""

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        self.entries.append(self.format(record))


def test_unhandled_exception_returns_generic_envelope_and_logs(
    settings: Settings,
) -> None:
    """Unexpected exceptions → 500 ``INTERNAL_ERROR`` for the client
    and a full traceback in the server log."""
    app = create_app(settings=settings)

    def boom() -> None:
        raise RuntimeError("SECRET_INTERNAL_DETAIL_77")

    app.add_api_route("/_boom", boom, methods=["GET"])

    # ``configure_logging`` replaces root handlers when the app is
    # built, so capture must attach afterwards.
    collector = _LogCollector()
    root = logging.getLogger()
    root.addHandler(collector)
    try:
        with TestClient(app, raise_server_exceptions=False) as boom_client:
            response = boom_client.get("/_boom")
    finally:
        root.removeHandler(collector)

    error = _assert_envelope(response, 500, "INTERNAL_ERROR")
    assert error["message"] == (
        "An unexpected error occurred. Please try again later."
    )
    assert "SECRET_INTERNAL_DETAIL_77" not in error["message"]
    assert "RuntimeError" not in error["message"]

    # Operators get the whole story: exception type, message, traceback.
    logged = "\n".join(collector.entries)
    assert "Unhandled exception for GET /_boom" in logged
    assert "SECRET_INTERNAL_DETAIL_77" in logged
    assert "Traceback" in logged
