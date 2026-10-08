"""Tests for multi-geometry handling: MultiPolygon, MultiLineString and
GeometryCollection — unit level (exact math in a projected CRS) and
end-to-end through both format processors.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
    box,
)

from app.geo.processors import KMLProcessor, ShapefileProcessor
from app.measurements.service import MeasurementService
from app.schemas.measurement import MeasurementStatus

UTM33 = "EPSG:32633"


# ------------------------------------------------ unit: multi geometries


def test_multipolygon_area_is_sum_of_parts() -> None:
    """Disjoint 10x10 + 30x40 MultiPolygon measures 1300 m2 exactly."""
    service = MeasurementService()
    # Parts must be disjoint — overlapping MultiPolygon parts are OGC-
    # invalid (and are deliberately skipped as such).
    multi = MultiPolygon([box(0, 0, 10, 10), box(20, 0, 50, 40)])

    result = service.measure_geometry(multi, UTM33)

    assert result.status == MeasurementStatus.MEASURED
    assert result.measurement.unit == "square_meters"
    assert result.measurement.area == 1_300.0


def test_multilinestring_length_is_sum_of_parts() -> None:
    """500 m + 700 m MultiLineString measures 1200 m exactly."""
    service = MeasurementService()
    multi = MultiLineString(
        [
            [(0, 0), (300, 400)],  # hypotenuse: 500 m
            [(0, 0), (300, 0), (300, 400)],  # 300 + 400 = 700 m
        ]
    )

    result = service.measure_geometry(multi, UTM33)

    assert result.status == MeasurementStatus.MEASURED
    assert result.measurement.unit == "meters"
    assert result.measurement.length == 1_200.0


# ------------------------------------------------ unit: collections


def test_collection_of_polygons_measures_area() -> None:
    """A collection of two polygons reports their summed area."""
    service = MeasurementService()
    collection = GeometryCollection(
        [box(0, 0, 10, 10), box(20, 0, 30, 10)]
    )

    result = service.measure_geometry(collection, UTM33)

    assert result.status == MeasurementStatus.MEASURED
    assert result.measurement.unit == "square_meters"
    assert result.measurement.area == 200.0
    assert result.measurement_crs == UTM33


def test_collection_of_lines_measures_length() -> None:
    """A collection of two lines reports their summed length."""
    service = MeasurementService()
    collection = GeometryCollection(
        [
            LineString([(0, 0), (300, 400)]),  # 500 m
            LineString([(0, 0), (1000, 0)]),  # 1000 m
        ]
    )

    result = service.measure_geometry(collection, UTM33)

    assert result.status == MeasurementStatus.MEASURED
    assert result.measurement.unit == "meters"
    assert result.measurement.length == 1_500.0


def test_nested_collections_are_recursed() -> None:
    """Collections inside collections flatten fully before measuring."""
    service = MeasurementService()
    nested = GeometryCollection(
        [
            GeometryCollection([box(0, 0, 2, 2)]),  # 4 m2
            GeometryCollection([box(10, 0, 15, 4)]),  # 20 m2
        ]
    )

    result = service.measure_geometry(nested, UTM33)

    assert result.status == MeasurementStatus.MEASURED
    assert result.measurement.area == 24.0


def test_mixed_collection_reports_area_with_note() -> None:
    """Areal parts win in mixed collections; line parts are disclosed."""
    service = MeasurementService()
    mixed = GeometryCollection(
        [box(0, 0, 10, 10), LineString([(0, 0), (100, 0)])]
    )

    result = service.measure_geometry(mixed, UTM33)

    assert result.status == MeasurementStatus.MEASURED
    assert result.measurement.unit == "square_meters"
    assert result.measurement.area == 100.0
    # The linear part is disclosed rather than silently ignored.
    assert result.reason is not None
    assert "linear part" in result.reason


def test_points_only_collection_returns_null() -> None:
    """Collections of pure points need no measurement."""
    service = MeasurementService()
    points = GeometryCollection([Point(0, 0), Point(1, 1)])

    result = service.measure_geometry(points, UTM33)

    assert result.status == MeasurementStatus.NOT_REQUIRED
    assert result.measurement is None
    payload = json.loads(result.model_dump_json())
    assert payload["measurement"] is None


def test_empty_collection_skipped() -> None:
    """An empty collection skips gracefully."""
    service = MeasurementService()

    result = service.measure_geometry(GeometryCollection([]), UTM33)

    assert result.status == MeasurementStatus.SKIPPED_EMPTY
    assert result.measurement is None


def test_collection_with_invalid_part_skipped() -> None:
    """A collection harbouring a self-intersecting part is skipped."""
    service = MeasurementService()
    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])
    tainted = GeometryCollection([box(10, 10, 20, 20), bowtie])

    assert not tainted.is_valid  # GEOS propagates part invalidity

    result = service.measure_geometry(tainted, UTM33)

    assert result.status == MeasurementStatus.SKIPPED_INVALID
    assert result.measurement is None


def test_collection_no_crs_skipped() -> None:
    """Collections obey the same no-CRS refusal as single geometries."""
    service = MeasurementService()

    result = service.measure_geometry(
        GeometryCollection([box(0, 0, 1, 1)]), None
    )

    assert result.status == MeasurementStatus.SKIPPED_NO_CRS
    assert result.measurement is None


# ------------------------------------------------ end-to-end: processors


def test_shapefile_multipolygon_end_to_end(tmp_path: Path) -> None:
    """Shapefile MultiPolygon in a projected CRS → exact area via the
    full pipeline (processor → domain model → measurement)."""
    multi = MultiPolygon([box(0, 0, 10, 10), box(100, 0, 110, 10)])
    gdf = gpd.GeoDataFrame(
        {"name": ["mp"]}, geometry=[multi], crs="EPSG:32633"
    )
    shp_path = tmp_path / "multi.shp"
    gdf.to_file(shp_path)

    geospatial_file = ShapefileProcessor().process(shp_path)
    results = MeasurementService().measure_file(geospatial_file)

    assert len(results) == 1
    assert results[0].status == MeasurementStatus.MEASURED
    assert results[0].measurement.unit == "square_meters"
    assert results[0].measurement.area == 200.0
    assert results[0].measurement_crs == "EPSG:32633"


def test_shapefile_multilinestring_end_to_end(tmp_path: Path) -> None:
    """Shapefile MultiLineString in a projected CRS → exact length."""
    multi = MultiLineString([[(0, 0), (300, 400)], [(0, 0), (600, 0)]])
    gdf = gpd.GeoDataFrame(
        {"name": ["ml"]}, geometry=[multi], crs="EPSG:32633"
    )
    shp_path = tmp_path / "multi.shp"
    gdf.to_file(shp_path)

    geospatial_file = ShapefileProcessor().process(shp_path)
    results = MeasurementService().measure_file(geospatial_file)

    assert results[0].status == MeasurementStatus.MEASURED
    assert results[0].measurement.unit == "meters"
    assert results[0].measurement.length == 1_100.0  # 500 + 600


def test_kml_multigeometry_end_to_end(tmp_path: Path) -> None:
    """KML <MultiGeometry> flows through the whole pipeline.

    GDAL may expose MultiGeometry as a native multi type *or* a
    GeometryCollection — both must measure cleanly.
    """
    path = tmp_path / "multi.kml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        "<Placemark><name>two-lines</name><MultiGeometry>"
        "<LineString><coordinates>0,0,0 0.01,0,0</coordinates></LineString>"
        "<LineString><coordinates>0,0,0 0,0.01,0</coordinates></LineString>"
        "</MultiGeometry></Placemark></Document></kml>",
        encoding="utf-8",
    )

    geospatial_file = KMLProcessor().process(path)
    results = MeasurementService().measure_file(geospatial_file)

    assert len(results) == 1
    assert results[0].status == MeasurementStatus.MEASURED
    assert results[0].measurement.unit == "meters"
    # Two ~1113 m legs at the equator: ~2226 m total (loose tolerance).
    assert 2_000 < results[0].measurement.length < 2_500
    assert results[0].measurement_crs.startswith("EPSG:32")


def test_kml_multigeometry_polygon_end_to_end(tmp_path: Path) -> None:
    """KML MultiGeometry of polygons measures area end to end."""
    path = tmp_path / "polys.kml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        "<Placemark><name>two-areas</name><MultiGeometry>"
        "<Polygon><outerBoundaryIs><LinearRing>"
        "<coordinates>0,0,0 0.01,0,0 0.01,0.01,0 0,0.01,0 0,0,0"
        "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
        "<Polygon><outerBoundaryIs><LinearRing>"
        "<coordinates>0.02,0,0 0.03,0,0 0.03,0.01,0 0.02,0.01,0 0.02,0,0"
        "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
        "</MultiGeometry></Placemark></Document></kml>",
        encoding="utf-8",
    )

    geospatial_file = KMLProcessor().process(path)
    results = MeasurementService().measure_file(geospatial_file)

    assert results[0].status == MeasurementStatus.MEASURED
    assert results[0].measurement.unit == "square_meters"
    # Each 0.01°x0.01° cell ≈ 1.239 km² at the equator; two cells.
    assert 2_000_000 < results[0].measurement.area < 2_500_000
