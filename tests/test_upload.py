"""Tests for ``POST /api/files/`` (upload endpoint).

Covers every required scenario: valid KML, valid shapefile ZIP,
unsupported extension, malformed ZIP, dangerous ZIP paths, oversized
files, duplicate filenames and missing shapefile components — plus
filename-traversal sanitization and optional ``.prj`` handling.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Point
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.base import get_session_factory
from app.db.models import FileRecord
from app.main import create_app

# ----------------------------------------------------------------- fixtures

KML_CONTENT = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark><name>sample</name><Point><coordinates>0,0,0</coordinates></Point></Placemark>
  </Document>
</kml>
"""


def _build_shapefile_zip(target: Path, *, with_crs: bool = True) -> bytes:
    """Create a real shapefile and zip it.

    Args:
        target: Directory to build the shapefile in.
        with_crs: Whether the layer carries a CRS (writes ``.prj``).

    Returns:
        The archive as bytes.
    """
    crs = "EPSG:4326" if with_crs else None
    target.mkdir(parents=True, exist_ok=True)
    gdf = gpd.GeoDataFrame({"name": ["a"]}, geometry=[Point(0, 0)], crs=crs)
    gdf.to_file(target / "sample.shp")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for part in target.iterdir():
            archive.write(part, part.name)
    return buffer.getvalue()


@pytest.fixture
def kml_bytes() -> bytes:
    """Valid minimal KML content."""
    return KML_CONTENT


@pytest.fixture
def shapefile_zip_bytes(tmp_path: Path) -> bytes:
    """Valid shapefile ZIP including ``.prj``."""
    return _build_shapefile_zip(tmp_path / "shp_full")


@pytest.fixture
def shapefile_zip_no_prj_bytes(tmp_path: Path) -> bytes:
    """Shapefile ZIP without a ``.prj`` sidecar (must still be accepted)."""
    return _build_shapefile_zip(tmp_path / "shp_noprj", with_crs=False)


@pytest.fixture
def missing_component_zip_bytes(tmp_path: Path) -> bytes:
    """ZIP missing the required ``.shx`` component."""
    data = _build_shapefile_zip(tmp_path / "shp_missing")
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(
        buffer, "w"
    ) as dst:
        for name in src.namelist():
            if not name.lower().endswith(".shx"):
                dst.writestr(name, src.read(name))
    return buffer.getvalue()


@pytest.fixture
def dangerous_zip_bytes(tmp_path: Path) -> bytes:
    """ZIP with a traversal member alongside an otherwise valid dataset."""
    data = _build_shapefile_zip(tmp_path / "shp_danger")
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(
        buffer, "w"
    ) as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name))
        dst.writestr("../../evil.shp", b"evil payload")
    return buffer.getvalue()


@pytest.fixture
def malformed_zip_bytes() -> bytes:
    """ZIP magic followed by garbage (invalid central directory)."""
    return b"PK\x03\x04" + b"garbage" * 50


# ------------------------------------------------------------------ helpers


def _all_records() -> list[FileRecord]:
    """Fetch every file record via a fresh session."""
    factory = get_session_factory()
    with factory() as session:
        return list(session.scalars(select(FileRecord)))


def _uploaded_files(settings: Settings) -> list[Path]:
    """List files present in the configured storage directory."""
    root = Path(settings.storage_dir)
    return sorted(root.iterdir()) if root.exists() else []


def _post(
    client: TestClient,
    filename: str,
    content: bytes,
) -> object:
    """Upload bytes under a filename.

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


# --------------------------------------------------------------- happy path


def test_valid_kml_upload(
    client: TestClient, settings: Settings, kml_bytes: bytes
) -> None:
    """A valid KML registers with id/filename/status and stores bytes."""
    response = _post(client, "survey.kml", kml_bytes)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "filename", "status"}
    assert body["filename"] == "survey.kml"
    assert body["status"] == "PROCESSING"
    assert body["id"]

    records = _all_records()
    assert len(records) == 1
    record = records[0]
    assert record.id == body["id"]
    assert record.file_type == "kml"
    # Phase 12: the pipeline runs within the upload request, so the
    # persisted record has reached its final state even though the
    # response reported the acceptance stage (PROCESSING).
    assert record.status == "COMPLETED"

    stored = Path(record.stored_path)
    assert stored.exists()
    assert stored.parent == Path(settings.storage_dir)
    assert stored.read_bytes() == kml_bytes


def test_valid_shapefile_zip_upload(
    client: TestClient, settings: Settings, shapefile_zip_bytes: bytes
) -> None:
    """A shapefile ZIP registers as ``shapefile_zip`` and is stored."""
    response = _post(client, "survey.zip", shapefile_zip_bytes)

    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "survey.zip"
    assert body["status"] == "PROCESSING"

    records = _all_records()
    assert len(records) == 1
    assert records[0].file_type == "shapefile_zip"
    assert Path(records[0].stored_path).exists()


def test_prj_absent_is_accepted(
    client: TestClient, shapefile_zip_no_prj_bytes: bytes
) -> None:
    """``.prj`` is optional: archives without it still upload (201)."""
    response = _post(client, "noprj.zip", shapefile_zip_no_prj_bytes)

    assert response.status_code == 201
    assert response.json()["status"] == "PROCESSING"


# ------------------------------------------------------------------ errors


def test_unsupported_extension(
    client: TestClient, settings: Settings
) -> None:
    """Non-geospatial extensions are rejected with 415; nothing stored."""
    response = _post(client, "notes.txt", b"hello world")

    assert response.status_code == 415
    assert ".txt" in response.json()["error"]["message"]
    assert _all_records() == []
    assert _uploaded_files(settings) == []


def test_malformed_zip(
    client: TestClient, settings: Settings, malformed_zip_bytes: bytes
) -> None:
    """Corrupt archives are rejected with 400; nothing stored."""
    response = _post(client, "broken.zip", malformed_zip_bytes)

    assert response.status_code == 400
    assert "ZIP" in response.json()["error"]["message"]
    assert _all_records() == []
    assert _uploaded_files(settings) == []


def test_dangerous_zip_paths(
    client: TestClient, settings: Settings, dangerous_zip_bytes: bytes
) -> None:
    """Traversal members (``..``) are rejected with 400; nothing stored."""
    response = _post(client, "evil.zip", dangerous_zip_bytes)

    assert response.status_code == 400
    detail = response.json()["error"]["message"]
    assert "unsafe" in detail and "traversal" in detail
    assert _all_records() == []
    assert _uploaded_files(settings) == []


def test_oversized_file(client: TestClient, settings: Settings) -> None:
    """Uploads larger than ``max_upload_mb`` fail with 413."""
    payload = KML_CONTENT + b"a" * (settings.max_upload_mb * 1024 * 1024 + 1)

    response = _post(client, "huge.kml", payload)

    assert response.status_code == 413
    assert str(settings.max_upload_mb) in response.json()["error"]["message"]
    assert _all_records() == []
    assert _uploaded_files(settings) == []


def test_missing_shapefile_components(
    client: TestClient, settings: Settings, missing_component_zip_bytes: bytes
) -> None:
    """A ZIP lacking ``.shx`` is rejected with 400 naming the component."""
    response = _post(client, "incomplete.zip", missing_component_zip_bytes)

    assert response.status_code == 400
    detail = response.json()["error"]["message"]
    assert ".shx" in detail
    assert _all_records() == []
    assert _uploaded_files(settings) == []


# ------------------------------------------------------- duplicates & safety


def test_duplicate_filenames(
    client: TestClient, settings: Settings, kml_bytes: bytes
) -> None:
    """Same filename twice → both succeed with distinct unique ids."""
    first = _post(client, "survey.kml", kml_bytes)
    second = _post(client, "survey.kml", kml_bytes)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]

    records = _all_records()
    assert len(records) == 2
    stored = _uploaded_files(settings)
    assert len(stored) == 2
    # Server-generated names never collide and never reuse client names.
    assert len({p.name for p in stored}) == 2
    assert all(p.name != "survey.kml" for p in stored)


def test_filename_traversal_is_neutralized(
    client: TestClient, settings: Settings, kml_bytes: bytes
) -> None:
    """Directory components in the client name never reach disk or DB."""
    response = _post(client, "../../evil.kml", kml_bytes)

    assert response.status_code == 201
    body = response.json()
    # Traversal segments stripped for display…
    assert body["filename"] == "evil.kml"

    record = next(r for r in _all_records() if r.id == body["id"])
    # …and storage stays strictly inside the configured root.
    storage_root = Path(settings.storage_dir).resolve()
    stored = Path(record.stored_path).resolve()
    assert stored.parent == storage_root
    assert stored.exists()
