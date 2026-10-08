"""ORM models.

:class:`FileRecord` stores metadata about an uploaded geospatial file and
its processing outcome. Geometry objects are deliberately NOT persisted —
only scalar summaries (feature count, CRS, status, error) live here.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FileStatus(str, enum.Enum):
    """Explicit lifecycle states of an uploaded file.

    Attributes:
        UPLOADED: Bytes accepted and stored; not yet processed.
        PROCESSING: Feature extraction and measurement in progress.
        COMPLETED: Processing finished successfully.
        FAILED: Processing failed; ``error_message`` explains why.
    """

    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def _utcnow() -> datetime:
    """Return the current UTC time (timezone-aware).

    Returns:
        Current UTC timestamp.
    """
    return datetime.now(timezone.utc)


class FileRecord(Base):
    """Metadata for one uploaded geospatial file.

    The primary key is a UUID string so identifiers are guessable-proof
    and safe to expose in URLs.

    Attributes:
        id: UUID primary key.
        original_filename: Name supplied by the client.
        stored_path: Server-side location of the stored bytes.
        file_type: Detected input format (e.g. ``shapefile_zip``, ``kml``).
        status: Processing lifecycle state.
        feature_count: Number of extracted features, once processed.
        crs: Authority code of the source CRS (e.g. ``EPSG:4326``).
        error_message: Failure detail when ``status`` is ``FAILED``.
        created_at: Insertion timestamp (UTC).
        updated_at: Last modification timestamp (UTC).
    """

    __tablename__ = "files"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="UUID primary key exposed as the file identifier.",
    )
    original_filename: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Filename as provided by the client.",
    )
    stored_path: Mapped[str] = mapped_column(
        String(1024),
        nullable=False,
        comment="Server-side path where the upload is stored.",
    )
    file_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Input format identifier, e.g. shapefile_zip or kml.",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=FileStatus.UPLOADED.value,
        index=True,
        comment="Lifecycle state: UPLOADED, PROCESSING, COMPLETED, FAILED.",
    )
    feature_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Extracted feature count; NULL until processing completes.",
    )
    crs: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Source CRS authority code, e.g. EPSG:4326.",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Failure detail when status is FAILED.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        comment="Insertion time (UTC).",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        comment="Last update time (UTC).",
    )

    def __repr__(self) -> str:
        """Return a compact debug representation (no path details)."""
        return (
            f"<FileRecord id={self.id} file={self.original_filename!r} "
            f"type={self.file_type} status={self.status}>"
        )


class FeatureMeasurementRecord(Base):
    """Persisted measurement outcome for one feature of one file.

    Written by the processing pipeline (Phase 12); read by the
    measurements endpoint. Only scalars are stored — no geometry.

    Attributes:
        id: Surrogate primary key.
        file_id: Owning file (``files.id``).
        feature_index: Zero-based feature index from processing.
        geometry_type: Geometry type name, when known.
        measurement_kind: ``"area"`` or ``"length"``, when measured.
        measurement_value: The measured number, when measured.
        unit: ``"square_meters"`` / ``"meters"``, when measured.
        status: Detailed per-feature status.
        category: Structured category (SUPPORTED, …).
        measurement_crs: Projected CRS used, when measured.
        reason: Explanation for non-measured outcomes.
        properties_json: Feature attributes as a JSON object string.
    """

    __tablename__ = "feature_measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        index=True,
        comment="Owning files.id.",
    )
    feature_index: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Zero-based feature index."
    )
    geometry_type: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="Geometry type name."
    )
    measurement_kind: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="area or length when measured.",
    )
    measurement_value: Mapped[float | None] = mapped_column(
        nullable=True, comment="Measured value in the unit below."
    )
    unit: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="square_meters or meters when measured.",
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="Detailed per-feature status."
    )
    category: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Structured category (SUPPORTED, …).",
    )
    measurement_crs: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Projected CRS used for the measurement.",
    )
    reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Why nothing was measured."
    )
    properties_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Feature attributes as a JSON object string.",
    )

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        return (
            f"<FeatureMeasurementRecord file={self.file_id} "
            f"feature={self.feature_index} status={self.status}>"
        )
