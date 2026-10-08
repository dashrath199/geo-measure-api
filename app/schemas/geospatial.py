"""Common geospatial domain models shared by every format processor.

These Pydantic models are the *only* geospatial structure that crosses
toward the API layer: they contain plain scalars (WKT text, bounding
boxes, strings) and never hold GeoPandas or Shapely objects, so a
``GeospatialFile`` is always JSON-serializable as-is.

Pipeline position::

    Format Processor → Normalized Features (these models)
        → CRS Service → Measurement Service → API Response
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, RootModel, model_validator


class CRSInformation(BaseModel):
    """Reference-system information for a file or feature.

    Attributes:
        authority_code: Compact authority form (``"EPSG:4326"``) or the
            CRS's own string when no authority code exists; ``None`` when
            the source defines no CRS.
        name: Human-readable CRS name when resolvable.
        is_geographic: Whether coordinates are angular (degrees) —
            ``None`` when the CRS is unknown.
        is_projected: Whether coordinates are planar (metres/feet) —
            ``None`` when the CRS is unknown.
    """

    authority_code: str | None = None
    name: str | None = None
    is_geographic: bool | None = None
    is_projected: bool | None = None

    @property
    def is_defined(self) -> bool:
        """Return whether any CRS information is available."""
        return self.authority_code is not None or self.name is not None


class Properties(RootModel[dict[str, Any]]):
    """JSON-safe feature attributes.

    Behaves like a read-only mapping (``props["name"]``, ``"name" in
    props``) while serializing as a plain JSON object.
    """

    root: dict[str, Any] = Field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        """Return one attribute by name.

        Args:
            key: Attribute name.

        Returns:
            The attribute value.
        """
        return self.root[key]

    def __contains__(self, key: object) -> bool:
        """Report whether an attribute name exists.

        Args:
            key: Attribute name.

        Returns:
            ``True`` when present.
        """
        return key in self.root

    def get(self, key: str, default: Any = None) -> Any:
        """Return an attribute or a default.

        Args:
            key: Attribute name.
            default: Value returned when the key is absent.

        Returns:
            The attribute value or ``default``.
        """
        return self.root.get(key, default)


class GeometryInformation(BaseModel):
    """Serializable geometry summary (no Shapely objects).

    Attributes:
        geometry_type: Shapely type name (``"Polygon"``, …).
        wkt: Well-Known Text rendering of the geometry.
        bbox: ``(minx, miny, maxx, maxy)``; ``None`` for empty
            geometries.
        is_valid: Whether the geometry passes OGC validity.
        is_empty: Whether the geometry has no coordinates.
    """

    geometry_type: str
    wkt: str
    bbox: tuple[float, float, float, float] | None = None
    is_valid: bool = True
    is_empty: bool = False


class Feature(BaseModel):
    """The common feature representation every processor produces.

    Attributes:
        feature_id: Zero-based index within the source dataset.
        geometry_type: Geometry type name, ``None`` when the source
            feature has no geometry.
        geometry: Serializable geometry details, ``None`` when missing.
        source_crs: The CRS this feature's coordinates are expressed in.
        properties: JSON-safe attribute mapping.
    """

    feature_id: int
    geometry_type: str | None = None
    geometry: GeometryInformation | None = None
    source_crs: CRSInformation
    properties: Properties = Field(default_factory=Properties)


class GeospatialFile(BaseModel):
    """A processed upload: file metadata plus normalized features.

    Attributes:
        filename: Name of the processed source file.
        file_type: Format identifier (``shapefile_zip`` / ``kml``).
        source_crs: CRS shared by all features.
        features: Normalized features in source order.
        feature_count: Number of features (kept in sync automatically).
    """

    filename: str
    file_type: str
    source_crs: CRSInformation
    features: list[Feature]
    feature_count: int = 0

    @model_validator(mode="after")
    def _sync_feature_count(self) -> GeospatialFile:
        """Keep ``feature_count`` equal to the number of features.

        Returns:
            The validated model instance.
        """
        self.feature_count = len(self.features)
        return self

    @property
    def invalid_geometry_ids(self) -> list[int]:
        """Return ids of features whose geometry exists but is invalid."""
        return [
            f.feature_id
            for f in self.features
            if f.geometry is not None and not f.geometry.is_valid
        ]

    @property
    def empty_geometry_ids(self) -> list[int]:
        """Return ids of features carrying an empty geometry."""
        return [
            f.feature_id
            for f in self.features
            if f.geometry is not None and f.geometry.is_empty
        ]

    @property
    def missing_geometry_ids(self) -> list[int]:
        """Return ids of features without any geometry."""
        return [
            f.feature_id for f in self.features if f.geometry is None
        ]
