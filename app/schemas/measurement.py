"""Schemas for measurement results.

Units are explicit and SI-only:

- area  → ``square_meters`` (m²)
- length → ``meters`` (m)

These units are only meaningful because :class:`~app.geo.crs.CRSService`
guarantees measurement happens in a projected, metric CRS — never in
geographic degrees.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.geospatial import Properties


class MeasurementStatus(str, Enum):
    """Outcome of measuring one feature.

    Attributes:
        MEASURED: A value was computed successfully.
        NOT_REQUIRED: Point/MultiPoint — no measurement applies.
        SKIPPED_MISSING_GEOMETRY: The feature has no geometry.
        SKIPPED_EMPTY: The geometry is empty (zero coordinates).
        SKIPPED_UNSUPPORTED_TYPE: Geometry type outside the supported
            point/line/polygon families (e.g. GeometryCollection).
        SKIPPED_INVALID: The geometry fails OGC validity — its planar
            measurement would be misleading.
        SKIPPED_NO_CRS: Source CRS undefined; nothing is assumed.
        SKIPPED_INVALID_CRS: Source CRS metadata is unparseable.
        ERROR: Unexpected failure isolated to this one feature.
    """

    MEASURED = "measured"
    NOT_REQUIRED = "not_required"
    SKIPPED_MISSING_GEOMETRY = "skipped_missing_geometry"
    SKIPPED_EMPTY = "skipped_empty"
    SKIPPED_UNSUPPORTED_TYPE = "skipped_unsupported_type"
    SKIPPED_INVALID = "skipped_invalid"
    SKIPPED_NO_CRS = "skipped_no_crs"
    SKIPPED_INVALID_CRS = "skipped_invalid_crs"
    ERROR = "error"


class MeasurementCategory(str, Enum):
    """Coarse, structured classification of a measurement outcome.

    Attributes:
        SUPPORTED: A meaningful value was measured.
        UNSUPPORTED: The geometry type is outside the supported
            matrix (never crashes the application — skipped).
        INVALID: The geometry fails validity (or the input was
            corrupt) — a planar figure would be misleading.
        NO_MEASUREMENT: No value applies: points, empty/missing
            geometry, or undefined/unparseable CRS.
    """

    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    INVALID = "INVALID"
    NO_MEASUREMENT = "NO_MEASUREMENT"


# Detailed status → structured category (always derived, never hand-set).
_CATEGORY_BY_STATUS: dict[MeasurementStatus, MeasurementCategory] = {
    MeasurementStatus.MEASURED: MeasurementCategory.SUPPORTED,
    MeasurementStatus.NOT_REQUIRED: MeasurementCategory.NO_MEASUREMENT,
    MeasurementStatus.SKIPPED_MISSING_GEOMETRY: (
        MeasurementCategory.NO_MEASUREMENT
    ),
    MeasurementStatus.SKIPPED_EMPTY: MeasurementCategory.NO_MEASUREMENT,
    MeasurementStatus.SKIPPED_UNSUPPORTED_TYPE: (
        MeasurementCategory.UNSUPPORTED
    ),
    MeasurementStatus.SKIPPED_INVALID: MeasurementCategory.INVALID,
    MeasurementStatus.SKIPPED_NO_CRS: MeasurementCategory.NO_MEASUREMENT,
    MeasurementStatus.SKIPPED_INVALID_CRS: (
        MeasurementCategory.NO_MEASUREMENT
    ),
    MeasurementStatus.ERROR: MeasurementCategory.INVALID,
}


class AreaMeasurement(BaseModel):
    """Polygonal area in square metres.

    Attributes:
        area: The area value.
        unit: Always ``square_meters``.
    """

    area: float = Field(description="Area value.")
    unit: str = Field(
        default="square_meters", description="Unit of the area value."
    )


class LengthMeasurement(BaseModel):
    """Linear length in metres.

    Attributes:
        length: The length value.
        unit: Always ``meters``.
    """

    length: float = Field(description="Length value.")
    unit: str = Field(
        default="meters", description="Unit of the length value."
    )


class FeatureMeasurement(BaseModel):
    """Measurement outcome for a single feature.

    Attributes:
        feature_id: Zero-based feature index from the normalized file.
        geometry_type: Geometry type name, when known.
        measurement: The measured value (``{"area": …}`` /
            ``{"length": …}``), or ``None`` when not measured.
        status: Why/how it was (not) measured.
        measurement_crs: Projected CRS the measurement was made in —
            ``None`` when nothing was measured.
        reason: Human-readable explanation for non-``measured`` statuses.
    """

    feature_id: int
    geometry_type: str | None = None
    measurement: AreaMeasurement | LengthMeasurement | None = None
    status: MeasurementStatus
    measurement_crs: str | None = None
    reason: str | None = None
    properties: Properties = Field(default_factory=Properties)
    category: MeasurementCategory = Field(
        default=MeasurementCategory.NO_MEASUREMENT,
        description="Structured outcome: SUPPORTED, UNSUPPORTED, "
        "INVALID or NO_MEASUREMENT (derived from status).",
    )

    @model_validator(mode="after")
    def _sync_category(self) -> FeatureMeasurement:
        """Derive the structured category from the detailed status.

        Returns:
            The validated result with ``category`` in sync.
        """
        self.category = _CATEGORY_BY_STATUS[self.status]
        return self


class MeasurementValue(BaseModel):
    """One measured value with its explicit type and unit.

    Attributes:
        type: ``area`` or ``length``.
        value: The measured number.
        unit: ``square_meters`` or ``meters``.
    """

    type: Literal["area", "length"]
    value: float
    unit: str


class FeatureMeasurementInfo(BaseModel):
    """API view of one feature's measurement outcome.

    Plain scalars only — no GeoPandas/Shapely objects ever cross this
    boundary.

    Attributes:
        feature_id: Zero-based feature index.
        geometry_type: Geometry type name, when known.
        measurement: The value block, or ``None`` when unmeasured.
        status: Structured category (``SUPPORTED`` / ``UNSUPPORTED`` /
            ``INVALID`` / ``NO_MEASUREMENT``).
        reason: Explanation for non-measured outcomes.
        properties: The feature's source attributes.
    """

    feature_id: int
    geometry_type: str | None = None
    measurement: MeasurementValue | None = None
    status: "MeasurementCategory"
    reason: str | None = None
    properties: Properties = Field(default_factory=Properties)


class FileMeasurementsResponse(BaseModel):
    """Response body returned by ``GET /api/files/{id}/measurements/``.

    Attributes:
        file_id: The file these measurements belong to.
        crs: The source CRS the features are expressed in.
        features: One entry per feature, in source order.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "file_id": "abc123",
                "crs": "EPSG:4326",
                "features": [
                    {
                        "feature_id": 0,
                        "geometry_type": "Polygon",
                        "measurement": {
                            "type": "area",
                            "value": 15234.52,
                            "unit": "square_meters",
                        },
                        "status": "SUPPORTED",
                        "reason": None,
                        "properties": {"name": "zone a"},
                    },
                    {
                        "feature_id": 2,
                        "geometry_type": "Point",
                        "measurement": None,
                        "status": "NO_MEASUREMENT",
                        "reason": "Point geometries require no measurement",
                        "properties": {},
                    },
                ],
            }
        }
    )

    file_id: str
    crs: str | None = None
    features: list[FeatureMeasurementInfo]
