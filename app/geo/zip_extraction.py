"""Secure extraction of shapefile ZIP archives.

A framework-agnostic service component — it imports nothing from
FastAPI/HTTP and exposes plain functions and value objects, so it can be
called from request handlers, background jobs or CLIs alike.

Security model (validated for **every** archive at extraction time, never
trusting an earlier check):

1. Member names are rejected when absolute (``/…``, ``C:…``, UNC) or when
   any ``..`` segment appears — the Zip Slip payload.
2. A second, independent guard resolves each output path and requires it
   to stay inside the destination directory.
3. Symlink members are rejected outright.
4. A strict extension whitelist decides what is written: only shapefile
   components (``.shp .shx .dbf .prj`` plus common sidecars) are ever
   extracted — executable payloads and foreign file types never touch
   disk, and archived permission bits are never applied.
5. Nested archives are rejected as explicitly unsupported (no
   recursive unpacking).
6. Bomb guards: member count and total declared uncompressed size.

Non-component documentation files (``README``, ``.DS_Store``,
``__MACOSX`` metadata) are skipped, not fatal. Missing mandatory
components (``.shp/.shx/.dbf``) produce a clear validation error.

On failure, a destination directory created by this module is removed
again; callers supplying a pre-existing directory should use a fresh
per-job directory and discard it on error.
"""

from __future__ import annotations

import io
import logging
import shutil
import stat
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

from app.exceptions import InvalidArchiveError
from app.geo.validation import MAX_ZIP_MEMBERS, is_unsafe_member_path

logger = logging.getLogger(__name__)

# Only these extensions may be written to disk (shapefile components).
SHAPEFILE_COMPONENT_SUFFIXES: frozenset[str] = frozenset(
    {".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qix", ".fix"}
)
# Without these the dataset cannot be read.
MANDATORY_COMPONENT_SUFFIXES: tuple[str, ...] = (".shp", ".shx", ".dbf")

# Recursive unpacking is explicitly out of scope — reject, don't recurse.
NESTED_ARCHIVE_SUFFIXES: frozenset[str] = frozenset(
    {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".zst"}
)
# Scripts/programs are never treated as geospatial data — reject loudly.
EXECUTABLE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".exe", ".bat", ".cmd", ".com", ".scr", ".msi", ".dll",
        ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
        ".sh", ".bash", ".zsh", ".py", ".pyc", ".rb", ".pl", ".jar",
        ".apk", ".app", ".deb", ".rpm",
    }
)
# AppleDouble/junk directories whose contents are metadata, not data.
JUNK_DIR_NAMES: frozenset[str] = frozenset({"__macosx"})

DEFAULT_MAX_TOTAL_UNCOMPRESSED = 50 * 1024 * 1024


@dataclass(frozen=True)
class ShapefileComponents:
    """Identified files of one shapefile dataset on disk.

    Attributes:
        directory: Directory holding the dataset.
        shp: Path of the geometry file.
        shx: Path of the shape index file.
        dbf: Path of the attribute table.
        prj: Path of the projection file, when present.
    """

    directory: Path
    shp: Path
    shx: Path
    dbf: Path
    prj: Path | None

    @property
    def has_prj(self) -> bool:
        """Return whether a ``.prj`` sidecar was found."""
        return self.prj is not None


@dataclass(frozen=True)
class ExtractionResult:
    """Outcome of a successful extraction.

    Attributes:
        destination: Directory the files were written into.
        datasets: Every shapefile dataset identified (at least one).
        extracted: Original member names that were written.
        skipped: Original member names deliberately left out (junk).
    """

    destination: Path
    datasets: tuple[ShapefileComponents, ...]
    extracted: tuple[str, ...]
    skipped: tuple[str, ...]

    @property
    def primary(self) -> ShapefileComponents:
        """Return the first identified dataset."""
        return self.datasets[0]


@contextmanager
def _open_zip(source: bytes | str | Path | IO[bytes]) -> Iterator[zipfile.ZipFile]:
    """Open a ZIP from bytes, a path or a file object.

    Args:
        source: Archive bytes, filesystem path, or binary file object.

    Yields:
        The opened archive.

    Raises:
        InvalidArchiveError: If the archive cannot be opened.
    """
    try:
        if isinstance(source, (str, Path)):
            archive = zipfile.ZipFile(source)
        elif isinstance(source, bytes):
            archive = zipfile.ZipFile(io.BytesIO(source))
        else:
            archive = zipfile.ZipFile(source)
    except zipfile.BadZipFile as exc:
        raise InvalidArchiveError(f"Malformed ZIP archive: {exc}") from exc
    try:
        yield archive
    finally:
        archive.close()


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    """Report whether a member stores a symbolic link.

    Args:
        info: The archive member.

    Returns:
        ``True`` if the stored unix mode marks a symlink.
    """
    return stat.S_ISLNK(info.external_attr >> 16)


def _target_path(destination: Path, member_name: str) -> Path:
    """Map a (pre-validated) member name to a path inside ``destination``.

    Applies the independent Zip Slip guard: the resolved target must
    remain inside the resolved destination.

    Args:
        destination: Extraction root.
        member_name: Forward-slash-normalized member name.

    Returns:
        The absolute target path.

    Raises:
        InvalidArchiveError: If the resolved path escapes the root.
    """
    relative = PurePosixPath(member_name)
    target = destination.joinpath(*relative.parts)
    if not target.resolve().is_relative_to(destination.resolve()):
        raise InvalidArchiveError(
            f"ZIP member would escape the extraction directory: "
            f"{member_name!r}",
            code="UNSAFE_ARCHIVE",
        )
    return target


def _find_sibling(directory: Path, stem: str, suffix: str) -> Path | None:
    """Find a sidecar sharing a stem in one directory (case-insensitive).

    Args:
        directory: Directory to search.
        stem: Filename without extension, as of the ``.shp``.
        suffix: Extension to look for (with dot).

    Returns:
        The matching path, or ``None``.
    """
    wanted = (stem + suffix).lower()
    for child in directory.iterdir():
        if child.is_file() and child.name.lower() == wanted:
            return child
    return None


def _identify_datasets(destination: Path) -> tuple[ShapefileComponents, ...]:
    """Locate extracted datasets and verify mandatory components on disk.

    Args:
        destination: Extraction root.

    Returns:
        One :class:`ShapefileComponents` per dataset, sorted by path.

    Raises:
        InvalidArchiveError: If no dataset exists or a mandatory
            component (``.shx``/``.dbf``) is missing.
    """
    shp_files = sorted(
        path
        for path in destination.rglob("*")
        if path.is_file() and path.suffix.lower() == ".shp"
    )
    if not shp_files:
        raise InvalidArchiveError(
            "ZIP archive does not contain a .shp file — "
            "not a shapefile archive",
            code="INVALID_SHAPEFILE",
        )

    datasets: list[ShapefileComponents] = []
    for shp in shp_files:
        stem = shp.name[: -len(".shp")]
        shx = _find_sibling(shp.parent, stem, ".shx")
        dbf = _find_sibling(shp.parent, stem, ".dbf")
        if shx is None or dbf is None:
            missing = ".shx" if shx is None else ".dbf"
            raise InvalidArchiveError(
                f"Shapefile '{shp.name}' is missing mandatory component "
                f"'{stem}{missing}'",
                code="MISSING_SHAPEFILE_COMPONENT",
            )
        datasets.append(
            ShapefileComponents(
                directory=shp.parent,
                shp=shp,
                shx=shx,
                dbf=dbf,
                prj=_find_sibling(shp.parent, stem, ".prj"),
            )
        )
    return tuple(datasets)


def _classify_members(
    infos: list[zipfile.ZipInfo],
    *,
    max_total_uncompressed: int,
) -> tuple[list[zipfile.ZipInfo], list[str]]:
    """Validate every member name and decide write/skip before touching disk.

    Args:
        infos: Central-directory entries.
        max_total_uncompressed: Declared-size budget for the archive.

    Returns:
        ``(members_to_extract, skipped_member_names)``.

    Raises:
        InvalidArchiveError: On unsafe names, symlinks, nested archives,
            executable payloads, oversized inputs or empty mandatory
            components.
    """
    if len(infos) > MAX_ZIP_MEMBERS:
        raise InvalidArchiveError(
            f"ZIP archive contains too many members "
            f"({len(infos)} > {MAX_ZIP_MEMBERS})",
            code="UNSAFE_ARCHIVE",
        )

    total = sum(info.file_size for info in infos)
    if total > max_total_uncompressed:
        raise InvalidArchiveError(
            "ZIP archive uncompressed size exceeds the allowed limit "
            f"({total} > {max_total_uncompressed} bytes)",
            code="UNSAFE_ARCHIVE",
        )

    to_extract: list[zipfile.ZipInfo] = []
    skipped: list[str] = []
    has_shp = False

    for info in infos:
        name = info.filename
        normalized = name.replace("\\", "/")

        if is_unsafe_member_path(name):
            raise InvalidArchiveError(
                f"ZIP archive contains unsafe member path: {name!r} "
                f"(path traversal rejected)",
                code="UNSAFE_ARCHIVE",
            )
        if info.is_dir():
            continue
        if _is_symlink(info):
            raise InvalidArchiveError(
                f"ZIP archive contains a symbolic link member: {name!r} "
                f"(links are not allowed)",
                code="UNSAFE_ARCHIVE",
            )

        path = PurePosixPath(normalized)
        suffix = path.suffix.lower()

        if info.flag_bits & 0x1:
            # Encrypted members cannot be inspected or validated.
            raise InvalidArchiveError(
                f"Encrypted ZIP members are not supported: {name!r}",
                code="UNSAFE_ARCHIVE",
            )
        if suffix in NESTED_ARCHIVE_SUFFIXES:
            raise InvalidArchiveError(
                f"Nested archives are not supported: {name!r}",
                code="UNSAFE_ARCHIVE",
            )
        if suffix in EXECUTABLE_SUFFIXES:
            raise InvalidArchiveError(
                f"Archive contains executable content: {name!r} — only "
                f"shapefile components are accepted",
                code="UNSAFE_ARCHIVE",
            )

        # AppleDouble metadata uses component-looking names (._x.shp);
        # treat the whole __MACOSX tree and ._ files as junk.
        if (
            any(part.lower() in JUNK_DIR_NAMES for part in path.parts)
            or path.name.startswith("._")
        ):
            skipped.append(name)
            continue

        if suffix not in SHAPEFILE_COMPONENT_SUFFIXES:
            skipped.append(name)
            continue

        if suffix in MANDATORY_COMPONENT_SUFFIXES and info.file_size == 0:
            raise InvalidArchiveError(
                f"Shapefile component '{name}' is empty (0 bytes)",
                code="INVALID_SHAPEFILE",
            )
        if suffix == ".shp":
            has_shp = True
        to_extract.append(info)

    if not has_shp:
        raise InvalidArchiveError(
            "ZIP archive does not contain a .shp file — "
            "not a shapefile archive",
            code="INVALID_SHAPEFILE",
        )
    return to_extract, skipped


def extract_shapefile_zip(
    source: bytes | str | Path | IO[bytes],
    destination: str | Path,
    *,
    max_total_uncompressed: int = DEFAULT_MAX_TOTAL_UNCOMPRESSED,
) -> ExtractionResult:
    """Securely extract a shapefile ZIP and identify its datasets.

    Args:
        source: Archive bytes, path, or open binary file object.
        destination: Directory to extract into (created as needed; it
            should be fresh — this module discards it on failure when it
            had to create it).
        max_total_uncompressed: Budget for total declared member size.

    Returns:
        :class:`ExtractionResult` with identified datasets and a report
        of extracted vs. skipped members.

    Raises:
        InvalidArchiveError: Any structural, safety or completeness
            violation — messages are specific and actionable.
    """
    dest = Path(destination)
    created_dest = not dest.exists()
    try:
        return _extract(
            source,
            dest,
            max_total_uncompressed=max_total_uncompressed,
        )
    except Exception:
        if created_dest:
            shutil.rmtree(dest, ignore_errors=True)
        raise


def _extract(
    source: bytes | str | Path | IO[bytes],
    dest: Path,
    *,
    max_total_uncompressed: int,
) -> ExtractionResult:
    """Perform the actual extraction (see :func:`extract_shapefile_zip`)."""
    with _open_zip(source) as archive:
        try:
            infos = archive.infolist()
        except zipfile.BadZipFile as exc:
            raise InvalidArchiveError(f"Malformed ZIP archive: {exc}") from exc

        to_extract, skipped = _classify_members(
            infos, max_total_uncompressed=max_total_uncompressed
        )

        dest.mkdir(parents=True, exist_ok=True)
        extracted: list[str] = []
        for info in to_extract:
            target = _target_path(dest, info.filename.replace("\\", "/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            # "xb": never overwrite, even if a crafted archive maps two
            # members onto one path — the collision surfaces as a
            # domain error rather than a half-written file.
            try:
                with archive.open(info) as src, target.open("xb") as out:
                    shutil.copyfileobj(src, out)
            except FileExistsError as exc:
                raise InvalidArchiveError(
                    f"ZIP archive maps multiple members onto "
                    f"{info.filename!r} (duplicate member paths)",
                    code="UNSAFE_ARCHIVE",
                ) from exc
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                # Corrupt/encrypted/unsupported member data — caught
                # here so CRC and codec failures stay domain errors.
                raise InvalidArchiveError(
                    f"ZIP member {info.filename!r} could not be read: "
                    f"{exc}",
                    code="MALFORMED_ZIP",
                ) from exc
            extracted.append(info.filename)

    datasets = _identify_datasets(dest)
    logger.info(
        "Extracted %d member(s) into %s (%d dataset(s), %d skipped)",
        len(extracted),
        dest,
        len(datasets),
        len(skipped),
    )
    return ExtractionResult(
        destination=dest,
        datasets=datasets,
        extracted=tuple(extracted),
        skipped=tuple(skipped),
    )
