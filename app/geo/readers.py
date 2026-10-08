"""Reading shapefiles into normalized feature records.

Framework-agnostic service component (no FastAPI imports): opens a
validated/extracted shapefile with GeoPandas, preserves its **actual**
source CRS (never assuming EPSG:4326 — ``None`` when absent), and walks
every feature into a uniform representation::

    {
        "feature_id": 0,
        "geometry_type": "Polygon",
        "geometry": <shapely geometry or None>,
        "crs": "EPSG:4326",
        "properties": {...},
        "is_valid": True,
        "is_empty": False,
    }

Degenerate data is first-class, not exceptional: missing geometries,
empty geometries and invalid (self-intersecting) geometries are detected
and reported per feature and per dataset without aborting the read.

No measurements are performed here.
"""

from __future__ import annotations

import base64
import json
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from pyproj import CRS
from shapely.geometry.base import BaseGeometry

from app.exceptions import (
    GeometryProcessingError,
    InvalidGeometryError,
    UnsupportedGeometryError,
)

logger = logging.getLogger(__name__)


def normalize_crs(crs: Any) -> str | None:
    """Normalize a CRS to a compact authority string without guessing.

    Never falls back to EPSG:4326: an absent CRS stays ``None`` so the
    processing pipeline can refuse to measure rather than assume.

    Args:
        crs: A pyproj CRS, any ``pyproj.CRS.from_user_input``-compatible
            value, or ``None``.

    Returns:
        ``"EPSG:4326"``-style authority code when available, the CRS's
        own string form otherwise, or ``None`` when no CRS is defined.
    """
    if crs is None:
        return None
    if not isinstance(crs, CRS):
        try:
            crs = CRS.from_user_input(crs)
        except Exception:  # noqa: BLE001 - unrecognizable CRS → best effort
            return str(crs)
    authority = crs.to_authority()
    if authority:
        return f"{authority[0]}:{authority[1]}"
    return crs.to_string()


def _coerce_property(value: Any) -> Any:
    """Convert an attribute value into a JSON-safe Python scalar.

    numpy/pandas scalars become plain Python, dates become ISO strings,
    NaN/``NA``/``NaT`` become ``None`` and bytes become base64 text.

    Args:
        value: Raw attribute value.

    Returns:
        A JSON-serializable representation of the value.
    """
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, int):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {str(key): _coerce_property(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce_property(item) for item in value]
    # numpy scalars (np.int64, np.datetime64, …) expose .item()
    item_method = getattr(value, "item", None)
    if callable(item_method) and getattr(value, "shape", None) == ():
        try:
            return _coerce_property(item_method())
        except (ValueError, TypeError):
            pass
    return str(value)


def _is_missing_geometry(geometry: Any) -> bool:
    """Report whether a geometry slot holds a missing value.

    Args:
        geometry: Value taken from the geometry column.

    Returns:
        ``True`` for ``None``, ``NA`` or floating NaN.
    """
    if geometry is None or geometry is pd.NA:
        return True
    return isinstance(geometry, float) and math.isnan(geometry)


@dataclass(frozen=True)
class GeospatialFeature:
    """One feature read from a source dataset.

    Attributes:
        feature_id: Zero-based positional index within the dataset.
        geometry_type: Shapely type name (``"Point"``, ``"Polygon"``,
            ``"MultiLineString"``, …) or ``None`` when geometry is missing.
        geometry: The shapely geometry object, or ``None`` when missing.
        crs: The dataset's source CRS for this feature (uniform across
            the dataset), ``None`` when the source defines no CRS.
        properties: JSON-safe attribute mapping (geometry excluded).
        is_valid: Whether shapely considers the geometry valid.
        is_empty: Whether the geometry is an empty (zero-part) shape.
    """

    feature_id: int
    geometry_type: str | None
    geometry: BaseGeometry | None
    crs: str | None
    properties: dict[str, Any]
    is_valid: bool = True
    is_empty: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return the canonical dict representation of this feature.

        The geometry is rendered as WKT text so the result is
        JSON-serializable.

        Returns:
            A dict with ``feature_id``, ``geometry_type``, ``geometry``,
            ``crs`` and ``properties`` (plus validity flags).
        """
        return {
            "feature_id": self.feature_id,
            "geometry_type": self.geometry_type,
            "geometry": self.geometry.wkt if self.geometry is not None else None,
            "crs": self.crs,
            "properties": self.properties,
            "is_valid": self.is_valid,
            "is_empty": self.is_empty,
        }


@dataclass(frozen=True)
class FeatureDataset:
    """All features of one dataset plus its CRS and quality summaries.

    Attributes:
        features: Normalized features in source order.
        crs: Dataset source CRS string, or ``None`` when undefined.
    """

    features: tuple[GeospatialFeature, ...]
    crs: str | None

    @property
    def feature_count(self) -> int:
        """Return the number of features read."""
        return len(self.features)

    @property
    def invalid_geometry_ids(self) -> tuple[int, ...]:
        """Return ids of features whose geometry exists but is invalid."""
        return tuple(
            f.feature_id
            for f in self.features
            if f.geometry is not None and not f.is_valid
        )

    @property
    def empty_geometry_ids(self) -> tuple[int, ...]:
        """Return ids of features carrying an empty geometry."""
        return tuple(
            f.feature_id for f in self.features if f.geometry is not None and f.is_empty
        )

    @property
    def missing_geometry_ids(self) -> tuple[int, ...]:
        """Return ids of features without any geometry."""
        return tuple(f.feature_id for f in self.features if f.geometry is None)


def features_from_gdf(gdf: gpd.GeoDataFrame) -> FeatureDataset:
    """Normalize an in-memory GeoDataFrame into a :class:`FeatureDataset`.

    Args:
        gdf: Source features; CRS is taken from ``gdf.crs`` as-is.

    Returns:
        The normalized dataset.

    Raises:
        InvalidGeometryError: If the frame has no usable geometry
            column.
        UnsupportedGeometryError: If a geometry slot holds a value that
            is not a Shapely geometry.
    """
    try:
        geometry_column = gdf.geometry.name
    except AttributeError as exc:
        raise InvalidGeometryError(
            "GeoDataFrame has no active geometry column"
        ) from exc

    crs = normalize_crs(gdf.crs)
    property_columns = [col for col in gdf.columns if col != geometry_column]
    property_rows = gdf[property_columns].to_dict(orient="records")

    features: list[GeospatialFeature] = []
    for feature_id, (geometry, raw_properties) in enumerate(
        zip(gdf.geometry, property_rows, strict=True)
    ):
        if _is_missing_geometry(geometry):
            features.append(
                GeospatialFeature(
                    feature_id=feature_id,
                    geometry_type=None,
                    geometry=None,
                    crs=crs,
                    properties={
                        key: _coerce_property(value)
                        for key, value in raw_properties.items()
                    },
                    is_valid=False,
                    is_empty=False,
                )
            )
            continue

        if not isinstance(geometry, BaseGeometry):
            # Missing/empty/invalid geometries are first-class (handled
            # above and per feature); anything that is not a geometry at
            # all is the reader's last-resort guard.
            raise UnsupportedGeometryError(
                f"Feature {feature_id} holds an unsupported geometry "
                f"value ({type(geometry).__name__}); expected a Shapely "
                f"geometry"
            )

        features.append(
            GeospatialFeature(
                feature_id=feature_id,
                geometry_type=str(geometry.geom_type),
                geometry=geometry,
                crs=crs,
                properties={
                    key: _coerce_property(value)
                    for key, value in raw_properties.items()
                },
                is_valid=bool(geometry.is_valid),
                is_empty=bool(geometry.is_empty),
            )
        )

    dataset = FeatureDataset(features=tuple(features), crs=crs)
    logger.debug(
        "Normalized %d feature(s), crs=%s (invalid=%d, empty=%d, missing=%d)",
        dataset.feature_count,
        dataset.crs,
        len(dataset.invalid_geometry_ids),
        len(dataset.empty_geometry_ids),
        len(dataset.missing_geometry_ids),
    )
    return dataset


def read_shapefile(shp_path: str | Path) -> FeatureDataset:
    """Open a shapefile with GeoPandas and normalize its features.

    The shapefile's ``.prj`` (when present) defines the CRS; when absent
    the dataset's CRS is ``None`` — it is never assumed to be 4326.

    Args:
        shp_path: Path to the ``.shp`` file (its sidecars must sit
            alongside it, as produced by the extraction service).

    Returns:
        The normalized dataset.

    Raises:
        GeometryProcessingError: If the file is missing, has the wrong
            extension, or cannot be parsed (code ``INVALID_SHAPEFILE``
            for unreadable datasets).
    """
    path = Path(shp_path)
    if path.suffix.lower() != ".shp":
        raise GeometryProcessingError(
            f"Expected a .shp file, got: {path.name!r}"
        )
    if not path.is_file():
        raise GeometryProcessingError(f"Shapefile not found: {path}")

    try:
        gdf = gpd.read_file(path)
    except Exception as exc:  # noqa: BLE001 - reader boundary wraps errors
        raise GeometryProcessingError(
            f"Could not read shapefile '{path.name}': {exc}",
            code="INVALID_SHAPEFILE",
        ) from exc

    dataset = features_from_gdf(gdf)
    logger.info(
        "Read shapefile %s: %d feature(s), crs=%s",
        path.name,
        dataset.feature_count,
        dataset.crs,
    )
    return dataset


def to_json(dataset: FeatureDataset) -> str:
    """Serialize a dataset to JSON (geometry as WKT) for diagnostics.

    Args:
        dataset: The dataset to serialize.

    Returns:
        A JSON string.
    """
    return json.dumps(
        {
            "crs": dataset.crs,
            "feature_count": dataset.feature_count,
            "features": [feature.as_dict() for feature in dataset.features],
        }
    )
