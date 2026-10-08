"""Integration tests for the complete upload → processing pipeline.

Covers the twelve pipeline steps end-to-end through the real HTTP
interface: upload, validation, storage, record creation, format
detection, extraction, features, CRS, transformation, measurements,
summary persistence and final status — plus the failure policy (FAILED
+ safe messages, no stack traces for clients, per-feature isolation).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Polygon, box

from app.api import deps
from app.config import Settings
from app.db.base import get_session_factory
from app.db.models import FileRecord, FileStatus
from app.db.repository import FeatureMeasurementRepository, FileRepository
from app.services.processing_service import ProcessingService, ProcessingSummary

# --------------------------------------------------------------- helpers


def _post(client: TestClient, filename: str, content: bytes):
    """Upload bytes under a filename."""
    return client.post(
        "/api/files/",
        files={"file": (filename, content, "application/octet-stream")},
    )


def _shapefile_zip(tmp_path: Path, gdf: gpd.GeoDataFrame) -> bytes:
    """Write a GeoDataFrame as a shapefile and return archive bytes."""
    build = tmp_path / "build"
    build.mkdir(parents=True, exist_ok=True)
    gdf.to_file(build / "sample.shp")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for part in build.iterdir():
            archive.write(part, part.name)
    return buffer.getvalue()


def _record(file_id: str) -> FileRecord:
    """Fetch a file record via a fresh session."""
    factory = get_session_factory()
    with factory() as session:
        return FileRepository(session).get(file_id)


def _measurements(file_id: str) -> list:
    """Fetch persisted measurement rows for a file."""
    factory = get_session_factory()
    with factory() as session:
        return FeatureMeasurementRepository(session).list_for_file(file_id)


KML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
    "<Placemark><name>p</name>"
    "<Point><coordinates>1,2,0</coordinates></Point>"
    "</Placemark></Document></kml>"
)


# ------------------------------------------------ happy-path: complete flow


def test_complete_flow_shapefile_zip(tmp_path: Path, client: TestClient) -> None:
    """Zip upload runs all 12 steps to COMPLETED with exact measurements."""
    gdf = gpd.GeoDataFrame(
        {"name": ["ok", "bad"]},
        # Valid square (100 m2) + self-intersecting bowtie (invalid).
        geometry=[box(0, 0, 10, 10), Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])],
        crs="EPSG:32633",
    )

    response = _post(client, "survey.zip", _shapefile_zip(tmp_path, gdf))

    # Upload contract: acceptance-stage status in the response body.
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "filename", "status"}
    assert body["status"] == "PROCESSING"

    # Pipeline completed within the request: summary persisted.
    record = _record(body["id"])
    assert record.status == "COMPLETED"
    assert record.feature_count == 2
    assert record.crs == "EPSG:32633"
    assert record.error_message is None

    rows = _measurements(body["id"])
    assert len(rows) == 2
    supported, invalid = rows[0], rows[1]
    # Step 10-11: exact measurement in the projected CRS, persisted.
    assert supported.feature_index == 0
    assert supported.category == "SUPPORTED"
    assert supported.measurement_kind == "area"
    assert supported.measurement_value == pytest.approx(100.0, rel=1e-9)
    assert supported.unit == "square_meters"
    assert supported.measurement_crs == "EPSG:32633"
    assert supported.geometry_type == "Polygon"
    # One invalid feature never fails the file.
    assert invalid.feature_index == 1
    assert invalid.category == "INVALID"
    assert invalid.measurement_value is None


def test_complete_flow_kml(tmp_path: Path, client: TestClient) -> None:
    """KML upload completes with CRS recorded and point left unmeasured."""
    response = _post(client, "survey.kml", KML.encode())

    assert response.status_code == 201
    record = _record(response.json()["id"])

    assert record.status == "COMPLETED"
    assert record.file_type == "kml"
    assert record.feature_count == 1
    assert record.crs == "EPSG:4326"  # detected, not assumed silently

    rows = _measurements(record.id)
    assert len(rows) == 1
    assert rows[0].category == "NO_MEASUREMENT"
    assert rows[0].status == "not_required"
    assert rows[0].geometry_type == "Point"
    assert rows[0].measurement_value is None


def test_complete_flow_shapefile_without_prj(
    tmp_path: Path, client: TestClient
) -> None:
    """Missing .prj still completes the file; measurements skip per-feature."""
    gdf = gpd.GeoDataFrame(
        {"name": ["p"]},
        geometry=[box(0, 0, 1, 1)],
        crs=None,
    )

    response = _post(client, "noprj.zip", _shapefile_zip(tmp_path, gdf))
    record = _record(response.json()["id"])

    # The file processed fine; per-feature no-CRS skips are not a failure.
    assert record.status == "COMPLETED"
    assert record.crs is None

    rows = _measurements(record.id)
    assert rows[0].status == "skipped_no_crs"
    assert rows[0].category == "NO_MEASUREMENT"


def test_upload_response_contract_is_preserved(
    client: TestClient,
) -> None:
    """Response reports acceptance stage; DB holds the final state."""
    response = _post(client, "contract.kml", KML.encode())
    body = response.json()

    assert body["status"] == "PROCESSING"  # Phase 4 contract intact
    assert _record(body["id"]).status == "COMPLETED"  # pipeline ran


# ------------------------------------------------------- failure handling


def test_processing_failure_marks_failed_with_safe_message(
    tmp_path: Path,
) -> None:
    """An unreadable stored file fails safely: FAILED, no internals."""
    factory = get_session_factory()
    with factory() as session:
        repository = FileRepository(session)
        record = repository.create(
            original_filename="gone.zip",
            stored_path=str(tmp_path / "gone.zip"),  # does not exist
            file_type="shapefile_zip",
        )
        service = ProcessingService(session=session, settings=_test_settings())

        summary = service.process(record.id)

        assert summary.status == FileStatus.FAILED
        refreshed = repository.get(record.id)
        assert refreshed.status == "FAILED"
        message = refreshed.error_message
        assert message
        # Safe: no stack traces, no exception internals, no server paths.
        assert "Traceback" not in message
        assert "site-packages" not in message
        assert "pytest-of" not in message
        assert "gone.zip" in message  # filename kept — useful context


def test_unexpected_error_stores_generic_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unexpected exceptions: generic client message, full logs kept."""
    factory = get_session_factory()
    with factory() as session:
        repository = FileRepository(session)
        record = repository.create(
            original_filename="boom.kml",
            stored_path=str(tmp_path / "boom.kml"),
            file_type="kml",
        )

        def explode(self, rec):
            raise RuntimeError("SECRET_INTERNAL_DETAIL_42")

        monkeypatch.setattr(
            ProcessingService, "_read_geospatial", explode
        )
        service = ProcessingService(session=session, settings=_test_settings())

        summary = service.process(record.id)

        assert summary.status == FileStatus.FAILED
        refreshed = repository.get(record.id)
        assert refreshed.status == "FAILED"
        # The internal exception text must not reach the API layer.
        assert refreshed.error_message == "Unexpected error during processing"
        assert "SECRET_INTERNAL_DETAIL_42" not in refreshed.error_message
        assert "RuntimeError" not in refreshed.error_message


def test_upload_returns_failed_status_when_pipeline_fails(
    settings: Settings,
) -> None:
    """A failing pipeline surfaces FAILED in the upload response body."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    def failing_process(file_id: str) -> ProcessingSummary:
        return ProcessingSummary(
            file_id=file_id,
            status=FileStatus.FAILED,
            error_message="safe",
        )

    stub = type(
        "StubProcessing",
        (),
        {"process": lambda self, file_id: failing_process(file_id)},
    )()
    app = create_app(settings=settings)
    app.dependency_overrides[deps.get_processing_service] = lambda: stub
    try:
        with TestClient(app) as failing_client:
            response = failing_client.post(
                "/api/files/",
                files={"file": ("bad.kml", KML.encode(), "application/octet-stream")},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json()["status"] == "FAILED"


def _test_settings() -> Settings:
    """Build settings suitable for pipeline tests."""
    return Settings(
        _env_file=None,
        log_level="WARNING",
        max_upload_mb=1,
        storage_dir="uploads",
    )
