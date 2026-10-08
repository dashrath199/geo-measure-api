"""Tests for the CRS handling service.

Required behaviors: EPSG:4326 gets transformed, projected CRS stays
usable, coordinate units change appropriately, invalid CRS is handled
gracefully — plus the documented edge cases: missing CRS, hemispheres,
UTM-boundary crossing, zone limits, global extents, antimeridian and
polar geometries, and the invariant that every measurement CRS is
projected and metric.
"""

from __future__ import annotations

import pytest
import pyproj
from pyproj import CRS
from shapely.geometry import Point, box

from app.exceptions import CRSError, InvalidCRSError, MissingCRSError
from app.geo.crs import CRSService, crs_information
from app.schemas.geospatial import CRSInformation


@pytest.fixture
def service() -> CRSService:
    """Provide a CRS service instance."""
    return CRSService()


# ------------------------------------------------------ required behaviors


def test_epsg4326_gets_transformed(service: CRSService) -> None:
    """A geographic source is re-projected to a projected UTM CRS."""
    geometry = box(-0.01, -0.01, 0.01, 0.01)

    context = service.prepare_for_measurement(geometry, "EPSG:4326")

    assert context.was_transformed is True
    assert context.strategy == "utm"
    assert context.measurement_crs.startswith("EPSG:326")  # northern UTM
    assert CRS.from_user_input(context.measurement_crs).is_projected
    assert context.geometry is not geometry


def test_projected_crs_remains_usable(service: CRSService) -> None:
    """An already-metric projected CRS is used as-is (identity)."""
    geometry = Point(500_000, 4_000_000)

    context = service.prepare_for_measurement(geometry, "EPSG:32633")

    assert context.was_transformed is False
    assert context.strategy == "source_projected"
    assert context.measurement_crs == "EPSG:32633"
    assert context.geometry is geometry


def test_transformation_changes_coordinate_units(service: CRSService) -> None:
    """Degrees become metres: same extent, physical magnitudes."""
    # 0.01° of longitude at the equator ≈ 1 113 m.
    degrees_box = box(-0.005, -0.005, 0.005, 0.005)
    assert degrees_box.bounds[2] - degrees_box.bounds[0] < 1  # degrees

    context = service.prepare_for_measurement(degrees_box, "EPSG:4326")

    minx, _, maxx, _ = context.geometry.bounds
    width_metres = maxx - minx
    assert 500 < width_metres < 2_000  # now metres, ~1.1 km


def test_invalid_crs_is_handled_gracefully(service: CRSService) -> None:
    """Unparseable CRS metadata raises a domain error, not pyproj's."""
    geometry = box(0, 0, 1, 1)

    with pytest.raises(InvalidCRSError) as excinfo:
        service.prepare_for_measurement(geometry, "definitely-not-a-crs")

    # Domain exception (mapped to HTTP), wrapping pyproj's error.
    assert isinstance(excinfo.value, CRSError)
    assert "definitely-not-a-crs" in str(excinfo.value)
    # pyproj's own exception must not leak through the service boundary.
    assert not isinstance(excinfo.value, pyproj.exceptions.CRSError)


# -------------------------------------------------------------- edge cases


def test_missing_crs_is_explicit_and_never_assumed(service: CRSService) -> None:
    """Absent CRS raises; the message states no default is assumed."""
    geometry = box(0, 0, 1, 1)

    with pytest.raises(MissingCRSError, match="EPSG:4326"):
        service.prepare_for_measurement(geometry, None)

    with pytest.raises(MissingCRSError):
        service.prepare_for_measurement(geometry, "   ")


def test_empty_geometry_rejected(service: CRSService) -> None:
    """Empty geometries have no extent to project."""
    from shapely.geometry import Polygon

    with pytest.raises(CRSError, match="empty"):
        service.prepare_for_measurement(Polygon(), "EPSG:4326")


def test_northern_hemisphere_utm_zone(service: CRSService) -> None:
    """Central Europe lands in zone 32 north (EPSG:32632)."""
    target = service.select_measurement_crs(
        "EPSG:4326", (9.5, 49.5, 10.5, 50.5)
    )

    assert target == "EPSG:32632"


def test_southern_hemisphere_utm_zone(service: CRSService) -> None:
    """Southern latitudes select the 327xx (south) series."""
    target = service.select_measurement_crs(
        "EPSG:4326", (9.5, -30.5, 10.5, -29.5)
    )

    assert target == "EPSG:32732"


def test_equator_crossing_picks_hemisphere_by_centre(
    service: CRSService,
) -> None:
    """A geometry straddling the equator uses its centre latitude."""
    # Centre latitude is -0.5° → southern hemisphere.
    target = service.select_measurement_crs(
        "EPSG:4326", (-1.0, -2.0, 1.0, 1.0)
    )

    assert target.startswith("EPSG:327")


def test_geometry_crossing_utm_boundary_still_uses_utm(
    service: CRSService,
) -> None:
    """A small geometry crossing 6°E is measured in its centre zone."""
    crossing = box(4.5, 50.0, 7.5, 51.0)  # spans the 6°E zone edge

    context = service.prepare_for_measurement(crossing, "EPSG:4326")

    assert context.strategy == "utm"
    # Centre lon 6.0 → zone 32 (boundary belongs to the eastern zone).
    assert context.measurement_crs == "EPSG:32632"


def test_zone_edge_is_deterministic(service: CRSService) -> None:
    """A centre exactly on a zone edge resolves deterministically."""
    # lon 6.0 is the first meridian of zone 32.
    assert (
        service.select_measurement_crs("EPSG:4326", (5.9, 40.0, 6.1, 40.2))
        == "EPSG:32632"
    )
    # lon just below belongs to zone 31.
    assert (
        service.select_measurement_crs("EPSG:4326", (5.0, 40.0, 5.9, 40.2))
        == "EPSG:32631"
    )


def test_wide_geometry_not_single_utm_area(service: CRSService) -> None:
    """A 20°-wide extent uses the global equal-area CRS for areas."""
    target = service.select_measurement_crs(
        "EPSG:4326", (0.0, 40.0, 20.0, 50.0), purpose="area"
    )

    assert target == "EPSG:6933"


def test_wide_geometry_uses_local_aeqd_for_length(
    service: CRSService,
) -> None:
    """Lengths of large extents use a local azimuthal-equidistant CRS."""
    target = service.select_measurement_crs(
        "EPSG:4326", (0.0, 40.0, 20.0, 50.0), purpose="length"
    )

    assert "aeqd" in target
    assert CRS.from_user_input(target).is_projected


def test_global_geometry_uses_equal_area(service: CRSService) -> None:
    """World-extent geometry never gets a UTM zone."""
    world = box(-180, -85, 180, 85)

    target = service.select_measurement_crs("EPSG:4326", world.bounds)

    assert target == "EPSG:6933"


def test_antimeridian_spanning_not_split_into_wrong_zone(
    service: CRSService,
) -> None:
    """A bbox spanning the antimeridian is 'large', not zone-garbage."""
    nearly_global = box(-179, -1, 179, 1)

    target = service.select_measurement_crs(
        "EPSG:4326", nearly_global.bounds
    )

    assert target == "EPSG:6933"


def test_polar_geometry_outside_utm_band(service: CRSService) -> None:
    """Beyond 84°N UTM is invalid; the large-geometry path is used."""
    polar = box(10.0, 84.5, 11.0, 85.5)

    target = service.select_measurement_crs(
        "EPSG:4326", polar.bounds, purpose="length"
    )

    assert "aeqd" in target
    assert not target.startswith("EPSG:326")


def test_non_metric_projected_crs_becomes_metric(
    service: CRSService,
) -> None:
    """A US-feet projected CRS is converted to a metric selection."""
    # EPSG:2225 = NAD83 / California zone 2 (ftUS).
    geometry = box(1_800_000, 600_000, 1_810_000, 610_000)

    context = service.prepare_for_measurement(geometry, "EPSG:2225")

    assert context.was_transformed is True
    assert context.measurement_crs != "EPSG:2225"
    target = CRS.from_user_input(context.measurement_crs)
    assert target.is_projected
    assert target.axis_info[0].unit_name.lower() in {"metre", "meter"}


def test_all_selected_crs_are_projected_and_metric(
    service: CRSService,
) -> None:
    """Invariant: no selection ever comes back geographic or in feet."""
    cases = [
        ("EPSG:4326", (-0.01, -0.01, 0.01, 0.01), "area"),
        ("EPSG:4326", (0.0, 40.0, 20.0, 50.0), "area"),
        ("EPSG:4326", (0.0, 40.0, 20.0, 50.0), "length"),
        ("EPSG:4326", (-180.0, -85.0, 180.0, 85.0), "area"),
        ("EPSG:32633", (500_000, 4_000_000, 501_000, 4_001_000), "area"),
        ("EPSG:4326", (100.0, -55.0, 101.0, -54.0), "length"),
    ]
    for source, bounds, purpose in cases:
        target = CRS.from_user_input(
            service.select_measurement_crs(
                source, bounds, purpose=purpose  # type: ignore[arg-type]
            )
        )
        assert target.is_projected, (source, bounds, purpose)
        assert target.axis_info[0].unit_name.lower() in {"metre", "meter"}


def test_transform_roundtrip_accuracy(service: CRSService) -> None:
    """UTM round-trip preserves coordinates to sub-millimetre scale."""
    original = Point(12.5, 41.9)

    context = service.prepare_for_measurement(original, "EPSG:4326")
    back = service.transform_geometry(
        context.geometry, context.measurement_crs, "EPSG:4326"
    )

    assert back.x == pytest.approx(original.x, abs=1e-7)
    assert back.y == pytest.approx(original.y, abs=1e-7)


def test_classification_helpers(service: CRSService) -> None:
    """Geographic/projected detection works for both families."""
    assert service.is_geographic("EPSG:4326") is True
    assert service.is_geographic("EPSG:32633") is False

    info = crs_information("EPSG:4326")
    assert isinstance(info, CRSInformation)
    assert info.is_geographic is True
    assert crs_information(None).is_defined is False


def test_select_rejects_unsupported_crs_type(service: CRSService) -> None:
    """Only geodetic (geographic/projected) CRS can be measurement sources."""
    vertical = CRS.from_user_input("EPSG:5703")  # vertical-only CRS
    assert vertical.is_geographic is False
    assert vertical.is_projected is False

    with pytest.raises(InvalidCRSError, match="neither geographic"):
        service.select_measurement_crs("EPSG:5703", (0.0, 0.0, 1.0, 1.0))
