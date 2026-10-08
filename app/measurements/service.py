"""Measurement engine: planar area and length of projected geometries.

Responsibility boundary
-----------------------
CRS detection, selection and transformation belong **exclusively** to
:class:`~app.geo.crs.CRSService`. This service never chooses or builds
a projection: it asks the CRS service to prepare a geometry and then
computes Shapely's planar values on the result — plus one defensive
check that the returned measurement CRS is not geographic, so a value in
degrees can never be reported as a physical measurement.

Rules
-----
=================== ============================ ==================
Geometry            Result                       Unit
=================== ============================ ==================
Polygon             area                         square_meters
MultiPolygon        area                         square_meters
LineString          length                       meters
LinearRing          length                       meters
MultiLineString     length                       meters
GeometryCollection  area (areal parts present)  square_meters
                    …else length (linear parts)  meters
                    …points only → ``null``      —
Point / MultiPoint  no measurement (``null``)    —
Anything else       skipped (unsupported)       —
=================== ============================ ==================

Collections are flattened recursively (nested collections included):
areal parts take precedence over linear parts, and linear parts in a
mixed collection are reported in the result's ``reason`` because they
don't contribute to an area figure. Empty, invalid, geometry-less,
CRS-less and unparseable features are skipped with an explicit status —
one bad feature never aborts a file.
"""

from __future__ import annotations

import logging

from shapely.geometry.base import BaseGeometry
from shapely.wkt import loads as load_wkt

from app.exceptions import CRSError, InvalidCRSError, MissingCRSError
from app.geo.crs import CRSService, MeasurementPurpose
from app.schemas.geospatial import Feature, GeospatialFile
from app.schemas.measurement import (
    AreaMeasurement,
    FeatureMeasurement,
    LengthMeasurement,
    MeasurementStatus,
)

logger = logging.getLogger(__name__)

AREA_UNIT = "square_meters"
LENGTH_UNIT = "meters"

_AREA_TYPES = frozenset({"Polygon", "MultiPolygon"})
_LENGTH_TYPES = frozenset({"LineString", "LinearRing", "MultiLineString"})
_POINT_TYPES = frozenset({"Point", "MultiPoint"})


def _flatten_parts(geometry: BaseGeometry) -> list[BaseGeometry]:
    """Recursively flatten a GeometryCollection into its leaf parts.

    Args:
        geometry: The geometry to flatten (may nest collections).

    Returns:
        Non-collection parts in document order.
    """
    if geometry.geom_type == "GeometryCollection":
        parts: list[BaseGeometry] = []
        for member in geometry.geoms:
            parts.extend(_flatten_parts(member))
        return parts
    return [geometry]


def _classify_parts(parts: list[BaseGeometry]) -> str:
    """Decide what a flattened collection can be measured as.

    Args:
        parts: Flattened leaf geometries.

    Returns:
        ``"area"`` when any areal part exists, ``"length"`` when any
        linear part exists, ``"points"`` when only point parts exist, or
        ``"unsupported"`` when nothing classifiable is present.
    """
    kinds = {part.geom_type for part in parts}
    if kinds & _AREA_TYPES:
        return "area"
    if kinds & _LENGTH_TYPES:
        return "length"
    if kinds and kinds <= _POINT_TYPES:
        return "points"
    return "unsupported"


def _linear_length(geometry: BaseGeometry) -> float:
    """Sum the lengths of a collection's linear parts only.

    Polygon perimeters are deliberately excluded: a collection's
    ``.length`` includes them, which would corrupt a length figure.

    Args:
        geometry: A (possibly nested) geometry.

    Returns:
        Total length of all linear parts in CRS units.
    """
    if geometry.geom_type in _LENGTH_TYPES:
        return float(geometry.length)
    if geometry.geom_type == "GeometryCollection":
        return sum(_linear_length(part) for part in geometry.geoms)
    return 0.0


def measurement_kind(geometry: BaseGeometry | None) -> str:
    """Decision table: which measurement (if any) is *meaningful*.

    Public so the supported-geometry matrix is inspectable and testable
    on its own. For :class:`GeometryCollection` the contained geometries
    are inspected (flattened, nested collections included):

    - any areal part   → ``"area"`` (linear parts don't affect area)
    - else any linear  → ``"length"`` (points don't affect length)
    - only points      → ``"none"`` (nothing meaningful to compute)
    - nothing usable   → ``"unsupported"``

    Args:
        geometry: The geometry to classify (may be ``None``).

    Returns:
        One of ``"area"``, ``"length"``, ``"none"`` or
        ``"unsupported"``.
    """
    if geometry is None or geometry.is_empty:
        return "none"
    geom_type = geometry.geom_type
    if geom_type in _AREA_TYPES:
        return "area"
    if geom_type in _LENGTH_TYPES:
        return "length"
    if geom_type in _POINT_TYPES:
        return "none"
    if geom_type == "GeometryCollection":
        return {
            "area": "area",
            "length": "length",
            "points": "none",
            "unsupported": "unsupported",
        }[_classify_parts(_flatten_parts(geometry))]
    return "unsupported"


class MeasurementService:
    """Computes area/length for normalized features.

    Args:
        crs_service: CRS collaborator; a fresh :class:`CRSService` is
            created when omitted. Injected so tests can substitute it.
    """

    def __init__(self, crs_service: CRSService | None = None) -> None:
        """Wire the service to its CRS collaborator.

        Args:
            crs_service: Optional injected CRS service.
        """
        self._crs = crs_service or CRSService()

    @staticmethod
    def _skip(
        feature_id: int,
        status: MeasurementStatus,
        reason: str,
        *,
        geometry_type: str | None = None,
    ) -> FeatureMeasurement:
        """Build a skipped (or errored) result.

        Args:
            feature_id: The feature's index.
            status: Skip/error status.
            reason: Human-readable explanation.
            geometry_type: Geometry type name, when known.

        Returns:
            The result with ``measurement=None``.
        """
        return FeatureMeasurement(
            feature_id=feature_id,
            geometry_type=geometry_type,
            measurement=None,
            status=status,
            reason=reason,
        )

    def measure_geometry(
        self,
        geometry: BaseGeometry | None,
        source_crs: str | None,
        *,
        feature_id: int = 0,
    ) -> FeatureMeasurement:
        """Measure one geometry, isolating every failure.

        Flow: classification (points short-circuit) → validity → CRS
        preparation via :class:`CRSService` → planar measurement with a
        geographic-CRS guard.

        Args:
            geometry: The geometry to measure (may be ``None``).
            source_crs: Source CRS metadata (``None`` → skipped).
            feature_id: Index reported in the result.

        Returns:
            The per-feature :class:`FeatureMeasurement`. Never raises
            for unsupported/empty/invalid/CRS-less inputs.
        """
        if geometry is None:
            return self._skip(
                feature_id,
                MeasurementStatus.SKIPPED_MISSING_GEOMETRY,
                "Feature has no geometry",
            )
        if geometry.is_empty:
            return self._skip(
                feature_id,
                MeasurementStatus.SKIPPED_EMPTY,
                "Geometry is empty",
                geometry_type=geometry.geom_type,
            )

        geom_type = geometry.geom_type
        collection_reason: str | None = None
        kind = measurement_kind(geometry)
        if kind == "none":
            # Points (and points-only collections) need no measurement —
            # and therefore no CRS work.
            is_plain_point = geom_type in _POINT_TYPES
            return FeatureMeasurement(
                feature_id=feature_id,
                geometry_type=geom_type,
                measurement=None,
                status=MeasurementStatus.NOT_REQUIRED,
                reason=(
                    "Point geometries require no measurement"
                    if is_plain_point
                    else "Collection contains only point geometries"
                ),
            )
        if kind == "unsupported":
            return self._skip(
                feature_id,
                MeasurementStatus.SKIPPED_UNSUPPORTED_TYPE,
                f"Geometry type {geom_type!r} is not supported",
                geometry_type=geom_type,
            )
        purpose: MeasurementPurpose = "area" if kind == "area" else "length"
        if kind == "area" and geom_type == "GeometryCollection":
            linear_parts = sum(
                1
                for part in _flatten_parts(geometry)
                if part.geom_type in _LENGTH_TYPES
            )
            if linear_parts:
                collection_reason = (
                    f"Collection contains {linear_parts} linear part(s) "
                    f"which do not contribute to the area figure"
                )

        if not geometry.is_valid:
            return self._skip(
                feature_id,
                MeasurementStatus.SKIPPED_INVALID,
                f"Geometry {geom_type} is invalid (fails OGC validity); "
                f"planar measurement would be misleading",
                geometry_type=geom_type,
            )

        try:
            context = self._crs.prepare_for_measurement(
                geometry, source_crs, purpose=purpose
            )
        except MissingCRSError as exc:
            return self._skip(
                feature_id, MeasurementStatus.SKIPPED_NO_CRS, str(exc)
            )
        except InvalidCRSError as exc:
            return self._skip(
                feature_id, MeasurementStatus.SKIPPED_INVALID_CRS, str(exc)
            )
        except CRSError as exc:
            return self._skip(
                feature_id, MeasurementStatus.ERROR, str(exc)
            )

        # Defense in depth: a geographic measurement CRS would mean the
        # value below is in degrees, not metres. Never report that.
        if self._crs.is_geographic(context.measurement_crs):
            logger.error(
                "CRS service returned a geographic measurement CRS %r "
                "for feature %d — refusing to measure",
                context.measurement_crs,
                feature_id,
            )
            return self._skip(
                feature_id,
                MeasurementStatus.ERROR,
                "Measurement CRS is geographic; refusing to measure "
                "in degrees",
            )

        if purpose == "area":
            # GEOS sums polygonal parts of a collection; lines/points
            # contribute zero area.
            measurement = AreaMeasurement(
                area=context.geometry.area, unit=AREA_UNIT
            )
        else:
            # Collections measure only their linear parts (polygon
            # perimeters must not leak into a length figure).
            length = (
                _linear_length(context.geometry)
                if geom_type == "GeometryCollection"
                else context.geometry.length
            )
            measurement = LengthMeasurement(length=length, unit=LENGTH_UNIT)

        return FeatureMeasurement(
            feature_id=feature_id,
            geometry_type=geom_type,
            measurement=measurement,
            status=MeasurementStatus.MEASURED,
            measurement_crs=context.measurement_crs,
            reason=collection_reason,
        )

    def measure_feature(self, feature: Feature) -> FeatureMeasurement:
        """Measure one normalized :class:`Feature` (WKT-backed).

        Args:
            feature: The domain feature to measure.

        Returns:
            Its measurement result; parse failures become ``ERROR``
            entries rather than exceptions.
        """
        if feature.geometry is None:
            return self._skip(
                feature.feature_id,
                MeasurementStatus.SKIPPED_MISSING_GEOMETRY,
                "Feature has no geometry",
            )
        try:
            geometry = load_wkt(feature.geometry.wkt)
        except Exception as exc:  # noqa: BLE001 - isolated per feature
            return self._skip(
                feature.feature_id,
                MeasurementStatus.ERROR,
                f"Could not parse geometry WKT: {exc}",
            )
        result = self.measure_geometry(
            geometry,
            feature.source_crs.authority_code,
            feature_id=feature.feature_id,
        )
        # Attach the source attributes so the result is self-contained
        # for persistence and the measurements API.
        return result.model_copy(update={"properties": feature.properties})

    def measure_file(
        self, geospatial_file: GeospatialFile
    ) -> list[FeatureMeasurement]:
        """Measure every feature of a file without letting one abort all.

        Each feature is wrapped: unexpected exceptions become ``ERROR``
        results while the remaining features are still measured.

        Args:
            geospatial_file: The normalized file to process.

        Returns:
            One :class:`FeatureMeasurement` per feature, in order.
        """
        results: list[FeatureMeasurement] = []
        for feature in geospatial_file.features:
            try:
                results.append(self.measure_feature(feature))
            except Exception as exc:  # noqa: BLE001 - file-level isolation
                logger.exception(
                    "Measurement failed for feature %d", feature.feature_id
                )
                results.append(
                    self._skip(
                        feature.feature_id,
                        MeasurementStatus.ERROR,
                        f"Unexpected measurement failure: {exc}",
                    )
                )
        measured = sum(
            1
            for r in results
            if r.status == MeasurementStatus.MEASURED
        )
        logger.info(
            "Measured %s: %d/%d feature(s) with values",
            geospatial_file.filename,
            measured,
            len(results),
        )
        return results
