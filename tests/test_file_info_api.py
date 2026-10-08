"""API tests for ``GET /api/files/{id}/``.

Covers the documented response shape, id validation (422), unknown ids
(404), clear failure reporting, and the security requirements: no
filesystem paths, no server-internal information.
"""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from app.db.base import get_session_factory
from app.db.models import FileStatus
from app.db.repository import FileRepository

KML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
    "<Placemark><name>p</name>"
    "<Point><coordinates>1,2,0</coordinates></Point>"
    "</Placemark></Document></kml>"
)

ALLOWED_KEYS = {
    "id",
    "filename",
    "file_type",
    "status",
    "feature_count",
    "crs",
    "error_message",
    "created_at",
    "updated_at",
}


def _upload(client: TestClient, filename: str = "survey.kml") -> str:
    """Upload a valid KML and return its id."""
    response = client.post(
        "/api/files/",
        files={"file": (filename, KML.encode(), "application/octet-stream")},
    )
    assert response.status_code == 201
    return response.json()["id"]


# -------------------------------------------------------------- happy path


def test_get_completed_file_returns_expected_shape(
    client: TestClient,
) -> None:
    """The response matches the documented contract plus safe metadata."""
    file_id = _upload(client)

    response = client.get(f"/api/files/{file_id}/")

    assert response.status_code == 200
    body = response.json()
    # Documented fields.
    assert body["id"] == file_id
    assert body["filename"] == "survey.kml"
    assert body["feature_count"] == 1
    assert body["crs"] == "EPSG:4326"
    assert body["status"] == "COMPLETED"
    # Additional safe metadata already available.
    assert body["file_type"] == "kml"
    assert body["error_message"] is None
    assert isinstance(body["created_at"], str)
    assert isinstance(body["updated_at"], str)
    datetime.fromisoformat(body["created_at"])  # parseable


def test_shapefile_file_reports_shapefile_metadata(
    tmp_path, client: TestClient
) -> None:
    """Shapefile uploads report their type and feature count."""
    import io
    import zipfile

    import geopandas as gpd
    from shapely.geometry import box

    build = tmp_path / "build"
    build.mkdir()
    gdf = gpd.GeoDataFrame(
        {"name": ["a"]}, geometry=[box(0, 0, 1, 1)], crs="EPSG:32633"
    )
    gdf.to_file(build / "sample.shp")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for part in build.iterdir():
            archive.write(part, part.name)

    upload = client.post(
        "/api/files/",
        files={"file": ("map.zip", buffer.getvalue(), "application/octet-stream")},
    )
    response = client.get(f"/api/files/{upload.json()['id']}/")

    assert response.status_code == 200
    body = response.json()
    assert body["file_type"] == "shapefile_zip"
    assert body["feature_count"] == 1
    assert body["crs"] == "EPSG:32633"


def test_unprocessed_file_shows_clear_state(client: TestClient) -> None:
    """A record that never finished processing reports null counts."""
    factory = get_session_factory()
    with factory() as session:
        repository = FileRepository(session)
        record = repository.create(
            original_filename="pending.kml",
            stored_path="uploads/pending.kml",
            file_type="kml",
            status=FileStatus.UPLOADED,
        )

    response = client.get(f"/api/files/{record.id}/")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "UPLOADED"
    assert body["feature_count"] is None
    assert body["crs"] is None
    assert body["error_message"] is None


# ---------------------------------------------------------- id validation


def test_unknown_id_returns_404(client: TestClient) -> None:
    """A well-formed but unknown UUID maps to 404 with an error body."""
    response = client.get(
        "/api/files/00000000-0000-4000-8000-000000000000/"
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "FILE_NOT_FOUND"


def test_malformed_id_returns_422(client: TestClient) -> None:
    """Ids that are not UUIDs are rejected as validation errors."""
    response = client.get("/api/files/not-a-valid-id/")

    assert response.status_code == 422


# ------------------------------------------------------ failure reporting


def test_failed_file_reports_failure_clearly(client: TestClient) -> None:
    """FAILED status comes with a sanitized, client-safe message."""
    factory = get_session_factory()
    with factory() as session:
        repository = FileRepository(session)
        record = repository.create(
            original_filename="broken.zip",
            stored_path="uploads/broken.zip",
            file_type="shapefile_zip",
        )
        repository.set_status(
            record,
            FileStatus.FAILED,
            error_message="Shapefile not found: <path>/broken.shp",
        )

    response = client.get(f"/api/files/{record.id}/")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["error_message"] == "Shapefile not found: <path>/broken.shp"
    assert body["feature_count"] is None


def test_internal_paths_never_leak(client: TestClient) -> None:
    """Read-path redaction scrubs paths even from unsanitized writers."""
    factory = get_session_factory()
    with factory() as session:
        repository = FileRepository(session)
        record = repository.create(
            original_filename="leaky.zip",
            stored_path="uploads/leaky.zip",
            file_type="shapefile_zip",
        )
        # Simulate a writer that skipped write-side sanitization.
        repository.set_status(
            record,
            FileStatus.FAILED,
            error_message=(
                "Cannot open C:\\Users\\secret\\appdata\\data\\leaky.shp "
                "and /var/lib/private/storage/leaky.shx"
            ),
        )

    response = client.get(f"/api/files/{record.id}/")

    message = response.json()["error_message"]
    assert "<path>/leaky.shp" in message
    assert "<path>/leaky.shx" in message
    assert "secret" not in message
    assert "private" not in message
    assert "Users" not in message


def test_error_hidden_when_not_failed(client: TestClient) -> None:
    """A stale error message never accompanies a non-FAILED status."""
    factory = get_session_factory()
    with factory() as session:
        repository = FileRepository(session)
        record = repository.create(
            original_filename="healed.kml",
            stored_path="uploads/healed.kml",
            file_type="kml",
        )
        repository.set_status(
            record, FileStatus.FAILED, error_message="old failure"
        )
        repository.set_status(record, FileStatus.COMPLETED, feature_count=1)

    response = client.get(f"/api/files/{record.id}/")

    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["error_message"] is None


# ------------------------------------------------- no server-side details


def test_response_exposes_no_internal_fields(client: TestClient) -> None:
    """The body contains exactly the whitelisted, client-safe fields."""
    file_id = _upload(client)

    body = client.get(f"/api/files/{file_id}/").json()

    assert set(body) == ALLOWED_KEYS
    # The one field that would be a path leak is structurally absent.
    assert "stored_path" not in body
    assert "storage_dir" not in body
    assert "database_url" not in body
