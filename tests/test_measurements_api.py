"""API tests for ``GET /api/files/{id}/measurements/``.

Centerpiece: a mixed-geometry KML (Polygon + LineString + Point in one
file) whose three features each exercise a different outcome. Plus
exact-value shapefile checks, empty/unprocessed/failed files, id
validation, and a no-internal-objects contract sweep.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import geopandas as gpd
from fastapi.testclient import TestClient
from shapely.geometry import LineString, box

from app.db.base import get_session_factory
from app.db.models import FileStatus
from app.db.repository import FileRepository

VALID_STATUSES = {"SUPPORTED", "UNSUPPORTED", "INVALID", "NO_MEASUREMENT"}
PER_FEATURE_KEYS = {
    "feature_id",
    "geometry_type",
    "measurement",
    "status",
    "reason",
    "properties",
}

MIXED_KML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
    '<Placemark><name>Poly A</name><Polygon><outerBoundaryIs><LinearRing>'
    "<coordinates>0,0,0 0.01,0,0 0.01,0.01,0 0,0.01,0 0,0,0"
    "</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
    '<Placemark><name>Line B</name><LineString>'
    "<coordinates>0,0,0 0.01,0,0</coordinates></LineString></Placemark>"
    '<Placemark><name>Point C</name><Point>'
    "<coordinates>1,2,0</coordinates></Point></Placemark>"
    "</Document></kml>"
)


def _upload(client: TestClient, filename: str, content: bytes) -> str:
    """Upload bytes and return the file id."""
    response = client.post(
        "/api/files/",
        files={"file": (filename, content, "application/octet-stream")},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _shapefile_zip(tmp_path: Path, gdf: gpd.GeoDataFrame) -> bytes:
    """Write a GeoDataFrame as a shapefile archive."""
    build = tmp_path / "build"
    build.mkdir(parents=True, exist_ok=True)
    gdf.to_file(build / "sample.shp")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for part in build.iterdir():
            archive.write(part, part.name)
    return buffer.getvalue()


def _assert_json_primitives(payload: object) -> None:
    """Recursively prove the payload holds only plain JSON values."""
    if isinstance(payload, dict):
        for value in payload.values():
            _assert_json_primitives(value)
    elif isinstance(payload, list):
        for item in payload:
            _assert_json_primitives(item)
    else:
        assert payload is None or isinstance(payload, (str, int, float, bool)), (
            type(payload)
        )


# ------------------------------------------------------ mixed-geometry file


def test_mixed_geometry_file_returns_all_three_outcomes(
    client: TestClient,
) -> None:
    """Polygon, LineString and Point in one file each report correctly."""
    file_id = _upload(client, "mixed.kml", MIXED_KML.encode())

    response = client.get(f"/api/files/{file_id}/measurements/")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"file_id", "crs", "features"}
    assert body["file_id"] == file_id
    assert body["crs"] == "EPSG:4326"
    assert len(body["features"]) == 3

    by_id = {f["feature_id"]: f for f in body["features"]}
    assert set(by_id) == {0, 1, 2}

    polygon = by_id[0]
    assert polygon["geometry_type"] == "Polygon"
    assert polygon["status"] == "SUPPORTED"
    assert polygon["measurement"]["type"] == "area"
    assert polygon["measurement"]["unit"] == "square_meters"
    # 0.01° cell at the equator ≈ 1.23 km² (loose tolerance).
    assert 1_000_000 < polygon["measurement"]["value"] < 1_500_000
    assert polygon["properties"]["Name"] == "Poly A"

    line = by_id[1]
    assert line["geometry_type"] == "LineString"
    assert line["status"] == "SUPPORTED"
    assert line["measurement"]["type"] == "length"
    assert line["measurement"]["unit"] == "meters"
    assert 1_000 < line["measurement"]["value"] < 1_300
    assert line["properties"]["Name"] == "Line B"

    point = by_id[2]
    assert point["geometry_type"] == "Point"
    assert point["measurement"] is None
    assert point["status"] == "NO_MEASUREMENT"
    assert point["reason"]
    assert point["properties"]["Name"] == "Point C"


def test_no_internal_objects_in_response(client: TestClient) -> None:
    """The payload is plain JSON: exact per-feature keys, primitive-only."""
    file_id = _upload(client, "mixed.kml", MIXED_KML.encode())

    body = client.get(f"/api/files/{file_id}/measurements/").json()

    for feature in body["features"]:
        assert set(feature) == PER_FEATURE_KEYS
        assert feature["status"] in VALID_STATUSES
    _assert_json_primitives(body)
    # No serialization of internal representation ever happens.
    assert "wkt" not in json.dumps(body)
    assert "shapely" not in json.dumps(body).lower()


# ------------------------------------------------------- exact values


def test_shapefile_area_exact_via_api(tmp_path: Path, client: TestClient) -> None:
    """A 100 m² projected square reports exactly 100 through the API."""
    gdf = gpd.GeoDataFrame(
        {"name": ["sq"]}, geometry=[box(0, 0, 10, 10)], crs="EPSG:32633"
    )
    file_id = _upload(client, "area.zip", _shapefile_zip(tmp_path, gdf))

    body = client.get(f"/api/files/{file_id}/measurements/").json()

    feature = body["features"][0]
    assert body["crs"] == "EPSG:32633"
    assert feature["measurement"] == {
        "type": "area",
        "value": 100.0,
        "unit": "square_meters",
    }
    assert feature["status"] == "SUPPORTED"
    assert feature["properties"] == {"name": "sq"}


def test_shapefile_length_exact_via_api(tmp_path: Path, client: TestClient) -> None:
    """A 3-4-5 line reports exactly 500 m through the API."""
    gdf = gpd.GeoDataFrame(
        {"name": ["hyp"]},
        geometry=[LineString([(0, 0), (300, 400)])],
        crs="EPSG:32633",
    )
    file_id = _upload(client, "length.zip", _shapefile_zip(tmp_path, gdf))

    body = client.get(f"/api/files/{file_id}/measurements/").json()

    assert body["features"][0]["measurement"] == {
        "type": "length",
        "value": 500.0,
        "unit": "meters",
    }


# ------------------------------------------------- empty / edge states


def test_empty_kml_returns_empty_features(client: TestClient) -> None:
    """A processed file with no placemarks returns an empty list."""
    empty = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2">'
        "<Document></Document></kml>"
    )
    file_id = _upload(client, "empty.kml", empty.encode())

    body = client.get(f"/api/files/{file_id}/measurements/").json()

    assert body["file_id"] == file_id
    assert body["crs"] == "EPSG:4326"
    assert body["features"] == []


def test_unprocessed_file_returns_empty_features(
    client: TestClient,
) -> None:
    """A record that never ran the pipeline yields no measurements."""
    factory = get_session_factory()
    with factory() as session:
        repository = FileRepository(session)
        record = repository.create(
            original_filename="pending.kml",
            stored_path="uploads/pending.kml",
            file_type="kml",
            status=FileStatus.UPLOADED,
        )

    body = client.get(f"/api/files/{record.id}/measurements/").json()

    assert body["features"] == []
    assert body["crs"] is None


def test_failed_file_returns_empty_features(client: TestClient) -> None:
    """FAILED files expose no measurements but respond successfully."""
    factory = get_session_factory()
    with factory() as session:
        repository = FileRepository(session)
        record = repository.create(
            original_filename="broken.zip",
            stored_path="uploads/broken.zip",
            file_type="shapefile_zip",
        )
        repository.set_status(
            record, FileStatus.FAILED, error_message="safe failure"
        )

    response = client.get(f"/api/files/{record.id}/measurements/")

    assert response.status_code == 200
    assert response.json()["features"] == []


# ------------------------------------------------------- id validation


def test_unknown_id_returns_404(client: TestClient) -> None:
    """Well-formed but unknown UUIDs map to 404."""
    response = client.get(
        "/api/files/00000000-0000-4000-8000-000000000000/measurements/"
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "FILE_NOT_FOUND"


def test_malformed_id_returns_422(client: TestClient) -> None:
    """Non-UUID path parameters are validation errors."""
    response = client.get("/api/files/not-a-uuid/measurements/")

    assert response.status_code == 422
