"""End-to-end processing pipeline for uploaded files.

Connects the upload side to the geospatial side::

    stored upload → detect format → extract/read → normalized features
        → CRS handling → measurements → persist summary → COMPLETED

Failure policy:

- Every failure marks the file ``FAILED`` with a **safe** message:
  domain errors are stored with server paths redacted; unexpected
  errors are replaced by a generic message — internal stack traces and
  exception internals never reach API clients.
- Full tracebacks are logged for developers (``exc_info`` / exception
  logging).
- Per-feature problems never fail the file: the measurement engine
  returns structured per-feature statuses, and only pipeline-level
  failures (unreadable archive, corrupt dataset) mark ``FAILED``.

The pipeline lives in the service layer — routes only call it.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import FileRecord, FileStatus
from app.db.repository import FeatureMeasurementRepository, FileRepository
from app.exceptions import (
    DatasetTooLargeError,
    GeometryProcessingError,
    GeoAppError,
    redact_paths,
)
from app.geo.processors import GeospatialFile, get_processor
from app.geo.validation import ZIP_UNCOMPRESSED_FACTOR
from app.geo.zip_extraction import extract_shapefile_zip
from app.measurements.service import MeasurementService
from app.schemas.measurement import FeatureMeasurement

logger = logging.getLogger(__name__)

# Stored verbatim when an unexpected exception occurs — no internals.
_GENERIC_ERROR_MESSAGE = "Unexpected error during processing"


@dataclass(frozen=True)
class ProcessingSummary:
    """Outcome of running the pipeline for one file.

    Attributes:
        file_id: The processed file's id.
        status: Final status (``COMPLETED`` or ``FAILED``).
        feature_count: Number of features extracted.
        crs: Source CRS authority code, when defined.
        category_counts: Feature counts per measurement category.
        error_message: Safe failure text when ``FAILED``.
    """

    file_id: str
    status: FileStatus
    feature_count: int = 0
    crs: str | None = None
    category_counts: dict[str, int] = field(default_factory=dict)
    error_message: str | None = None


class ProcessingService:
    """Runs the upload → measurement pipeline for stored files.

    Args:
        session: Active database session.
        settings: Application settings (limits, storage location).
    """

    def __init__(self, session: Session, settings: Settings) -> None:
        """Bind the pipeline to persistence and configuration.

        Args:
            session: Active database session.
            settings: Application settings.
        """
        self._files = FileRepository(session)
        self._measurements = FeatureMeasurementRepository(session)
        self._settings = settings
        self._measurement_service = MeasurementService()

    # ------------------------------------------------------------ reading

    def _read_geospatial(self, record: FileRecord) -> GeospatialFile:
        """Detect the format and extract/read the stored upload.

        Shapefile ZIPs are extracted with the secure extractor into a
        per-run temporary directory (removed afterwards); KML is read
        in place.

        Args:
            record: The stored file's database record.

        Returns:
            The normalized geospatial file.

        Raises:
            GeometryProcessingError-family: When the stored bytes are
                unreadable, unsafe or not valid for their format.
        """
        processor = get_processor(record.file_type)
        stored = Path(record.stored_path)
        if not stored.is_file():
            # Domain error (safe to surface) rather than a raw
            # FileNotFoundError escaping the extraction layer.
            raise GeometryProcessingError(
                f"Stored file is missing: {stored}"
            )
        if record.file_type == "shapefile_zip":
            with tempfile.TemporaryDirectory(prefix="geo-process-") as tmp:
                extraction = extract_shapefile_zip(
                    stored,
                    Path(tmp),
                    max_total_uncompressed=(
                        self._settings.max_upload_mb
                        * 1024
                        * 1024
                        * ZIP_UNCOMPRESSED_FACTOR
                    ),
                )
                return processor.process(extraction.primary.shp)
        return processor.process(stored)

    # ------------------------------------------------------- error safety

    def _fail(
        self, record: FileRecord, message: str
    ) -> ProcessingSummary:
        """Mark a record FAILED and build its summary.

        Args:
            record: The record to fail.
            message: Safe error message.

        Returns:
            The failure summary.
        """
        self._files.set_status(
            record,
            FileStatus.FAILED,
            error_message=message,
        )
        logger.error(
            "Processing FAILED for file %s (%r): %s",
            record.id,
            record.original_filename,
            message,
        )
        return ProcessingSummary(
            file_id=record.id,
            status=FileStatus.FAILED,
            error_message=message,
        )

    # ----------------------------------------------------------- pipeline

    def process(self, file_id: str) -> ProcessingSummary:
        """Run the full pipeline for one stored file.

        Steps: load record → PROCESSING → detect format → extract/read
        → features + CRS → measure → persist per-feature summary →
        COMPLETED. Any pipeline-level failure → FAILED with a safe
        message (never raised to the caller).

        Args:
            file_id: The file record's id.

        Returns:
            The processing summary (``COMPLETED`` or ``FAILED``).

        Raises:
            FileRecordNotFoundError: Only if the record itself is gone.
        """
        record = self._files.get(file_id)
        try:
            self._files.set_status(record, FileStatus.PROCESSING)
            logger.info("Processing file %s (%s)", file_id, record.file_type)

            geospatial = self._read_geospatial(record)
            if geospatial.feature_count > self._settings.max_features:
                # Byte limits bound the upload; this bounds the work
                # and response size derived from it.
                raise DatasetTooLargeError(
                    f"Dataset contains too many features "
                    f"({geospatial.feature_count} > "
                    f"{self._settings.max_features} allowed)"
                )
            results: list[FeatureMeasurement] = (
                self._measurement_service.measure_file(geospatial)
            )
            self._measurements.replace_for_file(file_id, results)

            category_counts: dict[str, int] = {}
            for result in results:
                key = result.category.value
                category_counts[key] = category_counts.get(key, 0) + 1

            self._files.set_status(
                record,
                FileStatus.COMPLETED,
                feature_count=geospatial.feature_count,
                crs=geospatial.source_crs.authority_code,
            )
            logger.info(
                "Processing COMPLETED for file %s: %d feature(s), crs=%s, "
                "categories=%s",
                file_id,
                geospatial.feature_count,
                geospatial.source_crs.authority_code,
                category_counts,
            )
            return ProcessingSummary(
                file_id=file_id,
                status=FileStatus.COMPLETED,
                feature_count=geospatial.feature_count,
                crs=geospatial.source_crs.authority_code,
                category_counts=category_counts,
            )

        except GeoAppError as exc:
            # Domain messages are curated; redact paths and keep logs
            # complete for developers.
            logger.error(
                "Processing failed for file %s: %s",
                file_id,
                exc,
                exc_info=True,
            )
            return self._fail(record, redact_paths(str(exc)))
        except Exception:  # noqa: BLE001 - pipeline boundary
            # Full traceback for developers… …but never for clients.
            logger.exception(
                "Unexpected processing failure for file %s", file_id
            )
            return self._fail(record, _GENERIC_ERROR_MESSAGE)
