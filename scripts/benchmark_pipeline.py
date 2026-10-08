"""Deterministic pipeline benchmark for the measurement hot path.

Usage::

    python scripts/benchmark_pipeline.py [feature_count]

Builds a synthetic uniform geographic dataset and reports:

- wall-clock time and per-feature cost of ``measure_file``;
- how often the expensive pyproj constructions ran
  (``Transformer.from_crs`` builds and ``CRS.from_user_input`` parses).

Prints numbers only — there are no thresholds, so the script can never
"fail"; use it to compare pipeline changes (e.g., the effect of CRS
memoization) on the same machine.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Allow running the script directly from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pyproj import CRS, Transformer  # noqa: E402

from app.geo.crs import crs_information  # noqa: E402
from app.measurements.service import MeasurementService  # noqa: E402
from app.schemas.geospatial import (  # noqa: E402
    Feature,
    GeospatialFile,
    GeometryInformation,
    Properties,
)
from shapely.geometry import box  # noqa: E402

# Spy counters (mutated by the wrappers below).
_COUNTS = {"transformer_builds": 0, "crs_parses": 0}
_ORIGINAL_TRANSFORMER = Transformer.from_crs
_ORIGINAL_PARSE = CRS.from_user_input


def _counting_transformer(*args: object, **kwargs: object) -> object:
    """Delegate to pyproj, counting construction calls."""
    _COUNTS["transformer_builds"] += 1
    return _ORIGINAL_TRANSFORMER(*args, **kwargs)


def _counting_parse(*args: object, **kwargs: object) -> object:
    """Delegate to pyproj, counting parse calls."""
    _COUNTS["crs_parses"] += 1
    return _ORIGINAL_PARSE(*args, **kwargs)


def build_geofile(feature_count: int) -> GeospatialFile:
    """Create a synthetic uniform geographic dataset.

    Args:
        feature_count: Number of polygon features (same UTM zone).

    Returns:
        The normalized file ready for ``measure_file``.
    """
    source = crs_information("EPSG:4326")
    features: list[Feature] = []
    for index in range(feature_count):
        geometry = box(
            12.0 + (index % 100) * 0.001,
            41.0 + (index // 100) * 0.001,
            12.0009 + (index % 100) * 0.001,
            41.0009 + (index // 100) * 0.001,
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
        filename="benchmark.kml",
        file_type="kml",
        source_crs=source,
        features=features,
    )


def main(feature_count: int) -> int:
    """Run the benchmark and print the report.

    Args:
        feature_count: Number of features to measure.

    Returns:
        Process exit code (``0``).
    """
    geofile = build_geofile(feature_count)
    service = MeasurementService()

    Transformer.from_crs = staticmethod(_counting_transformer)  # type: ignore[method-assign]
    CRS.from_user_input = staticmethod(_counting_parse)  # type: ignore[method-assign]
    try:
        started = time.perf_counter()
        results = service.measure_file(geofile)
        elapsed = time.perf_counter() - started
    finally:
        Transformer.from_crs = _ORIGINAL_TRANSFORMER  # type: ignore[method-assign]
        CRS.from_user_input = _ORIGINAL_PARSE  # type: ignore[method-assign]

    measured = sum(1 for r in results if r.status.value == "measured")
    print(f"features                : {feature_count}")
    print(f"measured                : {measured}")
    print(f"total                   : {elapsed * 1000:.1f} ms")
    print(f"per feature             : {elapsed * 1000 / feature_count:.4f} ms")
    print(f"throughput              : {feature_count / elapsed:,.0f} features/s")
    print(f"Transformer.from_crs    : {_COUNTS['transformer_builds']} build(s)")
    print(f"CRS.from_user_input     : {_COUNTS['crs_parses']} parse(s)")
    return 0


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5_000
    raise SystemExit(main(count))
