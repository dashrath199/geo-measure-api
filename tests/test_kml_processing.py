"""Tests for KML processing and the shared processor representation.

Required cases: Point KML, LineString KML, Polygon KML, invalid KML and
empty KML — plus explicit EPSG:4326 handling and proof that both
processors emit the identical internal structure.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point

from app.exceptions import GeometryProcessingError, InvalidFileError
from app.geo.processors import KMLProcessor, ShapefileProcessor, get_processor
from app.schemas.geospatial import Feature, GeospatialFile


def _kml(body: str) -> str:
    """Wrap placemark body markup in a minimal KML document.

    Args:
        body: Inner document content.

    Returns:
        A complete KML document string.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2">\n'
        f"<Document>\n{body}\n</Document>\n"
        "</kml>\n"
    )


@pytest.fixture
def point_kml_path(tmp_path: Path) -> Path:
    """A valid KML with one Point placemark."""
    path = tmp_path / "point.kml"
    path.write_text(
        _kml(
            '<Placemark><name>Place One</name><description>first</description>'
            "<Point><coordinates>-122.0,37.5,0</coordinates></Point>"
            "</Placemark>"
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def linestring_kml_path(tmp_path: Path) -> Path:
    """A valid KML with one LineString placemark."""
    path = tmp_path / "line.kml"
    path.write_text(
        _kml(
            '<Placemark><name>Route</name>'
            "<LineString><coordinates>0,0,0 1,1,0 2,0,0</coordinates>"
            "</LineString></Placemark>"
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def polygon_kml_path(tmp_path: Path) -> Path:
    """A valid KML with one Polygon placemark."""
    path = tmp_path / "area.kml"
    path.write_text(
        _kml(
            '<Placemark><name>Area</name><Polygon><outerBoundaryIs>'
            "<LinearRing><coordinates>"
            "0,0,0 0,1,0 1,1,0 1,0,0 0,0,0"
            "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
            "</Placemark>"
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def empty_kml_path(tmp_path: Path) -> Path:
    """A valid KML document containing no placemarks."""
    path = tmp_path / "empty.kml"
    path.write_text(_kml(""), encoding="utf-8")
    return path


# ---------------------------------------------------------- valid formats


def test_point_kml(point_kml_path: Path) -> None:
    """Point KML yields one Point feature with attributes."""
    dataset = KMLProcessor().process(point_kml_path)

    assert dataset.feature_count == 1
    feature = dataset.features[0]
    assert feature.feature_id == 0
    assert feature.geometry_type == "Point"
    assert feature.geometry is not None
    # Geometry is domain data (WKT/bbox), not a live Shapely object.
    assert feature.geometry.wkt.startswith("POINT")
    assert feature.geometry.bbox == pytest.approx(
        (-122.0, 37.5, -122.0, 37.5)
    )
    assert feature.properties["Name"] == "Place One"
    assert feature.properties["description"] == "first"
    # Attribute values stay JSON-safe despite driver metadata columns.
    json.dumps(feature.properties.root)


def test_linestring_kml(linestring_kml_path: Path) -> None:
    """LineString KML yields one LineString feature with 3 vertices."""
    dataset = KMLProcessor().process(linestring_kml_path)

    feature = dataset.features[0]
    assert dataset.feature_count == 1
    assert feature.geometry_type == "LineString"
    assert feature.geometry is not None
    assert feature.geometry.wkt.startswith("LINESTRING")
    # Three vertices → two coordinate-pair separators in the WKT.
    assert feature.geometry.wkt.count(",") == 2
    assert feature.properties["Name"] == "Route"


def test_polygon_kml(polygon_kml_path: Path) -> None:
    """Polygon KML yields one Polygon feature."""
    dataset = KMLProcessor().process(polygon_kml_path)

    feature = dataset.features[0]
    assert dataset.feature_count == 1
    assert feature.geometry_type == "Polygon"
    assert feature.geometry is not None
    assert feature.geometry.is_valid
    assert feature.properties["Name"] == "Area"


def test_feature_ids_are_sequential(tmp_path: Path) -> None:
    """Multiple placemarks are numbered from zero in document order."""
    path = tmp_path / "multi.kml"
    path.write_text(
        _kml(
            "<Placemark><name>a</name>"
            "<Point><coordinates>0,0,0</coordinates></Point></Placemark>"
            "<Placemark><name>b</name>"
            "<Point><coordinates>1,1,0</coordinates></Point></Placemark>"
        ),
        encoding="utf-8",
    )

    dataset = KMLProcessor().process(path)

    assert [f.feature_id for f in dataset.features] == [0, 1]


# ----------------------------------------------------------------- CRS


def test_kml_crs_is_explicitly_wgs84(point_kml_path: Path) -> None:
    """KML CRS is explicitly EPSG:4326 at dataset and feature level."""
    dataset = KMLProcessor().process(point_kml_path)

    assert dataset.source_crs.authority_code == "EPSG:4326"
    assert dataset.source_crs.is_geographic is True
    assert dataset.features[0].source_crs.authority_code == "EPSG:4326"


def test_empty_kml_still_reports_explicit_crs(empty_kml_path: Path) -> None:
    """Even with zero features the KML CRS is explicit, not None."""
    dataset = KMLProcessor().process(empty_kml_path)

    assert dataset.source_crs.authority_code == "EPSG:4326"


# ---------------------------------------------------- invalid and empty


def test_invalid_kml_not_xml(tmp_path: Path) -> None:
    """Garbage without a KML root fails content validation."""
    path = tmp_path / "bad.kml"
    path.write_text("this is definitely not xml <<<", encoding="utf-8")

    with pytest.raises(InvalidFileError, match="KML"):
        KMLProcessor().process(path)


def test_invalid_kml_malformed_xml(tmp_path: Path) -> None:
    """KML-shaped content that GDAL cannot parse is a domain error."""
    path = tmp_path / "broken.kml"
    path.write_text("<kml><Document><Placemark>", encoding="utf-8")

    with pytest.raises(GeometryProcessingError, match="Could not read KML"):
        KMLProcessor().process(path)


def test_empty_kml_yields_zero_features(empty_kml_path: Path) -> None:
    """A valid KML without placemarks reads as an empty dataset."""
    dataset = KMLProcessor().process(empty_kml_path)

    assert dataset.feature_count == 0
    assert dataset.features == []


def test_missing_kml_file_raises(tmp_path: Path) -> None:
    """A nonexistent KML is a clear domain error."""
    with pytest.raises(GeometryProcessingError, match="not found"):
        KMLProcessor().process(tmp_path / "nope.kml")


def test_wrong_extension_rejected(tmp_path: Path) -> None:
    """The KML processor only accepts ``.kml`` inputs."""
    other = tmp_path / "data.txt"
    other.write_text("<kml></kml>")

    with pytest.raises(GeometryProcessingError, match=r"\.kml"):
        KMLProcessor().process(other)


# ------------------------------------------- shared representation


def test_both_processors_emit_the_same_structure(
    tmp_path: Path, point_kml_path: Path
) -> None:
    """ShapefileProcessor and KMLProcessor return the same domain model."""
    gdf = gpd.GeoDataFrame(
        {"name": ["s"]}, geometry=[Point(1, 2)], crs="EPSG:4326"
    )
    shp_path = tmp_path / "same.shp"
    gdf.to_file(shp_path)

    shp_result = ShapefileProcessor().process(shp_path)
    kml_result = KMLProcessor().process(point_kml_path)

    # Identical domain type — the shared contract of the pipeline.
    assert type(shp_result) is type(kml_result) is GeospatialFile
    for result in (shp_result, kml_result):
        assert isinstance(result.features[0], Feature)
        assert set(Feature.model_fields) == {
            "feature_id",
            "geometry_type",
            "geometry",
            "source_crs",
            "properties",
        }
        # Serializable end-to-end: no live geospatial objects leak out.
        result.model_dump_json()


def test_get_processor_dispatches_by_file_type() -> None:
    """The factory maps upload file types to the right processor."""
    assert isinstance(get_processor("kml"), KMLProcessor)
    assert isinstance(get_processor("shapefile_zip"), ShapefileProcessor)

    with pytest.raises(GeometryProcessingError, match="No processor"):
        get_processor("csv")


def test_processors_contain_no_measurement_logic() -> None:
    """Measurement stays out of the processors (single future home)."""
    import ast

    for module in ("processors.py", "readers.py"):
        source = (
            Path(__file__).resolve().parents[1] / "app" / "geo" / module
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(
                    alias.name.split(".")[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        # Measurement would arrive via shapely .area/.length calls or a
        # measurements module; neither may exist here yet.
        assert "app.measurements" not in imported
        assert "area(" not in source
        assert ".length" not in source
