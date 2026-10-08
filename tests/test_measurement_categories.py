"""Tests for the structured measurement categories and the public
``measurement_kind`` decision table.

Covers every cell of the supported-geometry matrix: MultiPolygon,
MultiLineString, GeometryCollection, points, empty/missing/invalid
geometries, unsupported types and CRS-less inputs — each asserted to
produce its structured category (SUPPORTED / UNSUPPORTED / INVALID /
NO_MEASUREMENT) without ever raising.
"""

from __future__ import annotations

import json

import pytest
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    box,
)

from app.measurements.service import MeasurementService, measurement_kind
from app.schemas.geospatial import (
    CRSInformation,
    Feature,
    GeospatialFile,
    GeometryInformation,
    Properties,
)
from app.schemas.measurement import MeasurementCategory, MeasurementStatus

UTM33 = "EPSG:32633"


@pytest.fixture
def service() -> MeasurementService:
    """Provide a measurement service."""
    return MeasurementService()


# --------------------------------------- decision table (measurement_kind)


def test_decision_table_polygonal_geometries() -> None:
    """Polygons and multi-polygons measure area."""
    assert measurement_kind(box(0, 0, 1, 1)) == "area"
    assert measurement_kind(MultiPolygon([box(0, 0, 1, 1)])) == "area"


def test_decision_table_linear_geometries() -> None:
    """Lines, rings and multi-lines measure length."""
    assert measurement_kind(LineString([(0, 0), (1, 1)])) == "length"
    assert measurement_kind(MultiLineString([[(0, 0), (1, 1)]])) == "length"
    from shapely.geometry import LinearRing

    assert measurement_kind(LinearRing([(0, 0), (1, 0), (1, 1)])) == "length"


def test_decision_table_points_and_none() -> None:
    """Points, empty and missing geometries need no measurement."""
    assert measurement_kind(Point(0, 0)) == "none"
    assert measurement_kind(MultiPoint([(0, 0)])) == "none"
    assert measurement_kind(Polygon()) == "none"
    assert measurement_kind(None) == "none"


def test_decision_table_collections() -> None:
    """Collections are decided by inspecting their contained geometries."""
    # Areal parts dominate.
    assert (
        measurement_kind(
            GeometryCollection([box(0, 0, 1, 1), LineString([(0, 0), (1, 1)])])
        )
        == "area"
    )
    # Linear parts when no areal parts exist.
    assert (
        measurement_kind(
            GeometryCollection(
                [Point(0, 0), LineString([(0, 0), (1, 1)])]
            )
        )
        == "length"
    )
    # Points only → nothing meaningful.
    assert (
        measurement_kind(GeometryCollection([Point(0, 0), Point(1, 1)]))
        == "none"
    )
    # Nested collections flatten before deciding.
    assert (
        measurement_kind(
            GeometryCollection(
                [GeometryCollection([LineString([(0, 0), (2, 0)])])]
            )
        )
        == "length"
    )


def test_decision_table_unsupported() -> None:
    """Foreign geometry types classify as unsupported, not as a crash."""

    class _Unknown:
        geom_type = "CircularString"
        is_empty = False

    assert measurement_kind(_Unknown()) == "unsupported"  # type: ignore[arg-type]


# --------------------------------------------- structured categories


def _measure(geometry, crs: str | None = UTM33):
    """Measure one geometry with a fresh service."""
    return MeasurementService().measure_geometry(geometry, crs)


def test_multipolygon_category_supported_with_total_area() -> None:
    """MultiPolygon → SUPPORTED with the summed area."""
    result = _measure(MultiPolygon([box(0, 0, 10, 10), box(20, 0, 50, 40)]))

    assert result.category == MeasurementCategory.SUPPORTED
    assert result.status == MeasurementStatus.MEASURED
    assert result.measurement.area == 1_300.0


def test_multilinestring_category_supported_with_total_length() -> None:
    """MultiLineString → SUPPORTED with the summed length."""
    result = _measure(
        MultiLineString([[(0, 0), (300, 400)], [(0, 0), (600, 0)]])
    )

    assert result.category == MeasurementCategory.SUPPORTED
    assert result.measurement.length == 1_100.0  # 500 + 600


def test_collection_meaningful_measurement_supported() -> None:
    """Collections with measurable parts → SUPPORTED."""
    area_result = _measure(GeometryCollection([box(0, 0, 5, 5)]))
    length_result = _measure(
        GeometryCollection([LineString([(0, 0), (300, 400)])])
    )

    assert area_result.category == MeasurementCategory.SUPPORTED
    assert area_result.measurement.area == 25.0
    assert length_result.category == MeasurementCategory.SUPPORTED
    assert length_result.measurement.length == 500.0


def test_points_only_collection_category_no_measurement() -> None:
    """Points-only collections → NO_MEASUREMENT with a null value."""
    result = _measure(GeometryCollection([Point(0, 0), Point(1, 1)]))

    assert result.category == MeasurementCategory.NO_MEASUREMENT
    assert result.measurement is None


def test_point_and_multipoint_categories(service: MeasurementService) -> None:
    """Points and multi-points → NO_MEASUREMENT."""
    for geometry in (Point(1, 1), MultiPoint([(0, 0), (1, 1)])):
        result = service.measure_geometry(geometry, UTM33)
        assert result.category == MeasurementCategory.NO_MEASUREMENT
        assert result.measurement is None


def test_empty_and_missing_geometry_categories() -> None:
    """Empty/missing geometries → NO_MEASUREMENT, never an exception."""
    empty = _measure(Polygon())
    missing = _measure(None)

    assert empty.category == MeasurementCategory.NO_MEASUREMENT
    assert empty.status == MeasurementStatus.SKIPPED_EMPTY
    assert missing.category == MeasurementCategory.NO_MEASUREMENT
    assert missing.status == MeasurementStatus.SKIPPED_MISSING_GEOMETRY


def test_invalid_geometry_category_invalid() -> None:
    """Self-intersecting geometries → INVALID (measurement withheld)."""
    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])

    result = _measure(bowtie)

    assert result.category == MeasurementCategory.INVALID
    assert result.status == MeasurementStatus.SKIPPED_INVALID
    assert result.measurement is None


def test_unsupported_geometry_category_unsupported() -> None:
    """Unknown types → UNSUPPORTED; the application keeps running."""

    class _Unknown:
        geom_type = "CircularString"
        is_empty = False
        is_valid = True

    result = _measure(_Unknown())  # type: ignore[arg-type]

    assert result.category == MeasurementCategory.UNSUPPORTED
    assert result.status == MeasurementStatus.SKIPPED_UNSUPPORTED_TYPE
    assert result.measurement is None


def test_crs_problems_category_no_measurement() -> None:
    """Missing and invalid CRS → NO_MEASUREMENT (4326 never assumed)."""
    no_crs = _measure(box(0, 0, 1, 1), None)
    bad_crs = _measure(box(0, 0, 1, 1), "not-a-crs")

    assert no_crs.category == MeasurementCategory.NO_MEASUREMENT
    assert no_crs.status == MeasurementStatus.SKIPPED_NO_CRS
    assert bad_crs.category == MeasurementCategory.NO_MEASUREMENT
    assert bad_crs.status == MeasurementStatus.SKIPPED_INVALID_CRS


def test_error_category_invalid_for_corrupt_input() -> None:
    """Corrupt WKT (unexpected per-feature failure) → INVALID, contained."""
    broken = Feature(
        feature_id=7,
        geometry_type="Polygon",
        geometry=GeometryInformation(
            geometry_type="Polygon", wkt="POLYGON ((broken"
        ),
        source_crs=CRSInformation(authority_code=UTM33),
        properties=Properties({}),
    )
    file_model = GeospatialFile(
        filename="broken.kml",
        file_type="kml",
        source_crs=CRSInformation(authority_code=UTM33),
        features=[broken],
    )

    results = MeasurementService().measure_file(file_model)

    assert results[0].category == MeasurementCategory.INVALID
    assert results[0].status == MeasurementStatus.ERROR
    assert results[0].measurement is None


def test_category_serialized_in_json(service: MeasurementService) -> None:
    """The structured category appears in the API JSON for every case."""
    measured = service.measure_geometry(box(0, 0, 1, 1), UTM33)
    invalid = service.measure_geometry(
        Polygon([(0, 0), (2, 2), (2, 0), (0, 2)]), UTM33
    )

    assert (
        json.loads(measured.model_dump_json())["category"] == "SUPPORTED"
    )
    assert (
        json.loads(invalid.model_dump_json())["category"] == "INVALID"
    )
    assert (
        json.loads(
            service.measure_geometry(Point(0, 0), UTM33).model_dump_json()
        )["category"]
        == "NO_MEASUREMENT"
    )


def test_full_matrix_never_raises() -> None:
    """Every geometry case in the matrix returns a structured result."""
    cases = [
        (box(0, 0, 1, 1), UTM33),
        (MultiPolygon([box(0, 0, 1, 1)]), UTM33),
        (LineString([(0, 0), (1, 1)]), UTM33),
        (MultiLineString([[(0, 0), (1, 1)]]), UTM33),
        (GeometryCollection([box(0, 0, 1, 1)]), UTM33),
        (GeometryCollection([Point(0, 0)]), UTM33),
        (Point(0, 0), UTM33),
        (Polygon(), UTM33),
        (None, UTM33),
        (Polygon([(0, 0), (2, 2), (2, 0), (0, 2)]), UTM33),
        (box(0, 0, 1, 1), None),
    ]
    for geometry, crs in cases:
        result = MeasurementService().measure_geometry(geometry, crs)
        assert isinstance(result.category, MeasurementCategory)
        assert result.model_dump_json()  # serializes for the API
