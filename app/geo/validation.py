"""Upload file validation: extension, content and archive safety.

Pure functions with no storage or persistence side effects — the service
calls these before anything is written to disk or database.

Archive safety policy:
    Archives are never extracted during validation. Member *names* are
    inspected for traversal payloads (``..``, absolute paths, drive
    letters) and the shapefile component trio is required per ``.shp``.
    Any later extraction step only copies whitelisted, sanitized members
    into a fresh, per-request directory.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.exceptions import (
    InvalidArchiveError,
    InvalidFileError,
    UnsupportedFormatError,
)

# Extension → internal file-type identifier.
SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".zip": "shapefile_zip",
    ".kml": "kml",
}

# Components every shapefile inside the archive must provide.
SHAPEFILE_REQUIRED_SUFFIXES: tuple[str, ...] = (".shx", ".dbf")
# Optional sidecars; .prj presence is recorded but absence is not an error.
SHAPEFILE_PRJ_SUFFIX = ".prj"

# Sanity limits guarding against zip bombs and member-flood attacks.
MAX_ZIP_MEMBERS = 5_000
# Total uncompressed bytes allowed relative to the max upload size.
ZIP_UNCOMPRESSED_FACTOR = 20

ZIP_MAGIC = b"PK\x03\x04"
# Bytes of content inspected when sniffing formats.
SNIFF_BYTES = 8_192

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class ZipInspection:
    """Findings from validating a shapefile ZIP archive.

    Attributes:
        shapefile_count: Number of ``.shp`` datasets found.
        has_prj: Whether at least one ``.prj`` sidecar was present.
    """

    shapefile_count: int
    has_prj: bool


def detect_file_type(filename: str | None) -> str:
    """Resolve the internal file type from the upload's extension.

    Args:
        filename: Client-supplied filename (used for extension only).

    Returns:
        ``"zip"`` maps to ``"shapefile_zip"``; ``"kml"`` for KML.

    Raises:
        UnsupportedFormatError: If the extension is not supported.
    """
    suffix = Path(filename or "").suffix.lower()
    file_type = SUPPORTED_EXTENSIONS.get(suffix)
    if file_type is None:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        supplied = suffix or "no extension"
        raise UnsupportedFormatError(
            f"Unsupported file format ({supplied}); supported: {supported}"
        )
    return file_type


def reject_xml_entities(data: bytes) -> None:
    """Reject XML documents carrying DTD entity declarations.

    KML never needs a DTD, but entities enable XML-bomb / billion-laughs
    amplification and (in parsers that resolve external entities) XXE
    file disclosure. Scanned across the **whole** document — declarations
    past the sniff window are common in crafted payloads.

    Args:
        data: Full document bytes.

    Raises:
        InvalidFileError: If a ``DOCTYPE``/``ENTITY`` declaration is
            present (code ``UNSAFE_CONTENT``).
    """
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise InvalidFileError(
            "KML documents with DTD or ENTITY declarations are not "
            "allowed",
            code="UNSAFE_CONTENT",
        )


def validate_file_content(file_type: str, data: bytes) -> None:
    """Sanity-check the leading bytes of an upload.

    Practical, cheap content sniffing — not a full parse:

    - ``shapefile_zip`` must start with the ZIP local-file-header magic.
    - ``kml`` must contain a ``<kml`` root element near the start and
      must not carry DTD/entity declarations (see
      :func:`reject_xml_entities`, applied to the whole document).

    Args:
        file_type: Internal file type from :func:`detect_file_type`.
        data: File content (the beginning is sniffed; entity scanning
            sees the full document).

    Raises:
        InvalidFileError: If the content does not match the format or
            contains unsafe XML constructs.
    """
    head = data[:SNIFF_BYTES]
    if file_type == "shapefile_zip":
        if not head.startswith(ZIP_MAGIC):
            raise InvalidFileError(
                "File claims to be a ZIP archive but has invalid magic bytes",
                code="MALFORMED_ZIP",
            )
        return

    # KML: XML document whose root element is <kml> (namespace-tolerant).
    reject_xml_entities(data)
    lowered = head.lower()
    if b"<kml" not in lowered:
        raise InvalidFileError(
            "File does not look like KML (no <kml root element found)",
            code="MALFORMED_KML",
        )


def is_unsafe_member_path(name: str) -> bool:
    """Report whether a ZIP member name could escape the extraction root.

    Public so the extraction service enforces the exact same rule as
    upload validation (defense in depth: both layers check).

    Args:
        name: Raw member name from the archive.

    Returns:
        ``True`` for absolute paths, drive letters, ``..`` segments or
        embedded NUL bytes.
    """
    if not name or "\x00" in name:
        # Empty names and NUL-truncated names are classic parser-
        # confusion payloads.
        return True
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or _DRIVE_RE.match(normalized):
        return True
    return any(part == ".." for part in normalized.split("/"))


def validate_zip_archive(
    data: bytes, *, max_total_uncompressed: int
) -> ZipInspection:
    """Validate a shapefile ZIP without extracting it.

    Checks performed, in order:

    1. Archive opens (corrupt central directory → malformed).
    2. Member count within :data:`MAX_ZIP_MEMBERS`.
    3. No unsafe member paths (``..``, absolute, drive-letter).
    4. Total uncompressed size within limits (zip-bomb guard).
    5. Every ``.shp`` has sibling ``.shx`` and ``.dbf`` (same folder);
       ``.prj`` presence is recorded when available.

    Args:
        data: Raw archive bytes.
        max_total_uncompressed: Maximum allowed total uncompressed bytes.

    Returns:
        Findings about shapefile datasets and ``.prj`` presence.

    Raises:
        InvalidArchiveError: For any structural or safety violation.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise InvalidArchiveError(f"Malformed ZIP archive: {exc}") from exc

    with archive:
        infos = archive.infolist()

        if len(infos) > MAX_ZIP_MEMBERS:
            raise InvalidArchiveError(
                f"ZIP archive contains too many members "
                f"({len(infos)} > {MAX_ZIP_MEMBERS})",
                code="UNSAFE_ARCHIVE",
            )

        for info in infos:
            if is_unsafe_member_path(info.filename):
                raise InvalidArchiveError(
                    f"ZIP archive contains unsafe member path: "
                    f"{info.filename!r} (path traversal rejected)",
                    code="UNSAFE_ARCHIVE",
                )
            if info.flag_bits & 0x1:
                raise InvalidArchiveError(
                    f"Encrypted ZIP members are not supported: "
                    f"{info.filename!r}",
                    code="UNSAFE_ARCHIVE",
                )

        total_uncompressed = sum(info.file_size for info in infos)
        if total_uncompressed > max_total_uncompressed:
            raise InvalidArchiveError(
                "ZIP archive uncompressed size exceeds the allowed limit "
                f"({total_uncompressed} > {max_total_uncompressed} bytes)",
                code="UNSAFE_ARCHIVE",
            )

        file_names = [
            info.filename.replace("\\", "/")
            for info in infos
            if not info.is_dir()
        ]
        # Lowercased lookup so component matching is case-insensitive.
        present = {name.lower() for name in file_names}
        shp_names = [n for n in file_names if n.lower().endswith(".shp")]

        if not shp_names:
            raise InvalidArchiveError(
                "ZIP archive does not contain a .shp file — "
                "not a shapefile archive",
                code="INVALID_SHAPEFILE",
            )

        has_prj = False
        for shp in shp_names:
            stem = shp.lower()[:-4]  # strip ".shp"
            for suffix in SHAPEFILE_REQUIRED_SUFFIXES:
                if stem + suffix not in present:
                    raise InvalidArchiveError(
                        f"Shapefile '{shp}' is missing required component "
                        f"'{Path(stem).name}{suffix}'",
                        code="MISSING_SHAPEFILE_COMPONENT",
                    )
            if stem + SHAPEFILE_PRJ_SUFFIX in present:
                has_prj = True

        return ZipInspection(
            shapefile_count=len(shp_names),
            has_prj=has_prj,
        )
