"""Schemas for file upload and file metadata responses."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)

from app.db.models import FileStatus


def _as_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime.

    Timestamps are kept in UTC end to end, but SQLite's ``DATETIME``
    type hands them back without timezone information. A naive value is
    therefore *UTC by contract* — the missing offset is attached here so
    it can never be reinterpreted as local time.

    Args:
        value: A naive (UTC) or already aware datetime.

    Returns:
        The equivalent timezone-aware UTC datetime.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class FileUploadResponse(BaseModel):
    """Response body returned by ``POST /api/files/``.

    Attributes:
        id: Unique identifier of the registered file.
        filename: Sanitized original filename as supplied by the client.
        status: Current processing status.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "3f0c9f1e-8a5d-4f3a-9c2b-1d4e6a8b0c7d",
                "filename": "survey.kml",
                "status": "PROCESSING",
            }
        }
    )

    id: str = Field(description="Unique file identifier.")
    filename: str = Field(description="Sanitized original filename.")
    status: FileStatus = Field(description="Current processing status.")


class FileInfoResponse(BaseModel):
    """Response body returned by ``GET /api/files/{id}/``.

    Deliberately excludes anything server-internal (storage paths,
    settings, host details): only persisted, client-safe metadata.

    Attributes:
        id: The file's UUID.
        filename: Sanitized original filename.
        file_type: Detected input format (``shapefile_zip`` / ``kml``).
        status: Final (or in-flight) processing status.
        feature_count: Number of extracted features; ``None`` until
            processing has completed.
        crs: Source CRS authority code; ``None`` when undefined or not
            yet processed.
        error_message: Safe failure text — present only when ``status``
            is ``FAILED`` (paths redacted, never a stack trace).
        created_at: Upload timestamp (UTC); serialized as RFC 3339
            with an explicit ``Z`` suffix (e.g.
            ``2026-10-08T11:00:07.887267Z``).
        updated_at: Last status-change timestamp (UTC); serialized
            like ``created_at``.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "abc123",
                "filename": "survey.kml",
                "file_type": "kml",
                "status": "COMPLETED",
                "feature_count": 120,
                "crs": "EPSG:4326",
                "error_message": None,
                "created_at": "2026-10-08T12:00:00Z",
                "updated_at": "2026-10-08T12:00:01Z",
            }
        }
    )

    id: str = Field(description="The file's UUID.")
    filename: str = Field(description="Sanitized original filename.")
    file_type: str = Field(description="Detected input format.")
    status: FileStatus = Field(description="Processing status.")
    feature_count: int | None = Field(
        default=None,
        description="Extracted feature count; null until processed.",
    )
    crs: str | None = Field(
        default=None,
        description="Source CRS authority code; null when unknown.",
    )
    error_message: str | None = Field(
        default=None,
        description="Sanitized failure detail; set only when FAILED.",
    )
    created_at: datetime = Field(
        description=(
            "Upload timestamp (UTC, RFC 3339 with a Z suffix)."
        ),
        # Kept explicit: the JSON serializer below returns a plain
        # string, which would otherwise drop ``format`` from the
        # (serialization-mode) OpenAPI schema.
        json_schema_extra={"format": "date-time"},
    )
    updated_at: datetime = Field(
        description=(
            "Last update timestamp (UTC, RFC 3339 with a Z suffix)."
        ),
        json_schema_extra={"format": "date-time"},
    )

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _timestamps_stay_utc(cls, value: object) -> object:
        """Interpret naive stored timestamps as UTC.

        Storage keeps UTC without an offset; attaching ``tzinfo`` here
        preserves that meaning instead of letting serialization guess
        a local timezone.

        Args:
            value: The raw value coming from storage.

        Returns:
            The value with explicit UTC semantics (unchanged otherwise).
        """
        if isinstance(value, datetime):
            return _as_utc(value)
        return value

    @field_serializer("created_at", "updated_at", when_used="json")
    def _serialize_utc_z(self, value: datetime) -> str:
        """Serialize timestamps as explicit UTC with a ``Z`` suffix.

        Args:
            value: The timestamp to serialize.

        Returns:
            An RFC 3339 UTC string such as
            ``2026-10-08T11:00:07.887267Z``.
        """
        return _as_utc(value).isoformat().replace("+00:00", "Z")
