"""Extensive unit tests for the measurement engine.

Every value is verified against closed-form mathematics: grid squares
and 3-4-5 triangles in a projected CRS give exact areas/lengths, and a
known equatorial degree extent gives a verifiable metre result after
transformation. Degenerate inputs are asserted to skip gracefully, and
one bad feature is proven not to abort a file.
"""

from __future__ import annotations

import json

import pytest
from shapely.geometry import (
    GeometryCollection,
    LineString,
    LinearRing,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    box,
)

from app.geo.crs import CRSService
from app.measurements.service import MeasurementService
from app.schemas.geospatial import (
    CRSInformation,
    Feature,
    GeospatialFile,
    GeometryInformation,
    Properties,
)
from app.schemas.measurement import (
    AreaMeasurement,
    FeatureMeasurement,
    LengthMeasurement,
    MeasurementStatus,
)

# A projected, metric UTM zone: measurements here are exact grid metres.
UTM33 = "EPSG:32633"


@pytest.fixture
def service() -> MeasurementService:
    """Provide a measurement service with the real CRS service."""
    return MeasurementService()


def _feature(
    feature_id: int,
    geometry,
    crs: str | None = "EPSG:4326",
) -> Feature:
    """Build a domain feature from a shapely geometry.

    Args:
        feature_id: Feature index.
        geometry: Shapely geometry (or ``None``).
        crs: Authority code for the feature's CRS.

    Returns:
        The normalized feature.
    """
    crs_info = CRSInformation(authority_code=crs)
    geometry_info = None
    geometry_type = None
    if geometry is not None:
        geometry_type = geometry.geom_type
        bounds = geometry.bounds
        geometry_info = GeometryInformation(
            geometry_type=geometry_type,
            wkt=geometry.wkt,
            bbox=tuple(bounds) if bounds and all(
                v == v for v in bounds
            ) else None,
            is_valid=geometry.is_valid,
            is_empty=geometry.is_empty,
        )
    return Feature(
        feature_id=feature_id,
        geometry_type=geometry_type,
        geometry=geometry_info,
        source_crs=crs_info,
        properties=Properties({}),
    )


# ----------------------------------------------------- exact planar values


def test_polygon_area_is_exact_in_projected_crs(
    service: MeasurementService,
) -> None:
    """A 1000 m x 1000 m grid square in UTM measures exactly 1e6 m2."""
    square = box(500_000, 4_000_000, 501_000, 4_001_000)

    result = service.measure_geometry(square, UTM33)

    assert result.status == MeasurementStatus.MEASURED
    assert isinstance(result.measurement, AreaMeasurement)
    assert result.measurement.area == pytest.approx(1_000_000.0, rel=1e-9)
    assert result.measurement.unit == "square_meters"
    assert result.measurement_crs == UTM33


def test_rectangle_area_mathematical(service: MeasurementService) -> None:
    """A 300 x 400 m rectangle measures 120000 m2."""
    rectangle = box(0, 0, 300, 400)

    result = service.measure_geometry(rectangle, UTM33)

    assert result.measurement.area == pytest.approx(120_000.0, rel=1e-9)


def test_triangle_area_mathematical(service: MeasurementService) -> None:
    """A 1000 x 500 m right triangle measures exactly half: 250000 m2."""
    triangle = Polygon([(0, 0), (1000, 0), (0, 500)])

    result = service.measure_geometry(triangle, UTM33)

    assert result.measurement.area == pytest.approx(250_000.0, rel=1e-9)


def test_multipolygon_area_is_the_sum(service: MeasurementService) -> None:
    """Two 10 x 10 m squares total 200 m2."""
    multi = MultiPolygon([box(0, 0, 10, 10), box(100, 0, 110, 10)])

    result = service.measure_geometry(multi, UTM33)

    assert result.measurement.area == pytest.approx(200.0, rel=1e-9)
    assert result.measurement.unit == "square_meters"


def test_linestring_length_is_exact_345(service: MeasurementService) -> None:
    """The 3-4-5 hypotenuse measures exactly 500 m."""
    line = LineString([(0, 0), (300, 400)])

    result = service.measure_geometry(line, UTM33)

    assert result.status == MeasurementStatus.MEASURED
    assert isinstance(result.measurement, LengthMeasurement)
    assert result.measurement.length == pytest.approx(500.0, rel=1e-9)
    assert result.measurement.unit == "meters"
    assert result.measurement_crs == UTM33


def test_multilinestring_length_is_the_sum(
    service: MeasurementService,
) -> None:
    """500 m + 1000 m segments total 1500 m."""
    multi = MultiLineString(
        [
            [(0, 0), (300, 400)],  # 500 m hypotenuse
            [(0, 0), (1000, 0)],  # 1000 m
        ]
    )

    result = service.measure_geometry(multi, UTM33)

    assert result.measurement.length == pytest.approx(1_500.0, rel=1e-9)


def test_linear_ring_length_mathematical(
    service: MeasurementService,
) -> None:
    """A 300-400-500 closed ring measures 1200 m."""
    ring = LinearRing([(0, 0), (300, 0), (300, 400)])

    result = service.measure_geometry(ring, UTM33)

    assert result.status == MeasurementStatus.MEASURED
    assert result.measurement.length == pytest.approx(1_200.0, rel=1e-9)


# ---------------------------------------------- geographic transformation


def test_geographic_polygon_is_transformed_then_measured(
    service: MeasurementService,
) -> None:
    """A 0.01 deg square at the equator measures ~1.113 km per side."""
    degree_square = box(-0.005, -0.005, 0.005, 0.005)

    result = service.measure_geometry(degree_square, "EPSG:4326")

    assert result.status == MeasurementStatus.MEASURED
    # 0.01 deg lon at equator ≈ 1113.19 m → area ≈ 1.239e6 m2.
    assert result.measurement.area == pytest.approx(
        1_113.19**2, rel=0.01
    )
    assert result.measurement.unit == "square_meters"
    # Measured in a projected UTM CRS, never in degrees.
    assert result.measurement_crs.startswith("EPSG:326")
    assert result.measurement.area > 1  # nowhere near "0.0001 deg2"


def test_geographic_linestring_is_transformed_then_measured(
    service: MeasurementService,
) -> None:
    """0.01 deg of longitude at the equator is ~1113 m."""
    line = LineString([(0, 0), (0.01, 0)])

    result = service.measure_geometry(line, "EPSG:4326")

    assert result.status == MeasurementStatus.MEASURED
    assert result.measurement.length == pytest.approx(1_113.19, rel=0.01)
    assert result.measurement.unit == "meters"


def test_service_never_measures_in_geographic_degrees(
    service: MeasurementService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the CRS service misbehaves, degrees are never measured.

    The CRS collaborator is forced to return a geographic measurement
    CRS; the measurement service must refuse instead of computing.
    """
    forced = CRSService()
    real_prepare = forced.prepare_for_measurement

    def lying_prepare(geometry, source_crs, *, purpose="area"):
        context = real_prepare(geometry, source_crs, purpose=purpose)
        return type(context)(
            geometry=context.geometry,  # geometry left in degrees
            source_crs=context.source_crs,
            measurement_crs="EPSG:4326",  # geographic — the lie
            strategy=context.strategy,
            was_transformed=context.was_transformed,
        )

    monkeypatch.setattr(forced, "prepare_for_measurement", lying_prepare)
    lying_service = MeasurementService(crs_service=forced)

    result = lying_service.measure_geometry(
        box(-0.01, -0.01, 0.01, 0.01), "EPSG:4326"
    )

    assert result.status == MeasurementStatus.ERROR
    assert result.measurement is None
    assert "degrees" in result.reason


# --------------------------------------------------- points & null results


def test_point_returns_null_measurement(service: MeasurementService) -> None:
    """Points produce exactly the spec shape: measurement null."""
    result = service.measure_geometry(Point(500_000, 4_000_000), UTM33)

    assert result.status == MeasurementStatus.NOT_REQUIRED
    assert result.measurement is None
    payload = json.loads(result.model_dump_json())
    assert payload["measurement"] is None


def test_multipoint_returns_null_measurement(
    service: MeasurementService,
) -> None:
    """MultiPoint behaves like Point: no measurement."""
    result = service.measure_geometry(
        MultiPoint([(0, 0), (1, 1)]), UTM33
    )

    assert result.status == MeasurementStatus.NOT_REQUIRED
    assert result.measurement is None


def test_point_requires_no_crs(service: MeasurementService) -> None:
    """A point is null regardless of CRS — even undefined."""
    result = service.measure_geometry(Point(1, 2), None)

    assert result.status == MeasurementStatus.NOT_REQUIRED
    assert result.measurement is None


# ------------------------------------------- graceful degradation per case


def test_unsupported_geometry_type_skipped(
    service: MeasurementService,
) -> None:
    """Geometry types outside the known families skip, not raise.

    Shapely itself only emits the families handled above, so the
    unsupported branch is exercised with a stand-in geometry carrying
    an unknown ``geom_type`` (defensive for other geometry libraries).
    """

    class _UnknownGeometry:
        geom_type = "CircularString"
        is_empty = False
        is_valid = True

    result = service.measure_geometry(_UnknownGeometry(), UTM33)  # type: ignore[arg-type]

    assert result.status == MeasurementStatus.SKIPPED_UNSUPPORTED_TYPE
    assert result.measurement is None
    assert "CircularString" in result.reason


def test_empty_polygon_skipped(service: MeasurementService) -> None:
    """Empty geometries skip gracefully."""
    result = service.measure_geometry(Polygon(), UTM33)

    assert result.status == MeasurementStatus.SKIPPED_EMPTY
    assert result.measurement is None


def test_invalid_geometry_skipped(service: MeasurementService) -> None:
    """Self-intersecting bowties skip rather than report a bogus area."""
    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])

    result = service.measure_geometry(bowtie, UTM33)

    assert result.status == MeasurementStatus.SKIPPED_INVALID
    assert result.measurement is None
    assert "invalid" in result.reason


def test_missing_geometry_skipped(service: MeasurementService) -> None:
    """None geometry skips with its own status."""
    result = service.measure_geometry(None, UTM33)

    assert result.status == MeasurementStatus.SKIPPED_MISSING_GEOMETRY
    assert result.measurement is None


def test_missing_crs_skipped_not_assumed(service: MeasurementService) -> None:
    """No CRS → skipped_no_crs carrying Phase 9's explicit refusal."""
    square = box(0, 0, 1000, 1000)

    result = service.measure_geometry(square, None)

    assert result.status == MeasurementStatus.SKIPPED_NO_CRS
    assert result.measurement is None
    assert "EPSG:4326" in result.reason  # the refusal text


def test_invalid_crs_skipped(service: MeasurementService) -> None:
    """Unparseable CRS metadata becomes a skip, not a pyproj error."""
    square = box(0, 0, 1, 1)

    result = service.measure_geometry(square, "not-a-real-crs")

    assert result.status == MeasurementStatus.SKIPPED_INVALID_CRS
    assert result.measurement is None


# ------------------------------------------------------------- file level


def test_one_bad_feature_does_not_abort_the_file(
    service: MeasurementService,
) -> None:
    """A mixed file measures its good features and reports the rest."""
    features = [
        _feature(0, box(0, 0, 10, 10), UTM33),  # exact: 100 m2
        _feature(1, Point(1, 1), UTM33),  # null
        _feature(2, Polygon(), UTM33),  # empty skip
        _feature(3, Polygon([(0, 0), (2, 2), (2, 0), (0, 2)]), UTM33),  # invalid
        _feature(4, GeometryCollection([Point(0, 0)]), UTM33),  # points-only → null
        _feature(5, box(0, 0, 20, 20), None),  # no CRS skip
        _feature(6, box(0, 0, 5, 5), UTM33),  # exact: 25 m2
    ]
    geospatial_file = GeospatialFile(
        filename="mixed.bin",
        file_type="shapefile_zip",
        source_crs=CRSInformation(authority_code=UTM33),
        features=features,
    )

    results = service.measure_file(geospatial_file)

    assert len(results) == 7  # one entry per feature, order preserved
    assert [r.feature_id for r in results] == list(range(7))
    assert results[0].measurement.area == pytest.approx(100.0, rel=1e-9)
    assert results[1].status == MeasurementStatus.NOT_REQUIRED
    assert results[2].status == MeasurementStatus.SKIPPED_EMPTY
    assert results[3].status == MeasurementStatus.SKIPPED_INVALID
    assert results[4].status == MeasurementStatus.NOT_REQUIRED
    assert results[5].status == MeasurementStatus.SKIPPED_NO_CRS
    assert results[6].measurement.area == pytest.approx(25.0, rel=1e-9)
    assert results[6].measurement_crs == UTM33


def test_corrupt_wkt_is_isolated_per_feature(
    service: MeasurementService,
) -> None:
    """A feature whose WKT cannot be parsed errors without raising."""
    broken = Feature(
        feature_id=0,
        geometry_type="Polygon",
        geometry=GeometryInformation(
            geometry_type="Polygon", wkt="POLYGON ((nonsense"
        ),
        source_crs=CRSInformation(authority_code=UTM33),
    )
    file_model = GeospatialFile(
        filename="broken.kml",
        file_type="kml",
        source_crs=CRSInformation(authority_code=UTM33),
        features=[broken],
    )

    results = service.measure_file(file_model)

    assert results[0].status == MeasurementStatus.ERROR
    assert "WKT" in results[0].reason
    assert results[0].measurement is None


def test_response_json_shapes_match_spec(
    service: MeasurementService,
) -> None:
    """Serialized results match the documented response shapes."""
    polygon = service.measure_geometry(box(0, 0, 10, 10), UTM33)
    line = service.measure_geometry(LineString([(0, 0), (300, 400)]), UTM33)
    point = service.measure_geometry(Point(1, 1), UTM33)

    polygon_payload = json.loads(polygon.model_dump_json())
    line_payload = json.loads(line.model_dump_json())
    point_payload = json.loads(point.model_dump_json())

    assert polygon_payload["measurement"] == {
        "area": polygon.measurement.area,
        "unit": "square_meters",
    }
    assert line_payload["measurement"] == {
        "length": line.measurement.length,
        "unit": "meters",
    }
    assert point_payload["measurement"] is None
    assert polygon_payload["measurement_crs"] == UTM33


def test_feature_measurement_serializes_cleanly(
    service: MeasurementService,
) -> None:
    """Every result is JSON round-trippable (API-ready)."""
    result = service.measure_geometry(box(0, 0, 1, 1), UTM33)

    round_tripped = FeatureMeasurement.model_validate_json(
        result.model_dump_json()
    )
    assert round_tripped.status == MeasurementStatus.MEASURED
    assert isinstance(round_tripped.measurement, AreaMeasurement)
