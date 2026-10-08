"""CRS handling service: detection, selection and transformation.

Why a projected CRS is required for area and distance calculations
------------------------------------------------------------------
The geographic CRS used for storage (e.g. EPSG:4326) measures coordinates
in **degrees on a curved ellipsoid**, not metres on a plane:

- 1° of latitude ≈ 111.32 km almost everywhere, but 1° of longitude =
  111.32 km · cos(latitude) — 0 km at the poles. The two axes therefore
  have *different, latitude-dependent* physical lengths.
- Square degrees are not square kilometres: an area computed from
  degrees shrinks toward the poles for identical ground area.
- Planar algorithms (Shapely's ``.length`` / ``.area``) assume a
  Euclidean plane. Applied to degree coordinates they silently return
  numbers in degrees / square degrees — physically meaningless values
  that also distort shapes away from the equator.

A projected CRS maps a region of the ellipsoid onto a plane in **metres**
with bounded, documented distortion (UTM's transverse-Mercator scale
error is under ~0.1 % within its zone), so planar measurements become
physically meaningful. This service guarantees no geometry is ever
measured while its CRS is geographic.

Selection strategy and its assumptions
--------------------------------------
- **Regional geometries** (bbox ≤ 8° lon × 12° lat, within UTM's valid
  latitude band 80°S–84°N) → the UTM zone of the bbox *centre*:
  ``EPSG:326xx`` (north) or ``EPSG:327xx`` (south); hemisphere chosen by
  the centre latitude. A geometry crossing a zone boundary but within
  that tolerance is measured in the centre zone — distortion grows with
  distance from the central meridian (typically ≲ 0.1 %).
- **Large / multi-zone / polar / antimeridian-spanning geometries** →
  no single UTM zone is honest: area uses the global equal-area
  projection ``EPSG:6933``; length uses a local azimuthal-equidistant
  projection centred on the geometry (``+proj=aeqd``). Distances far
  from the centre of an AEQD world view are inherently distorted — that
  is a property of the Earth, not of this code.
- **Already-projected, metric CRS** → used as-is (identity).
- **Projected but non-metric** (e.g. US survey feet) → re-projected to a
  metric selection via geographic bounds.
- **Missing CRS** → :class:`MissingCRSError`; **never** silently
  assumed to be EPSG:4326. **Unparseable CRS** → :class:`InvalidCRSError`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Literal

import pyproj
from pyproj import CRS, Transformer
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from app.exceptions import (
    CRSError,
    EmptyGeometryError,
    InvalidCRSError,
    MissingCRSError,
)
from app.geo.readers import normalize_crs
from app.schemas.geospatial import CRSInformation

logger = logging.getLogger(__name__)

# What the measurement will be used for (drives the large-geometry pick).
MeasurementPurpose = Literal["area", "length"]

# Single-UTM eligibility: roughly one UTM zone of slack (a zone is 6°).
_MAX_UTM_WIDTH_DEG = 8.0
_MAX_UTM_HEIGHT_DEG = 12.0
# UTM is defined between these latitudes.
_UTM_MIN_LAT = -80.0
_UTM_MAX_LAT = 84.0
# Global equal-area CRS for area of large extents (metres).
_GLOBAL_EQUAL_AREA_CRS = "EPSG:6933"
_WGS84 = "EPSG:4326"


@dataclass(frozen=True)
class MeasurementContext:
    """Result of preparing one geometry for measurement.

    Attributes:
        geometry: The geometry expressed in ``measurement_crs``.
        source_crs: Normalized source CRS string.
        measurement_crs: Projected, metric CRS the geometry now uses
            (authority code or PROJ string).
        strategy: How the target was chosen (``utm``,
            ``global_equal_area``, ``local_aeqd``,
            ``source_projected``, ``reprojected_metric``).
        was_transformed: Whether coordinates were actually re-projected.
    """

    geometry: BaseGeometry
    source_crs: str
    measurement_crs: str
    strategy: str
    was_transformed: bool


def crs_information(crs: str | None) -> CRSInformation:
    """Describe a source CRS string without ever inventing one.

    Args:
        crs: Authority string (``"EPSG:4326"``), any pyproj-parseable
            form, or ``None`` when the source defines no CRS.

    Returns:
        A :class:`CRSInformation`; for ``None`` every field stays
        ``None`` (unknown), and unparseable strings are kept verbatim as
        ``authority_code`` so nothing is lost.
    """
    if crs is None:
        return CRSInformation()

    try:
        parsed = CRS.from_user_input(crs)
    except Exception:  # noqa: BLE001 - unknown input → preserve verbatim
        logger.debug("Unparseable CRS string preserved as-is: %r", crs)
        return CRSInformation(authority_code=crs)

    authority = parsed.to_authority()
    authority_code = (
        f"{authority[0]}:{authority[1]}" if authority else parsed.to_string()
    )
    return CRSInformation(
        authority_code=authority_code,
        name=parsed.name,
        is_geographic=parsed.is_geographic,
        is_projected=parsed.is_projected,
    )


def _is_metric(crs: CRS) -> bool:
    """Report whether a CRS's horizontal axis unit is the metre.

    Args:
        crs: The parsed CRS.

    Returns:
        ``True`` when the first axis unit is metre(s).
    """
    if not crs.axis_info:
        return False
    return crs.axis_info[0].unit_name.lower() in {"metre", "meter", "m"}


def _code(crs: CRS) -> str:
    """Render a CRS as its authority code when possible.

    Args:
        crs: The parsed CRS.

    Returns:
        ``"EPSG:x"``-style code, else the CRS's string form.
    """
    authority = crs.to_authority()
    if authority:
        return f"{authority[0]}:{authority[1]}"
    return crs.to_string()


class CRSService:
    """Detects, classifies and transforms coordinate reference systems."""

    def parse_source(self, source_crs: str | None) -> CRS:
        """Parse source CRS metadata strictly.

        Args:
            source_crs: CRS string from a reader, or ``None``.

        Returns:
            The parsed :class:`pyproj.CRS`.

        Raises:
            MissingCRSError: When no CRS metadata exists — the explicit,
                never-assume-EPSG:4326 behavior for absent CRS.
            InvalidCRSError: When the metadata cannot be parsed.
        """
        if source_crs is None or not str(source_crs).strip():
            raise MissingCRSError(
                "Source CRS is not defined. Refusing to assume a default "
                "(e.g. EPSG:4326); measurement must be skipped until the "
                "dataset's CRS is known."
            )
        try:
            return CRS.from_user_input(source_crs)
        except pyproj.exceptions.CRSError as exc:
            raise InvalidCRSError(
                f"Source CRS {source_crs!r} is not a valid CRS: {exc}"
            ) from exc

    def is_geographic(self, source_crs: str | None) -> bool:
        """Classify a CRS as geographic (angular units).

        Args:
            source_crs: CRS string or ``None``.

        Returns:
            ``True`` for geographic CRS.

        Raises:
            MissingCRSError: If the CRS is absent.
            InvalidCRSError: If the CRS is unparseable.
        """
        return self.parse_source(source_crs).is_geographic

    @staticmethod
    def _utm_epsg(center_lon: float, center_lat: float) -> str:
        """Return the UTM EPSG code for a centre coordinate.

        Args:
            center_lon: Longitude in degrees (any sign).
            center_lat: Latitude in degrees; sign selects hemisphere.

        Returns:
            ``EPSG:326xx`` (north) or ``EPSG:327xx`` (south).
        """
        lon = ((center_lon + 180.0) % 360.0) - 180.0
        zone = int(math.floor((lon + 180.0) / 6.0)) + 1
        zone = min(max(zone, 1), 60)
        prefix = 32600 if center_lat >= 0 else 32700
        return f"EPSG:{prefix + zone}"

    @staticmethod
    def _large_geometry_target(
        bounds: tuple[float, float, float, float],
        purpose: MeasurementPurpose,
    ) -> tuple[str, str]:
        """Choose the projection for extents no single UTM zone fits.

        Args:
            bounds: Geographic ``(minx, miny, maxx, maxy)`` in degrees.
            purpose: Whether the measurement is an area or a length.

        Returns:
            ``(target_crs, strategy)``.
        """
        if purpose == "area":
            return _GLOBAL_EQUAL_AREA_CRS, "global_equal_area"
        minx, miny, maxx, maxy = bounds
        lon_0 = (minx + maxx) / 2.0
        lat_0 = min(max((miny + maxy) / 2.0, -89.0), 89.0)
        return (
            f"+proj=aeqd +lat_0={lat_0:.6f} +lon_0={lon_0:.6f} "
            f"+datum=WGS84 +units=m +no_defs",
            "local_aeqd",
        )

    def _select_with_strategy(
        self,
        source_crs: str | None,
        bounds: tuple[float, float, float, float],
        *,
        purpose: MeasurementPurpose = "area",
    ) -> tuple[str, str]:
        """Select a measurement CRS and report the strategy used.

        Args:
            source_crs: The dataset's CRS metadata (``None`` → error).
            bounds: Geometry extent in *source* CRS units.
            purpose: ``"area"`` or ``"length"`` (large-geometry pick).

        Returns:
            ``(target_crs, strategy)`` where strategy is one of
            ``source_projected``, ``utm``, ``global_equal_area`` or
            ``local_aeqd``.

        Raises:
            MissingCRSError: When the source CRS is absent.
            InvalidCRSError: When the source CRS cannot be parsed or is
                of an unsupported type.
        """
        crs = self.parse_source(source_crs)

        if crs.is_projected:
            if _is_metric(crs):
                return _code(crs), "source_projected"
            # Non-metric (e.g. feet): select via geographic bounds.
            bounds = self._to_geographic_bounds(bounds, crs)
        elif not crs.is_geographic:
            raise InvalidCRSError(
                f"CRS {normalize_crs(source_crs)!r} is neither geographic "
                f"nor projected; cannot select a measurement CRS"
            )

        minx, miny, maxx, maxy = bounds
        width = maxx - minx
        height = maxy - miny
        lon_0 = (minx + maxx) / 2.0
        lat_0 = (miny + maxy) / 2.0

        fits_utm = (
            width <= _MAX_UTM_WIDTH_DEG
            and height <= _MAX_UTM_HEIGHT_DEG
            and miny >= _UTM_MIN_LAT
            and maxy <= _UTM_MAX_LAT
        )
        if fits_utm:
            return self._utm_epsg(lon_0, lat_0), "utm"

        target, strategy = self._large_geometry_target(bounds, purpose)
        logger.debug(
            "Large/polar extent (%.1f°x%.1f°) → %s (%s)",
            width,
            height,
            target,
            strategy,
        )
        return target, strategy

    def select_measurement_crs(
        self,
        source_crs: str | None,
        bounds: tuple[float, float, float, float],
        *,
        purpose: MeasurementPurpose = "area",
    ) -> str:
        """Select a projected, metric CRS suitable for measuring.

        See :meth:`_select_with_strategy` for the semantics; this thin
        wrapper returns only the CRS string.

        Args:
            source_crs: The dataset's CRS metadata (``None`` → error).
            bounds: Geometry extent in *source* CRS units.
            purpose: ``"area"`` or ``"length"``.

        Returns:
            A projected, metric CRS string.

        Raises:
            MissingCRSError: When the source CRS is absent.
            InvalidCRSError: When the source CRS cannot be parsed or is
                of an unsupported type (neither geographic nor
                projected).
        """
        target, _ = self._select_with_strategy(
            source_crs, bounds, purpose=purpose
        )
        return target

    @staticmethod
    def _to_geographic_bounds(
        bounds: tuple[float, float, float, float], source: CRS
    ) -> tuple[float, float, float, float]:
        """Reproject a bounding box to WGS 84 lon/lat via its corners.

        Args:
            bounds: Extent in the source CRS.
            source: The source CRS.

        Returns:
            The geographic extent ``(minx, miny, maxx, maxy)``.
        """
        minx, miny, maxx, maxy = bounds
        transformer = Transformer.from_crs(source, _WGS84, always_xy=True)
        corners = [
            transformer.transform(x, y)
            for x in (minx, maxx)
            for y in (miny, maxy)
        ]
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        return (min(xs), min(ys), max(xs), max(ys))

    def transform_geometry(
        self,
        geometry: BaseGeometry,
        source_crs: str,
        target_crs: str,
    ) -> BaseGeometry:
        """Re-project one geometry between two CRS.

        Args:
            geometry: The geometry to transform.
            source_crs: CRS the geometry is currently in.
            target_crs: Desired CRS (authority code or PROJ string).

        Returns:
            The re-projected geometry.

        Raises:
            InvalidCRSError: If either CRS is unusable or the transform
                cannot be built.
        """
        source = self.parse_source(source_crs)
        try:
            target = CRS.from_user_input(target_crs)
            transformer = Transformer.from_crs(
                source, target, always_xy=True
            )
        except (pyproj.exceptions.CRSError, ValueError) as exc:
            raise InvalidCRSError(
                f"Cannot build transform {source_crs!r} → {target_crs!r}: "
                f"{exc}"
            ) from exc
        return shapely_transform(transformer.transform, geometry)

    def prepare_for_measurement(
        self,
        geometry: BaseGeometry,
        source_crs: str | None,
        *,
        purpose: MeasurementPurpose = "area",
    ) -> MeasurementContext:
        """Detect, select and transform: geometry ready for measurement.

        This is the pipeline entry point::

            source CRS → classify → (geographic? select projected CRS :
            reuse metric CRS) → transform → MeasurementContext

        Args:
            geometry: Geometry in the source CRS.
            source_crs: CRS metadata (``None`` → :class:`MissingCRSError`).
            purpose: ``"area"`` or ``"length"``.

        Returns:
            The transformed geometry plus the measurement CRS.

        Raises:
            MissingCRSError: Absent CRS — never assumed.
            InvalidCRSError: Unparseable/unsupported CRS.
            EmptyGeometryError: Empty geometry has no extent to project.
        """
        crs = self.parse_source(source_crs)
        source_code = _code(crs)

        if geometry is None or geometry.is_empty:
            raise EmptyGeometryError(
                "Cannot prepare an empty or missing geometry for "
                "measurement"
            )

        if crs.is_projected and _is_metric(crs):
            return MeasurementContext(
                geometry=geometry,
                source_crs=source_code,
                measurement_crs=source_code,
                strategy="source_projected",
                was_transformed=False,
            )

        target, strategy = self._select_with_strategy(
            source_code, geometry.bounds, purpose=purpose
        )
        transformed = self.transform_geometry(geometry, source_code, target)
        logger.debug(
            "Prepared geometry: %s → %s (%s)",
            source_code,
            target,
            strategy,
        )
        return MeasurementContext(
            geometry=transformed,
            source_crs=source_code,
            measurement_crs=target,
            strategy=strategy,
            was_transformed=True,
        )
