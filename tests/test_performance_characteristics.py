"""Deterministic performance-characterism tests (Phase 19).

These assert *algorithmic* behavior — how often expensive operations
run — instead of wall-clock timing, so they can never flake:

- CRS parsing and pyproj transformer construction are memoized per
  run, so a uniform N-feature file builds exactly one transformer
  instead of one per feature.
- The memoization caches are bounded, so a pathological run (a
  distinct projection per feature) cannot grow memory without limit.
- A cache hit returns bit-identical coordinates to a fresh build.
"""

from __future__ import annotations

import pytest
import shapely
from pyproj import CRS, Transformer
from shapely.geometry import Point, box

from app.geo.crs import CRSService, crs_information
from app.measurements.service import MeasurementService
from app.schemas.geospatial import (
    Feature,
    GeospatialFile,
    GeometryInformation,
    Properties,
)
from app.schemas.measurement import MeasurementStatus


def _uniform_geofile(feature_count: int) -> GeospatialFile:
    """Build a synthetic geographic file whose features share one zone.

    All polygons sit in the same UTM zone, so every feature selects the
    same measurement CRS — the "uniform dataset" worst case for
    repeated transformer construction.

    Args:
        feature_count: Number of polygon features to generate.

    Returns:
        The normalized file ready for ``measure_file``.
    """
    source = crs_information("EPSG:4326")
    features: list[Feature] = []
    for index in range(feature_count):
        geometry = box(
            12.0 + (index % 10) * 0.001,
            41.0 + (index // 10) * 0.001,
            12.0009 + (index % 10) * 0.001,
            41.0009 + (index // 10) * 0.001,
        )
        features.append(
            Feature(
                feature_id=index,
                geometry_type="Polygon",
                geometry=GeometryInformation(
                    geometry_type="Polygon",
                    wkt=geometry.wkt,
                    bbox=tuple(geometry.bounds),
                    is_valid=True,
                    is_empty=False,
                ),
                source_crs=source,
                properties=Properties({}),
            )
        )
    return GeospatialFile(
        filename="bench.kml",
        file_type="kml",
        source_crs=source,
        features=features,
    )


def test_transformer_built_once_per_crs_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A uniform 50-feature run builds exactly one transformer.

    Before memoization this was one ``Transformer.from_crs`` per
    feature (~0.19 ms each — the dominant per-feature cost).
    """
    geofile = _uniform_geofile(50)
    builds = 0
    original = Transformer.from_crs

    def counting(*args: object, **kwargs: object) -> object:
        nonlocal builds
        builds += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(Transformer, "from_crs", staticmethod(counting))

    results = MeasurementService().measure_file(geofile)

    assert all(r.status is MeasurementStatus.MEASURED for r in results)
    assert builds == 1


def test_crs_parse_count_does_not_scale_with_features(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CRS parsing happens once per distinct string, not per feature.

    Before memoization each feature parsed six CRS strings
    (``6 × N`` — 300 parses for 50 features).
    """
    geofile = _uniform_geofile(50)
    parses = 0
    original = CRS.from_user_input

    def counting(*args: object, **kwargs: object) -> object:
        nonlocal parses
        parses += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(CRS, "from_user_input", staticmethod(counting))

    results = MeasurementService().measure_file(geofile)

    assert all(r.status is MeasurementStatus.MEASURED for r in results)
    # Source + target only; every later feature hits the memo. The
    # generous bound tolerates a couple of extra legitimate parse
    # sites while failing hard on any per-feature parsing regression.
    assert parses <= 10


def test_transformer_cache_stays_bounded() -> None:
    """Many distinct projections cannot grow the caches unboundedly."""
    service = CRSService()
    point = Point(12.5, 41.9)

    for index in range(100):
        target = (
            f"+proj=aeqd +lat_0={40.0 + index * 0.1:.1f} "
            f"+lon_0={10.0 + index * 0.1:.1f} "
            "+datum=WGS84 +units=m +no_defs"
        )
        service.transform_geometry(point, "EPSG:4326", target)

    # Pathological input (100 distinct targets): the cache stops
    # growing at its cap and later builds are simply not retained.
    assert len(service._transformer_cache) <= (
        service._MAX_CACHED_TRANSFORMERS
    )
    assert len(service._parsed_cache) <= service._MAX_CACHED_CRS


def test_cached_transformer_matches_fresh_build() -> None:
    """A cache hit returns exactly what a fresh build computes."""
    service = CRSService()
    geometry = box(12.4, 41.8, 12.6, 42.0)

    first = service.transform_geometry(geometry, "EPSG:4326", "EPSG:32632")
    cached = service.transform_geometry(geometry, "EPSG:4326", "EPSG:32632")
    fresh = CRSService().transform_geometry(
        geometry, "EPSG:4326", "EPSG:32632"
    )

    # Bit-for-bit identical coordinate output (WKB comparison).
    expected = shapely.to_wkb(first)
    assert shapely.to_wkb(cached) == expected
    assert shapely.to_wkb(fresh) == expected


def test_measured_values_identical_across_cold_and_warm_runs() -> None:
    """Re-running on the same service (warm cache) changes no numbers."""
    geofile = _uniform_geofile(25)
    service = MeasurementService()

    cold = service.measure_file(geofile)
    warm = service.measure_file(geofile)  # same instance → warm caches

    for first, second in zip(cold, warm, strict=True):
        assert first.status is second.status
        assert first.measurement == second.measurement
        assert first.measurement_crs == second.measurement_crs
