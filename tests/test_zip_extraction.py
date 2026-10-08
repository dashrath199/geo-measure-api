"""Unit tests for the secure shapefile ZIP extraction service.

Covers valid archives (identification, case-insensitivity, nesting,
sidecars, source forms) and malicious ones (Zip Slip variants, absolute
paths, symlinks, nested archives, executables, bombs, missing/empty
components).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.exceptions import InvalidArchiveError
from app.geo.zip_extraction import (
    MAX_ZIP_MEMBERS,
    extract_shapefile_zip,
)


# ----------------------------------------------------------------- builders


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    """Build an archive from a name→content mapping.

    Args:
        entries: Member names and their content.

    Returns:
        Raw archive bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _valid_entries(*, with_prj: bool = True) -> dict[str, bytes]:
    """Return a complete, minimal shapefile component set.

    Args:
        with_prj: Include a ``.prj`` sidecar.

    Returns:
        Mapping of member names to bytes.
    """
    entries = {
        "sample.shp": b"SHP-DATA",
        "sample.shx": b"SHX-DATA",
        "sample.dbf": b"DBF-DATA",
    }
    if with_prj:
        entries["sample.prj"] = b"GEOGCS[WGS 84]"
    return entries


# ------------------------------------------------------------ happy paths


def test_extracts_components_and_identifies(tmp_path: Path) -> None:
    """Valid archive extracts all files and identifies the dataset."""
    dest = tmp_path / "out"

    result = extract_shapefile_zip(_zip_bytes(_valid_entries()), dest)

    assert result.destination == dest
    assert result.skipped == ()
    assert set(result.extracted) == {
        "sample.shp",
        "sample.shx",
        "sample.dbf",
        "sample.prj",
    }
    dataset = result.primary
    assert dataset.shp == dest / "sample.shp"
    assert dataset.shx.exists()
    assert dataset.dbf.exists()
    assert dataset.prj == dest / "sample.prj"
    assert dataset.has_prj
    # Round-trip: extracted bytes equal archived bytes.
    assert dataset.shp.read_bytes() == b"SHP-DATA"


def test_prj_is_optional(tmp_path: Path) -> None:
    """Datasets without ``.prj`` extract fine; prj is None."""
    result = extract_shapefile_zip(
        _zip_bytes(_valid_entries(with_prj=False)), tmp_path / "out"
    )

    assert result.primary.prj is None
    assert not result.primary.has_prj


def test_components_matched_case_insensitively(tmp_path: Path) -> None:
    """Uppercase extensions are identified as one dataset."""
    entries = {
        "ROADS.SHP": b"shp",
        "roads.shx": b"shx",
        "ROADS.DBF": b"dbf",
    }

    result = extract_shapefile_zip(_zip_bytes(entries), tmp_path / "out")

    dataset = result.primary
    assert dataset.shp.name == "ROADS.SHP"
    assert dataset.shx.name == "roads.shx"
    assert dataset.dbf.name == "ROADS.DBF"


def test_nested_member_directories_preserved(tmp_path: Path) -> None:
    """Members in subfolders land inside the destination only."""
    entries = {f"data/{name}": data for name, data in _valid_entries().items()}

    result = extract_shapefile_zip(_zip_bytes(entries), tmp_path / "out")

    assert result.primary.shp == tmp_path / "out" / "data" / "sample.shp"
    assert result.primary.shp.resolve().is_relative_to(
        (tmp_path / "out").resolve()
    )


def test_junk_members_skipped_not_written(tmp_path: Path) -> None:
    """Documentation/OS junk is reported as skipped, never written."""
    entries = _valid_entries()
    entries["README.txt"] = b"hello"
    entries[".DS_Store"] = b"junk"
    entries["__MACOSX/._sample.shp"] = b"appledouble"
    dest = tmp_path / "out"

    result = extract_shapefile_zip(_zip_bytes(entries), dest)

    assert set(result.skipped) == {
        "README.txt",
        ".DS_Store",
        "__MACOSX/._sample.shp",
    }
    assert not (dest / "README.txt").exists()
    assert not (dest / ".DS_Store").exists()
    # AppleDouble twin must not masquerade as a component.
    assert not (dest / "__MACOSX" / "._sample.shp").exists()


def test_source_as_path_bytes_and_stream(tmp_path: Path) -> None:
    """Bytes, filesystem path and file objects are all accepted."""
    data = _zip_bytes(_valid_entries())
    archive_path = tmp_path / "src.zip"
    archive_path.write_bytes(data)

    from_bytes = extract_shapefile_zip(data, tmp_path / "d1")
    from_path = extract_shapefile_zip(archive_path, tmp_path / "d2")
    from_stream = extract_shapefile_zip(
        io.BytesIO(data), tmp_path / "d3"
    )

    for result in (from_bytes, from_path, from_stream):
        assert result.primary.shp.read_bytes() == b"SHP-DATA"


def test_multiple_datasets_identified(tmp_path: Path) -> None:
    """Each complete dataset inside the archive is returned."""
    entries = dict(_valid_entries())
    entries.update(
        {
            "other.shp": b"shp2",
            "other.shx": b"shx2",
            "other.dbf": b"dbf2",
        }
    )

    result = extract_shapefile_zip(_zip_bytes(entries), tmp_path / "out")

    assert len(result.datasets) == 2
    stems = {d.shp.stem for d in result.datasets}
    assert stems == {"sample", "other"}


# ------------------------------------------------------------ malicious


def test_parent_traversal_rejected(tmp_path: Path) -> None:
    """``..`` members are rejected before anything is written."""
    entries = _zip_bytes(_valid_entries())
    archive = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(entries)) as src, zipfile.ZipFile(
        archive, "w"
    ) as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name))
        dst.writestr("../evil.shp", b"evil")
    parent = tmp_path / "parent"
    parent.mkdir()
    dest = parent / "out"

    with pytest.raises(InvalidArchiveError, match="traversal"):
        extract_shapefile_zip(archive.getvalue(), dest)

    # Nothing escaped and the failed directory was cleaned up.
    assert not (parent / "evil.shp").exists()
    assert not dest.exists()


def test_absolute_path_rejected(tmp_path: Path) -> None:
    """POSIX-absolute member names are rejected."""
    entries = _valid_entries()
    entries["/etc/evil.shp"] = b"evil"

    with pytest.raises(InvalidArchiveError, match="unsafe"):
        extract_shapefile_zip(_zip_bytes(entries), tmp_path / "out")


def test_windows_drive_path_rejected(tmp_path: Path) -> None:
    """Drive-letter member names are rejected."""
    entries = _valid_entries()
    entries["C:\\evil.shp"] = b"evil"

    with pytest.raises(InvalidArchiveError, match="unsafe"):
        extract_shapefile_zip(_zip_bytes(entries), tmp_path / "out")


def test_backslash_traversal_rejected(tmp_path: Path) -> None:
    """Backslash-encoded ``..`` (Windows Zip Slip) is rejected."""
    entries = _valid_entries()
    entries["..\\evil.shp"] = b"evil"

    with pytest.raises(InvalidArchiveError, match="traversal"):
        extract_shapefile_zip(_zip_bytes(entries), tmp_path / "out")


def test_nested_archive_rejected(tmp_path: Path) -> None:
    """Recursively unpacking archives is explicitly unsupported."""
    entries = _valid_entries()
    entries["backup.zip"] = b"PK\x03\x04nested"

    with pytest.raises(InvalidArchiveError, match="Nested archives"):
        extract_shapefile_zip(_zip_bytes(entries), tmp_path / "out")


def test_executable_member_rejected(tmp_path: Path) -> None:
    """Executable payloads fail loudly and never reach disk."""
    entries = _valid_entries()
    entries["payload.exe"] = b"MZ..."

    with pytest.raises(InvalidArchiveError, match="executable"):
        extract_shapefile_zip(_zip_bytes(entries), tmp_path / "out")

    assert not (tmp_path / "out").exists()


def test_symlink_member_rejected(tmp_path: Path) -> None:
    """Symlink members (zip-symlink attack) are rejected."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in _valid_entries().items():
            archive.writestr(name, data)
        info = zipfile.ZipInfo("link.shp")
        info.external_attr = 0o120777 << 16  # S_IFLNK | 0777
        archive.writestr(info, "/etc/passwd")

    with pytest.raises(InvalidArchiveError, match="symbolic link"):
        extract_shapefile_zip(buffer.getvalue(), tmp_path / "out")


def test_missing_mandatory_component_reported(tmp_path: Path) -> None:
    """An archive lacking ``.shx``/``.dbf`` fails with a specific error."""
    entries = {"sample.shp": b"shp", "sample.prj": b"prj"}

    with pytest.raises(InvalidArchiveError, match="missing mandatory"):
        extract_shapefile_zip(_zip_bytes(entries), tmp_path / "out")


def test_no_shapefile_rejected(tmp_path: Path) -> None:
    """Archives without any ``.shp`` are not shapefile archives."""
    entries = {"README.txt": b"hi", "data.csv": b"a,b"}

    with pytest.raises(InvalidArchiveError, match=r"\.shp"):
        extract_shapefile_zip(_zip_bytes(entries), tmp_path / "out")


def test_empty_mandatory_component_rejected(tmp_path: Path) -> None:
    """Zero-byte mandatory components are rejected as malformed."""
    entries = _valid_entries()
    entries["sample.shx"] = b""

    with pytest.raises(InvalidArchiveError, match="empty"):
        extract_shapefile_zip(_zip_bytes(entries), tmp_path / "out")


def test_uncompressed_size_limit_enforced(tmp_path: Path) -> None:
    """Declared-size budget is checked before any extraction."""
    entries = _valid_entries()

    with pytest.raises(InvalidArchiveError, match="uncompressed size"):
        extract_shapefile_zip(
            _zip_bytes(entries),
            tmp_path / "out",
            max_total_uncompressed=4,
        )
    assert not (tmp_path / "out").exists()


def test_member_count_limit_enforced(tmp_path: Path) -> None:
    """Archives beyond the member cap are rejected."""
    entries = {f"junk/file_{i}.txt": b"x" for i in range(MAX_ZIP_MEMBERS + 1)}

    with pytest.raises(InvalidArchiveError, match="too many members"):
        extract_shapefile_zip(_zip_bytes(entries), tmp_path / "out")


def test_corrupt_archive_rejected(tmp_path: Path) -> None:
    """Garbage with a ZIP magic prefix fails cleanly."""
    payload = b"PK\x03\x04" + b"garbage" * 30

    with pytest.raises(InvalidArchiveError, match="[Mm]alformed"):
        extract_shapefile_zip(payload, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_failed_extraction_removes_created_directory(tmp_path: Path) -> None:
    """A destination this module created is removed on failure."""
    dest = tmp_path / "out"

    with pytest.raises(InvalidArchiveError):
        extract_shapefile_zip(_zip_bytes({"README.txt": b"x"}), dest)

    assert not dest.exists()


def test_preexisting_destination_survives_failure(tmp_path: Path) -> None:
    """Caller-owned directories are not deleted on failure."""
    dest = tmp_path / "out"
    dest.mkdir()

    with pytest.raises(InvalidArchiveError):
        extract_shapefile_zip(_zip_bytes({"README.txt": b"x"}), dest)

    assert dest.exists()


def test_service_module_has_no_fastapi_dependency() -> None:
    """The extraction service must not know anything about FastAPI.

    Checks actual imports via the AST — the module may *mention* the
    framework in prose, but must never import it.
    """
    import ast

    module_source = (
        Path(__file__).resolve().parents[1]
        / "app" / "geo" / "zip_extraction.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(module_source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(
                alias.name.split(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "fastapi" not in imported
    assert "starlette" not in imported
    # Service layer stays framework-free beyond the web stack too.
    assert not {"app.api", "app.main"} & imported
