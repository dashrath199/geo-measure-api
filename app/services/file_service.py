"""File upload orchestration service.

Owns the upload lifecycle::

    validate extension → read with size limit → sniff content →
    validate archive safety → store bytes → persist record (UPLOADED) →
    transition to PROCESSING

The route stays HTTP-only; every rule above lives here. Geospatial
extraction/measurement will be slotted in between the record creation
and the final status transition in a later phase.
"""

from __future__ import annotations

import logging
import posixpath
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import FileStatus
from app.db.repository import FileRepository
from app.exceptions import FileTooLargeError
from app.geo.validation import (
    ZIP_UNCOMPRESSED_FACTOR,
    detect_file_type,
    validate_file_content,
    validate_zip_archive,
)
from app.storage.local_store import LocalStore

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 64 * 1024
_MAX_FILENAME_LEN = 255


def sanitize_filename(filename: str | None) -> str:
    """Reduce a client-supplied name to a safe basename for display/DB.

    Removes any directory components (both separators), strips control
    characters and caps the length. Never used to build storage paths —
    storage names are always server-generated UUIDs.

    Args:
        filename: Raw client-supplied name.

    Returns:
        A sanitized basename, or ``"upload"`` when nothing survives.
    """
    name = (filename or "").replace("\\", "/")
    name = posixpath.basename(name)
    name = "".join(ch for ch in name if ch.isprintable())
    name = name.strip().strip(".").strip()
    return name[:_MAX_FILENAME_LEN] or "upload"


class FileService:
    """Validates, stores and registers uploaded geospatial files.

    Args:
        session: Active database session.
        settings: Application settings (limits, storage location).
    """

    def __init__(self, session: Session, settings: Settings) -> None:
        """Bind the service to a session and configuration.

        Args:
            session: Active database session.
            settings: Application settings.
        """
        self._repo = FileRepository(session)
        self._settings = settings
        self._store = LocalStore(settings.storage_dir)

    @property
    def max_upload_bytes(self) -> int:
        """Return the configured upload limit in bytes."""
        return self._settings.max_upload_mb * 1024 * 1024

    def _read_limited(self, upload: UploadFile) -> bytes:
        """Read the whole upload, aborting as soon as the limit is passed.

        Reads the already-spooled upload file directly: the route runs
        in FastAPI's threadpool, so synchronous file I/O is the right
        model here (the multipart parser has fully buffered the part
        and left it positioned at the start).

        Args:
            upload: The multipart upload.

        Returns:
            The complete content.

        Raises:
            FileTooLargeError: Once bytes read exceed the configured cap.
        """
        limit = self.max_upload_bytes
        buffer = bytearray()
        while True:
            chunk = upload.file.read(_CHUNK_SIZE)
            if not chunk:
                break
            buffer.extend(chunk)
            if len(buffer) > limit:
                raise FileTooLargeError(limit)
        return bytes(buffer)

    def upload(self, upload: UploadFile) -> object:
        """Process one upload end-to-end.

        Synchronous by design: the caller (``POST /api/files/``) is a
        plain ``def`` route executed in the threadpool, keeping CPU and
        file work off the event loop.

        Args:
            upload: The multipart file part.

        Returns:
            The persisted ``FileRecord`` in ``PROCESSING`` state.

        Raises:
            UnsupportedFormatError: Extension not in the allowed set.
            FileTooLargeError: Content exceeds ``settings.max_upload_mb``.
            InvalidFileContentError-family: Content does not match the
                declared format (mapped to HTTP 400).
            InvalidArchiveError: Corrupt/unsafe/incomplete shapefile ZIP.
        """
        original_filename = sanitize_filename(upload.filename)
        file_type = detect_file_type(original_filename)
        data = self._read_limited(upload)
        validate_file_content(file_type, data)

        if file_type == "shapefile_zip":
            inspection = validate_zip_archive(
                data,
                max_total_uncompressed=(
                    self.max_upload_bytes * ZIP_UNCOMPRESSED_FACTOR
                ),
            )
            logger.info(
                "Validated shapefile ZIP: %d dataset(s), prj=%s",
                inspection.shapefile_count,
                inspection.has_prj,
            )

        suffix = Path(original_filename).suffix.lower()
        stored_path = self._store.save(data, suffix)
        try:
            record = self._repo.create(
                original_filename=original_filename,
                stored_path=str(stored_path),
                file_type=file_type,
                status=FileStatus.UPLOADED,
            )
            record = self._repo.set_status(record, FileStatus.PROCESSING)
        except Exception:
            # Don't leave orphaned bytes behind if persistence fails.
            self._store.delete(stored_path)
            raise

        logger.info(
            "Upload registered: id=%s file=%r type=%s",
            record.id,
            original_filename,
            file_type,
        )
        return record

    def get(self, file_id: str) -> object:
        """Fetch one stored file record by id.

        Args:
            file_id: The file's UUID string.

        Returns:
            The persisted ``FileRecord``.

        Raises:
            FileRecordNotFoundError: When no record has that id.
        """
        return self._repo.get(file_id)
