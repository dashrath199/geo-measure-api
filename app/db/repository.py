"""Repository abstraction for file metadata persistence.

Routes/services depend on this interface rather than on raw SQLAlchemy,
which keeps SQL concerns out of the business layer and makes the
persistence strategy swappable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import FeatureMeasurementRecord, FileRecord, FileStatus
from app.exceptions import FileRecordNotFoundError
from app.schemas.measurement import (
    AreaMeasurement,
    FeatureMeasurement,
    LengthMeasurement,
)

logger = logging.getLogger(__name__)


class FileRepository:
    """CRUD operations over :class:`FileRecord` rows.

    The repository commits on ``create``/``update``; callers own the
    surrounding transaction boundaries for multi-step operations.

    Args:
        session: Active SQLAlchemy session to operate within.
    """

    def __init__(self, session: Session) -> None:
        """Bind the repository to a session.

        Args:
            session: The active SQLAlchemy session.
        """
        self._session = session

    def create(
        self,
        *,
        original_filename: str,
        stored_path: str,
        file_type: str,
        status: FileStatus = FileStatus.UPLOADED,
    ) -> FileRecord:
        """Insert a new file record in ``UPLOADED`` state by default.

        Args:
            original_filename: Client-provided filename.
            stored_path: Server-side storage path.
            file_type: Detected format identifier.
            status: Initial lifecycle state.

        Returns:
            The persisted record (refreshed from the database).
        """
        record = FileRecord(
            original_filename=original_filename,
            stored_path=stored_path,
            file_type=file_type,
            status=status.value,
        )
        self._session.add(record)
        self._session.commit()
        self._session.refresh(record)
        return record

    def get(self, file_id: str) -> FileRecord:
        """Fetch a record by its UUID.

        Args:
            file_id: UUID of the record.

        Returns:
            The matching :class:`FileRecord`.

        Raises:
            FileRecordNotFoundError: If no record has that identifier.
        """
        record = self._session.get(FileRecord, file_id)
        if record is None:
            raise FileRecordNotFoundError(file_id)
        return record

    def list(self, *, limit: int = 50, offset: int = 0) -> list[FileRecord]:
        """List records newest-first.

        Args:
            limit: Maximum number of rows to return.
            offset: Number of rows to skip.

        Returns:
            The requested page of records.
        """
        stmt = (
            select(FileRecord)
            .order_by(FileRecord.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(stmt))

    def update(self, record: FileRecord) -> FileRecord:
        """Persist changes made to a record in memory.

        Refreshes ``updated_at`` explicitly so callers don't rely solely
        on the in-Python ``onupdate`` default.

        Args:
            record: The dirty record.

        Returns:
            The refreshed record.
        """
        record.updated_at = datetime.now(timezone.utc)
        self._session.add(record)
        self._session.commit()
        self._session.refresh(record)
        return record

    def set_status(
        self,
        record: FileRecord,
        status: FileStatus,
        *,
        error_message: str | None = None,
        feature_count: int | None = None,
        crs: str | None = None,
    ) -> FileRecord:
        """Transition a record to a new status, optionally updating results.

        Args:
            record: Record to transition.
            status: Target status.
            error_message: Failure detail (typically with ``FAILED``).
            feature_count: Feature count to persist (with ``COMPLETED``).
            crs: Source CRS to persist (with ``COMPLETED``).

        Returns:
            The updated record.
        """
        record.status = status.value
        if error_message is not None:
            record.error_message = error_message
        if feature_count is not None:
            record.feature_count = feature_count
        if crs is not None:
            record.crs = crs
        return self.update(record)


class FeatureMeasurementRepository:
    """CRUD for per-feature measurement results.

    Args:
        session: Active SQLAlchemy session to operate within.
    """

    def __init__(self, session: Session) -> None:
        """Bind the repository to a session.

        Args:
            session: The active SQLAlchemy session.
        """
        self._session = session

    @staticmethod
    def _split_measurement(
        result: FeatureMeasurement,
    ) -> tuple[str | None, float | None, str | None]:
        """Flatten a result's measurement union into scalar columns.

        Args:
            result: The per-feature measurement.

        Returns:
            ``(kind, value, unit)`` — all ``None`` when unmeasured.
        """
        measurement = result.measurement
        if isinstance(measurement, AreaMeasurement):
            return "area", measurement.area, measurement.unit
        if isinstance(measurement, LengthMeasurement):
            return "length", measurement.length, measurement.unit
        return None, None, None

    def replace_for_file(
        self, file_id: str, results: list[FeatureMeasurement]
    ) -> None:
        """Persist a file's results, replacing any previous run.

        Args:
            file_id: Owning file id.
            results: Per-feature measurements from the engine.
        """
        self._session.execute(
            delete(FeatureMeasurementRecord).where(
                FeatureMeasurementRecord.file_id == file_id
            )
        )
        for result in results:
            kind, value, unit = self._split_measurement(result)
            self._session.add(
                FeatureMeasurementRecord(
                    file_id=file_id,
                    feature_index=result.feature_id,
                    geometry_type=result.geometry_type,
                    measurement_kind=kind,
                    measurement_value=value,
                    unit=unit,
                    status=result.status.value,
                    category=result.category.value,
                    measurement_crs=result.measurement_crs,
                    reason=result.reason,
                    properties_json=result.properties.model_dump_json(),
                )
            )
        self._session.commit()
        logger.info(
            "Stored %d measurement row(s) for file %s", len(results), file_id
        )

    def list_for_file(self, file_id: str) -> list[FeatureMeasurementRecord]:
        """Return a file's measurement rows in feature order.

        Args:
            file_id: Owning file id.

        Returns:
            The persisted rows.
        """
        stmt = (
            select(FeatureMeasurementRecord)
            .where(FeatureMeasurementRecord.file_id == file_id)
            .order_by(FeatureMeasurementRecord.feature_index)
        )
        return list(self._session.scalars(stmt))
