"""Application exception hierarchy.

Domain errors derive from :class:`GeoAppError` so the API layer can map
them to HTTP responses in one place instead of scattering ``try/except``
blocks through the routes. Every error carries two attributes the API
layer relies on:

``code``
    Stable, machine-readable identifier (e.g. ``UNSUPPORTED_FILE_TYPE``)
    returned to clients so integrations can branch on the error kind.
    Set per class, overridable per instance for errors whose class
    covers several scenarios (``InvalidFileError``, ``InvalidArchiveError``,
    ``GeometryProcessingError``).
``status_code``
    The HTTP status the error maps to.

The module also hosts :func:`redact_paths`, the single helper that
keeps filesystem paths out of anything returned or stored for clients.
"""

from __future__ import annotations

import re

# Any whitespace-delimited token containing a path separator is a path.
_PATHISH = re.compile(r"[^\s\"'(),;]*[\\/][^\s\"'(),;]*")
_MAX_ERROR_MESSAGE = 500


def redact_paths(message: str) -> str:
    """Make an error message safe to show clients.

    Directory components are replaced by ``<path>/`` (only the final
    filename survives — something the client already knows), the text
    is collapsed to a single line and capped at
    ``_MAX_ERROR_MESSAGE`` characters. Idempotent: redacting an already
    redacted message is a no-op. Used both when persisting failures and
    when serving them, so paths can never leak even if a writer
    forgets.

    Args:
        message: Raw error text.

    Returns:
        A single-line, path-free message.
    """

    def _mask(match: re.Match[str]) -> str:
        token = match.group(0).replace("\\", "/").rstrip("/")
        return f"<path>/{token.rsplit('/', 1)[-1]}"

    message = _PATHISH.sub(_mask, message)
    return " ".join(message.split())[:_MAX_ERROR_MESSAGE]


class GeoAppError(Exception):
    """Base class for all domain-specific errors raised by this service.

    Attributes:
        code: Machine-readable error code surfaced in API responses.
        status_code: HTTP status the API layer responds with.
    """

    code: str = "INTERNAL_ERROR"
    status_code: int = 400

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        """Build the error.

        Args:
            message: Human-readable, client-safe explanation.
            code: Optional per-instance override of the class code, for
                errors whose class spans several scenarios.
        """
        super().__init__(message)
        if code is not None:
            self.code = code


class GeospatialDependencyError(GeoAppError):
    """Raised when a required geospatial dependency is missing or broken.

    The message lists every missing package together with an install
    hint so the failure is immediately actionable. Surfaced as HTTP 503
    (service unavailable) if it ever reaches a client.
    """

    code = "GEOSPATIAL_UNAVAILABLE"
    status_code = 503


class UnsupportedFormatError(GeoAppError):
    """Raised when an upload's extension/format is not supported.

    Mapped to HTTP 415 by the API layer.
    """

    code = "UNSUPPORTED_FILE_TYPE"
    status_code = 415


class InvalidFileError(GeoAppError):
    """Raised when file content fails validation (bad magic bytes, etc.).

    Mapped to HTTP 400. The class default covers general content mismatches;
    raise sites pass a precise ``code`` (``MALFORMED_KML``, ``MALFORMED_ZIP``)
    when the declared format is known.
    """

    code = "INVALID_FILE"
    status_code = 400


class InvalidArchiveError(GeoAppError):
    """Raised for malformed or unsafe ZIP archives.

    Covers corrupt archives (``MALFORMED_ZIP``, the class default),
    unsafe member paths and zip-bomb limits (``UNSAFE_ARCHIVE``),
    missing Shapefile components (``MISSING_SHAPEFILE_COMPONENT``) and
    archives without a usable dataset (``INVALID_SHAPEFILE``).
    Mapped to HTTP 400.
    """

    code = "MALFORMED_ZIP"
    status_code = 400


class FileTooLargeError(GeoAppError):
    """Raised when an upload exceeds the configured size limit.

    Mapped to HTTP 413 by the API layer.

    Attributes:
        max_bytes: The configured limit that was exceeded.
    """

    code = "FILE_TOO_LARGE"
    status_code = 413

    def __init__(self, max_bytes: int) -> None:
        """Build the error for a given limit.

        Args:
            max_bytes: The configured maximum upload size in bytes.
        """
        self.max_bytes = max_bytes
        max_mb = max_bytes // (1024 * 1024)
        super().__init__(
            f"Uploaded file exceeds the maximum allowed size of {max_mb} MB"
        )


class StorageError(GeoAppError):
    """Raised when a file cannot be written to or removed from storage.

    Maps to HTTP 500: the client did nothing wrong (disk full,
    permissions, broken storage root), so the response is a generic
    server error with the ``STORAGE_FAILURE`` code; details go to logs.
    """

    code = "STORAGE_FAILURE"
    status_code = 500


class CRSError(GeoAppError):
    """Base class for coordinate-reference-system failures.

    Mapped to HTTP 400 by the API layer's ``GeoAppError`` catch-all.
    """

    code = "CRS_ERROR"
    status_code = 400


class MissingCRSError(CRSError):
    """Raised when a dataset has no CRS metadata.

    The service refuses to assume a default (in particular it never
    assumes EPSG:4326): measurements are impossible without knowing the
    coordinate units, so callers must skip measurement explicitly.
    """

    code = "MISSING_CRS"


class InvalidCRSError(CRSError):
    """Raised when CRS metadata cannot be parsed into a usable CRS.

    Wraps pyproj failures in a domain error instead of leaking them.
    """

    code = "INVALID_CRS"


class EmptyGeometryError(CRSError):
    """Raised when measurement is attempted on an empty/missing geometry.

    Subclasses :class:`CRSError` because it is raised during CRS
    preparation — an empty geometry has no extent to project. Callers
    turn it into an explicit skip rather than a fabricated number.
    """

    code = "EMPTY_GEOMETRY"


class DatasetTooLargeError(GeoAppError):
    """Raised when a parsed dataset exceeds the configured feature cap.

    Upload size limits bound bytes; this bounds *features* — the axis
    that drives processing time and response size. Fails the file with
    an explicit message instead of grinding through an unbounded
    dataset. Mapped to HTTP 400 should it ever surface directly.
    """

    code = "TOO_MANY_FEATURES"
    status_code = 400


class GeometryProcessingError(GeoAppError):
    """Raised when a geospatial dataset cannot be read or parsed.

    The class default (``PROCESSING_FAILURE``) covers pipeline-level
    read/parse failures; raise sites pass a precise ``code``
    (``MALFORMED_KML``, ``INVALID_SHAPEFILE``) when the format is known.
    Mapped to HTTP 400 by the API layer's ``GeoAppError`` catch-all.
    """

    code = "PROCESSING_FAILURE"
    status_code = 400


class InvalidGeometryError(GeometryProcessingError):
    """Raised when a dataset's geometry column is unusable.

    Distinct from *invalid geometry values* (self-intersecting shapes),
    which are detected per feature and reported as skip statuses — this
    error means there is no geometry at all to work with.
    """

    code = "INVALID_GEOMETRY"


class UnsupportedGeometryError(GeometryProcessingError):
    """Raised when the geometry slot holds something that is not a
    supported (Shapely) geometry.

    Per-feature geometry problems never raise: they are isolated as
    skip statuses. This error is the reader's last-resort guard.
    """

    code = "UNSUPPORTED_GEOMETRY"


class FileRecordNotFoundError(GeoAppError):
    """Raised when a requested file record does not exist.

    Mapped to HTTP 404 by the API layer.
    """

    code = "FILE_NOT_FOUND"
    status_code = 404

    def __init__(self, file_id: str) -> None:
        """Build the error for a specific identifier.

        Args:
            file_id: The identifier that was looked up.
        """
        self.file_id = file_id
        super().__init__(f"File record not found: {file_id}")
