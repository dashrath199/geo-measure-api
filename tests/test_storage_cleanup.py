"""Storage-cleanup verification for the processing pipeline (Phase 19).

Cleanup contract under test:

- Temporary shapefile extraction directories (``geo-process-*``) must
  vanish after **every** outcome: successful processing and processing
  failure alike.
- Uploaded bytes are deleted when registration (the database write)
  fails — the storage call happens first, so a failed insert must not
  leave an orphaned file.
- For a *registered* file the stored upload is retained by design,
  including after a ``FAILED`` processing run: the record still points
  at the bytes for inspection, and no API response ever exposes the
  path.
"""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Point

from app.config import Settings
from app.db.repository import FileRepository
from app.main import create_app

KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document><Placemark><Point><coordinates>0,0</coordinates></Point></Placemark></Document>
</kml>
"""


# ----------------------------------------------------------------- helpers


def _shapefile_zip(target: Path) -> bytes:
    """Build a valid one-feature shapefile archive.

    Args:
        target: Directory to write the shapefile into.

    Returns:
        The archive as bytes.
    """
    target.mkdir(parents=True, exist_ok=True)
    gdf = gpd.GeoDataFrame(
        {"name": ["a"]}, geometry=[Point(0, 0)], crs="EPSG:4326"
    )
    gdf.to_file(target / "sample.shp")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for part in target.iterdir():
            archive.write(part, part.name)
    return buffer.getvalue()


def _unreadable_shp_zip() -> bytes:
    """Build a structurally complete archive whose components are garbage.

    Passes upload validation (component trio present, limits fine) but
    fails during reading — a processing-stage failure that happens
    *after* extraction started.

    Returns:
        The archive as bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("sample.shp", b"this is not a shapefile")
        archive.writestr("sample.shx", b"nor is this an shx")
        archive.writestr("sample.dbf", b"nor is this a dbf")
    return buffer.getvalue()


def _extraction_dirs() -> set[Path]:
    """Return every live per-run extraction directory."""
    return set(Path(tempfile.gettempdir()).glob("geo-process-*"))


def _stored(settings: Settings) -> list[Path]:
    """List files present in the configured storage directory."""
    root = Path(settings.storage_dir)
    return sorted(root.iterdir()) if root.exists() else []


def _post(
    client: TestClient, filename: str, content: bytes
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


# ------------------------------------------------------------------- tests


def test_temp_dir_removed_after_successful_processing(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    """A successful shapefile run leaves no extraction directory behind."""
    before = _extraction_dirs()

    response = _post(client, "survey.zip", _shapefile_zip(tmp_path / "ok"))

    assert response.status_code == 201
    file_id = response.json()["id"]
    info = client.get(f"/api/files/{file_id}/")
    assert info.json()["status"] == "COMPLETED"

    # No new extraction directory survives the run.
    assert _extraction_dirs() == before
    # The upload itself is retained: the record points at it.
    assert len(_stored(settings)) == 1


def test_temp_dir_removed_after_failed_processing(
    client: TestClient, settings: Settings
) -> None:
    """A processing failure must not leak the extraction directory."""
    before = _extraction_dirs()

    response = _post(client, "bad.zip", _unreadable_shp_zip())

    assert response.status_code == 201
    assert response.json()["status"] == "FAILED"

    # The TemporaryDirectory context cleans up even on the failure
    # path that marks the record FAILED.
    assert _extraction_dirs() == before
    # Retention by design: the FAILED record still references the
    # bytes (never exposed through the API).
    assert len(_stored(settings)) == 1


def test_stored_bytes_removed_when_registration_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the database write fails, the just-stored upload is deleted."""

    def boom(self: object, **kwargs: object) -> object:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(FileRepository, "create", boom)
    app = create_app(settings=settings)

    with TestClient(app, raise_server_exceptions=False) as broken_client:
        response = _post(broken_client, "survey.kml", KML)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    # No orphaned bytes: the cleanup in FileService ran before the
    # exception propagated to the catch-all handler.
    assert _stored(settings) == []
