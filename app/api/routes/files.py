"""File resource routes.

HTTP concerns only: parse the multipart request, delegate to the
services, shape the response. All validation, business rules and the
processing pipeline live in the service layer.
"""

from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, File, UploadFile

from app.api.deps import (
    get_feature_measurement_repository,
    get_file_service,
    get_processing_service,
)
from app.db.models import FeatureMeasurementRecord, FileRecord, FileStatus
from app.db.repository import FeatureMeasurementRepository
from app.exceptions import redact_paths
from app.schemas.error import ErrorResponse
from app.schemas.file import FileInfoResponse, FileUploadResponse
from app.schemas.geospatial import Properties
from app.schemas.measurement import (
    FeatureMeasurementInfo,
    FileMeasurementsResponse,
    MeasurementCategory,
    MeasurementValue,
)
from app.services.file_service import FileService
from app.services.processing_service import ProcessingService

router = APIRouter(prefix="/files", tags=["files"])


@router.post(
    "/",
    response_model=FileUploadResponse,
    status_code=201,
    summary="Register a geospatial file for measurement",
    description=(
        "Register a geospatial file for measurement. Valid formats are a «.zip» "
        "archive containing a shapefile (with or without ``.prj``) or a «.kml» "
        "document; anything else is rejected with 415. The response reports the "
        "acceptance stage (``PROCESSING``) per the upload contract; the pipeline "
        "then runs within this request, persists the final state "
        "(``COMPLETED``/``FAILED``) and exposes it through the two retrieval "
        "endpoints below. On pipeline failure the response reports ``FAILED`` — "
        "clients never receive internal error details here."
    ),
    responses={
        400: {
            "description": "Malformed archive or invalid file content.",
            "model": ErrorResponse,
        },
        413: {
            "description": "Upload exceeds the configured size limit.",
            "model": ErrorResponse,
        },
        415: {
            "description": "Unsupported file format.",
            "model": ErrorResponse,
        },
        422: {
            "description": "Missing multipart file part.",
            "model": ErrorResponse,
        },
        500: {
            "description": "Unexpected server or storage failure.",
            "model": ErrorResponse,
        },
    },
)
async def upload_file(
    file: UploadFile = File(
        description="Geospatial file: a .zip containing a shapefile, or a .kml file."
    ),
    service: FileService = Depends(get_file_service),
    processing: ProcessingService = Depends(get_processing_service),
) -> FileUploadResponse:
    """Validate, store, register and process an uploaded file.

    The response reports the acceptance stage (``PROCESSING``) per the
    upload contract; the pipeline then runs within this request and
    persists the final state (``COMPLETED``/``FAILED``) for retrieval
    endpoints. On pipeline failure the response reports ``FAILED`` —
    clients never receive internal error details here.

    Args:
        file: The multipart file part.
        service: Upload orchestration service.
        processing: End-to-end processing pipeline.

    Returns:
        The registered file's id, sanitized filename and status.
    """
    record: FileRecord = await service.upload(file)
    response = FileUploadResponse(
        id=record.id,
        filename=record.original_filename,
        status=record.status,
    )

    summary = processing.process(record.id)
    if summary.status == FileStatus.FAILED:
        response.status = FileStatus.FAILED
    return response


def _to_file_info(record: FileRecord) -> FileInfoResponse:
    """Map a stored record to the client-safe info response.

    Excludes ``stored_path`` and any other server-side detail. Failure
    text is re-redacted on the read path (defense in depth — a writer
    that skipped sanitization still cannot leak a path), and is exposed
    only while the record is actually ``FAILED`` so stale messages
    never accompany a successful status.

    Args:
        record: The persisted file record.

    Returns:
        The response model.
    """
    error_message = None
    if record.status == FileStatus.FAILED.value and record.error_message:
        error_message = redact_paths(record.error_message)
    return FileInfoResponse(
        id=record.id,
        filename=record.original_filename,
        file_type=record.file_type,
        status=record.status,
        feature_count=record.feature_count,
        crs=record.crs,
        error_message=error_message,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


@router.get(
    "/{file_id}/",
    response_model=FileInfoResponse,
    summary="Return metadata for one uploaded file",
    description=(
        "Return the persisted metadata of one uploaded file. The response\n    contains client-safe fields only: the sanitized filename, detected\n    input format, processing status, extracted feature count, source CRS\n    (when known) and an error message \u2014 exposed **only** while the record\n    is `FAILED`, never for a successful status. Storage paths,\n    settings and host details are never serialized.\n\n    Unrecognised (but well-formed) ids return 404 `FILE_NOT_FOUND`;\n    malformed ids (not a UUID) return 422 `VALIDATION_ERROR`.\n\n    Response format:\n    {\n      «id»: \"3f0c9f1e-8a5d-4f3a-9c2b-1d4e6a8b0c7d\",\n      «filename»: \"survey.kml\",\n      «file_type»: \"shapefile_zip\" | «kml\",\n      «status»: «UPLOADED» | «PROCESSING» | «COMPLETED» | «FAILED»,\n      «feature_count»: 120,\n      «crs»: null | «EPSG:32632»,\n      «error_message»: null | \"safe, redacted message\",\n      «created_at»: «2026-10-08T12:00:00Z»,\n      «updated_at»: «2026-10-08T12:00:01Z»\n    }\n\nPossible errors\n----------------\n- **404** `FILE_NOT_FOUND` \u2014 no record with that id;\n- **422** `VALIDATION_ERROR` \u2014 path parameter is not a UUID;\n- **500** `INTERNAL_ERROR` \u2014 unexpected server failure (generic message).\n"
    ),
    responses={
        404: {
            "description": "No file with that id.",
            "model": ErrorResponse,
        },
        422: {
            "description": "Malformed id (not a UUID).",
            "model": ErrorResponse,
        },
        500: {
            "description": "Unexpected server failure.",
            "model": ErrorResponse,
        },
    },
)
async def get_file(
    file_id: UUID,
    service: FileService = Depends(get_file_service),
) -> FileInfoResponse:
    """Return persisted metadata for one uploaded file.

    The id is validated as a UUID (malformed → 422); unknown ids map to
    404 via the domain not-found error. Only client-safe fields are
    serialized — no storage paths, settings or host details.

    Args:
        file_id: The file's UUID path parameter.
        service: File service (persistence access).

    Returns:
        The file's metadata, including a clear failure state with a
        sanitized message when processing failed.

    Raises:
        FileRecordNotFoundError: Mapped to HTTP 404 by the app handler.
    """
    record: FileRecord = service.get(str(file_id))
    return _to_file_info(record)


def _to_measurement_info(
    row: FeatureMeasurementRecord,
) -> FeatureMeasurementInfo:
    """Map a persisted measurement row to the API view.

    Only scalars cross this boundary: the measurement block is built
    from ``kind``/``value``/``unit`` and attributes are parsed from
    their JSON column — no ORM or geometry objects leak into the
    response.

    Args:
        row: The persisted row.

    Returns:
        The API view model.
    """
    measurement = None
    if row.measurement_kind and row.measurement_value is not None:
        measurement = MeasurementValue(
            type=row.measurement_kind,  # type: ignore[arg-type]
            value=row.measurement_value,
            unit=row.unit or "",
        )
    return FeatureMeasurementInfo(
        feature_id=row.feature_index,
        geometry_type=row.geometry_type,
        measurement=measurement,
        status=MeasurementCategory(row.category),
        reason=row.reason,
        properties=Properties(
            json.loads(row.properties_json or "{}")
        ),
    )


@router.get(
    "/{file_id}/measurements/",
    response_model=FileMeasurementsResponse,
    summary="Return per-feature measurements for one file",
    description=(
        "Return every feature of one uploaded file with its measurement,\n    if any, and a structured outcome. Measuring is always planar in a\n    projected, metric CRS: areas are `square_meters` (m²), lengths are\n    `meters` (m) \u2014 never degrees.\n\n    The `crs` field carries the source CRS of the features. Files never\n    processed simply return an empty `features` list; a file whose\n    processing failed returns an empty list too (check `status` via\nGET `/api/files/{id}/`).\n\n    Measurement rules\n    -----------------\n    | Geometry | Unit | Outcome |\n    |---|---|---|---|\n    | Polygon / MultiPolygon | `square_meters` | measured (SUPPORTED) |\n    | LineString / LinearRing / MultiLineString | `meters` | measured (SUPPORTED) |\n    | GeometryCollection (areal, or linear-only, or points-only) | \u2014 | \u2014 |\n    | Point / MultiPoint | \u2014 | `null` measurement, NO_MEASUREMENT |\n    | Empty / missing geometry | \u2014 | skipped_empty / skipped_missing_geometry |\n    | Invalid geometry | \u2014 | skipped_invalid |\n    | Unknown geometry type | \u2014 | skipped_unsupported_type |\n    | Undefined / unparseable CRS | \u2014 | skipped_no_crs / skipped_invalid_crs |\n\n    Response format\n    ---------------\n    {\n      \"file_id\": \"3f0c9f1e-8a5d-4f3a-9c2b-1d4e6a8b0c7d\",\n      \"crs\": \"EPSG:4326\",\n      \"features\": [\n        {\n          \"feature_id\": 0,\n          \"geometry_type\": \"Polygon\",\n          \"measurement\": {\n            \"type\": \"area\",\n            \"value\": 15234.52,\n            \"unit\": \"square_meters\"\n          },\n          \"status\": \"SUPPORTED\",\n          \"category\": \"SUPPORTED\",\n          \"reason\": null,\n          \"properties\": {\"name\": \"zone a\"}\n        }\n      ]\n    }\n\nPossible errors\n----------------\n- **404** `FILE_NOT_FOUND` \u2014 no record with that id;\n- **422** `VALIDATION_ERROR` \u2014 path parameter is not a UUID;\n- **500** `INTERNAL_ERROR` \u2014 unexpected server failure (generic message).\n"
    ),
    responses={
        404: {
            "description": "No file with that id.",
            "model": ErrorResponse,
        },
        422: {
            "description": "Malformed id (not a UUID).",
            "model": ErrorResponse,
        },
        500: {
            "description": "Unexpected server failure.",
            "model": ErrorResponse,
        },
    },
)
async def get_file_measurements(
    file_id: UUID,
    service: FileService = Depends(get_file_service),
    measurements: FeatureMeasurementRepository = Depends(
        get_feature_measurement_repository
    ),
) -> FileMeasurementsResponse:
    """Return measurements for all features of one uploaded file.

    The id is validated as a UUID (malformed → 422) and unknown ids map
    to 404. Files that were never processed simply return an empty
    feature list. The payload contains only plain JSON values — no
    GeoPandas/Shapely/ORM objects.

    Args:
        file_id: The file's UUID path parameter.
        service: File service (existence/404 handling).
        measurements: Persisted measurement rows.

    Returns:
        ``file_id``, source ``crs`` and one entry per feature with its
        geometry type, measurement block (or ``null``), structured
        status, reason and source properties.

    Raises:
        FileRecordNotFoundError: Mapped to HTTP 404 by the app handler.
    """
    record: FileRecord = service.get(str(file_id))
    rows = measurements.list_for_file(str(file_id))
    return FileMeasurementsResponse(
        file_id=record.id,
        crs=record.crs,
        features=[_to_measurement_info(row) for row in rows],
    )
