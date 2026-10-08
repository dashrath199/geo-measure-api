"""Schemas for file upload and file metadata responses."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import FileStatus


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
        created_at: Upload timestamp (UTC).
        updated_at: Last status-change timestamp (UTC).
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
    created_at: datetime = Field(description="Upload timestamp (UTC).")
    updated_at: datetime = Field(description="Last update timestamp (UTC).")
