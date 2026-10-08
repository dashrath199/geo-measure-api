"""Phase 16 tests: upload & processing security protections.

Covers the new hardening layers:

- request-body size gate (declared Content-Length *and* chunked bodies)
  rejecting oversized uploads before the multipart parser spools them;
- malicious-archive details: NUL-byte member names, encrypted members,
  duplicate member paths (Zip Slip-adjacent collisions);
- XML entity / DTD declarations in KML (XML-bomb and XXE class),
  scanned across the whole document at upload and processing time;
- the per-dataset feature cap that bounds processing work and
  measurements-response size.

Existing suites keep covering: extension whitelist, magic-byte sniffing,
traversal/`..`/backslash member names, symlink/nested/executable
members, declared uncompressed-size and member-count limits, filename
sanitization, size limits on file content, and path redaction.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.base import get_session_factory
from app.db.models import FileRecord
from app.exceptions import InvalidArchiveError, InvalidFileError
from app.geo.processors import KMLProcessor
from app.geo.validation import SNIFF_BYTES, is_unsafe_member_path
from app.geo.zip_extraction import extract_shapefile_zip
from app.main import _MULTIPART_OVERHEAD, create_app
from sqlalchemy import select

KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document><Placemark><Point><coordinates>0,0</coordinates></Point></Placemark></Document>
</kml>
"""

KML_TWO_FEATURES = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark><Point><coordinates>0,0</coordinates></Point></Placemark>
    <Placemark><Point><coordinates>1,1</coordinates></Point></Placemark>
  </Document>
</kml>
"""


# ----------------------------------------------------------------- helpers

def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Build isolated settings for a security test.

    Args:
        tmp_path: Per-test temp directory.
        **overrides: Field overrides (e.g. ``max_features``).

    Returns:
        The settings instance.
    """
    return Settings(
        _env_file=None,
        environment="development",
        debug=False,
        log_level="WARNING",
        max_upload_mb=1,
        storage_dir=str(tmp_path / "uploads"),
        **overrides,
    )


def _all_records() -> list[FileRecord]:
    """Fetch every file record via a fresh session."""
    factory = get_session_factory()
    with factory() as session:
        return list(session.scalars(select(FileRecord)))


def _post(
    client: TestClient, filename: str, content: bytes
) -> Any:
    """Upload bytes under a filename via ``POST /api/files/``.

    Args:
        client: Test client.
        filename: Name to send.
        content: File bytes.

    Returns:
        The HTTP response.
    """
    return client.post(
        "/api/files/",
        files={"file": (filename, content, "application/octet-stream")},
    )


def _assert_too_large(response: Any, settings: Settings) -> None:
    """Assert a 413 ``FILE_TOO_LARGE`` envelope and nothing persisted."""
    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "FILE_TOO_LARGE"
    assert str(settings.max_upload_mb) in body["error"]["message"]
    assert _all_records() == []
    root = Path(settings.storage_dir)
    stored = list(root.iterdir()) if root.exists() else []
    assert stored == []


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    """Build an in-memory ZIP from a name→content map.

    Args:
        members: Member names and contents (duplicates allowed via
            :func:`_zip_bytes_dupes`).

    Returns:
        Archive bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _set_encrypted_flag(data: bytes) -> bytes:
    """Patch every central-directory header's encrypted bit.

    Python's zipfile cannot *write* encryption, so a valid archive is
    rewritten in memory: general-purpose flag bit 0 (offset 8 of each
    ``PK\\x01\\x02`` record) marks the member as encrypted.

    Args:
        data: Bytes of a valid ZIP archive.

    Returns:
        Archive bytes with the encrypted flag set on all members.
    """
    patched = bytearray(data)
    index = 0
    while True:
        index = patched.find(b"PK\x01\x02", index)
        if index == -1:
            break
        patched[index + 8] |= 0x01  # general purpose bit 0 = encrypted
        index += 4
    assert patched != data, "no central-directory records found"
    return bytes(patched)


# ------------------------------------------------------- body size gate


def test_oversized_body_rejected_by_content_length(tmp_path: Path) -> None:
    """A declared Content-Length above the threshold is refused with
    413 before any body byte is read."""
    settings = _settings(tmp_path)
    limit = settings.max_upload_mb * 1024 * 1024
    payload = KML + b"a" * (limit + _MULTIPART_OVERHEAD + 4096)
    app = create_app(settings=settings)

    with TestClient(app) as client:
        response = _post(client, "huge.kml", payload)

    _assert_too_large(response, settings)


def test_chunked_body_aborts_mid_transfer(tmp_path: Path) -> None:
    """A body with no (or lying) Content-Length is counted as it
    arrives and aborted at the threshold — the multipart parser never
    gets to spool an unbounded body to disk."""
    settings = _settings(tmp_path)
    limit = settings.max_upload_mb * 1024 * 1024
    threshold = limit + _MULTIPART_OVERHEAD
    app = create_app(settings=settings)

    boundary = "p16chunkboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="big.kml"\r\n'
        "Content-Type: application/octet-stream\r\n"
        "\r\n"
    ).encode()
    body += b"<kml>" + b"a" * (threshold + 8192)
    body += f"\r\n--{boundary}--\r\n".encode()

    chunk = 64 * 1024
    frames = [
        {
            "type": "http.request",
            "body": body[offset : offset + chunk],
            "more_body": offset + chunk < len(body),
        }
        for offset in range(0, len(body), chunk)
    ]
    index = 0

    async def receive() -> dict:
        nonlocal index
        if index < len(frames):
            message = frames[index]
            index += 1
            return message
        return {"type": "http.disconnect"}

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/files/",
        "raw_path": b"/api/files/",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (
                b"content-type",
                f"multipart/form-data; boundary={boundary}".encode(),
            ),
            # Deliberately no content-length: chunked-style delivery.
            (b"transfer-encoding", b"chunked"),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }

    asyncio.run(app(scope, receive, send))

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413
    raw = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    error = json.loads(raw)["error"]
    assert error["code"] == "FILE_TOO_LARGE"
    assert _all_records() == []


# -------------------------------------------------- malicious archives


def test_nul_byte_member_name_rejected(tmp_path: Path) -> None:
    """Member names embedding NUL bytes are parse-confusion payloads.

    CPython's zipfile truncates names at NUL before this app ever sees
    them, so the crafted member arrives as a bare ``sample.shp`` — the
    upload is still rejected (missing components) and nothing is
    stored; the validator rule itself is asserted directly below.
    """
    settings = _settings(tmp_path)
    payload = _zip_bytes({"sample.shp\x00.txt": b"x"})
    app = create_app(settings=settings)

    with TestClient(app) as client:
        response = _post(client, "nul.zip", payload)

    assert response.status_code == 400
    assert _all_records() == []
    root = Path(settings.storage_dir)
    assert not root.exists() or list(root.iterdir()) == []


def test_nul_byte_member_name_rule() -> None:
    """The shared path rule treats embedded NUL bytes as unsafe."""
    assert is_unsafe_member_path("sample.shp\x00.txt") is True
    assert is_unsafe_member_path("") is True
    assert is_unsafe_member_path("sample.shp") is False


def test_encrypted_member_rejected(tmp_path: Path) -> None:
    """Encrypted members cannot be inspected or validated → 400."""
    settings = _settings(tmp_path)
    payload = _set_encrypted_flag(
        _zip_bytes(
            {
                "sample.shp": b"junk",
                "sample.shx": b"junk",
                "sample.dbf": b"junk",
            }
        )
    )
    app = create_app(settings=settings)

    with TestClient(app) as client:
        response = _post(client, "secret.zip", payload)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNSAFE_ARCHIVE"
    assert "Encrypted" in response.json()["error"]["message"]
    assert _all_records() == []


def test_duplicate_member_paths_rejected(tmp_path: Path) -> None:
    """Two members mapping to one path (case folds, exact dupes) are
    refused during extraction instead of overwriting each other."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("sample.shp", b"first")
        archive.writestr("sample.shx", b"junk")
        archive.writestr("sample.dbf", b"junk")
        archive.writestr("sample.shp", b"duplicate")

    with pytest.raises(InvalidArchiveError) as excinfo:
        extract_shapefile_zip(buffer.getvalue(), tmp_path / "out")

    assert excinfo.value.code == "UNSAFE_ARCHIVE"
    assert "duplicate" in str(excinfo.value)


# ------------------------------------------------------ XML entity attack


def test_kml_with_entity_declaration_rejected(tmp_path: Path) -> None:
    """DOCTYPE/ENTITY declarations (XML bomb / XXE class) are refused
    at upload with 400 ``UNSAFE_CONTENT``."""
    settings = _settings(tmp_path)
    payload = (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE kml [<!ENTITY a "' + b"a" * 100 + b'">]>\n'
        b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        b"<Placemark><Point><coordinates>0,0</coordinates></Point>"
        b"</Placemark></Document></kml>"
    )
    app = create_app(settings=settings)

    with TestClient(app) as client:
        response = _post(client, "bomb.kml", payload)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNSAFE_CONTENT"
    assert _all_records() == []


def test_processor_scans_whole_document_for_entities(
    tmp_path: Path,
) -> None:
    """The processing layer rescans stored bytes, so hiding the DTD
    past the 8 KB sniff window does not slip through."""
    # "<kml" appears inside an early comment (sniff passes) while the
    # DOCTYPE sits beyond SNIFF_BYTES.
    pad = b"<!-- " + b"<kml marker " + b"x" * SNIFF_BYTES + b" -->\n"
    document = (
        b'<?xml version="1.0"?>\n'
        + pad
        + b'<!DOCTYPE kml [<!ENTITY a "boom">]>\n'
        + b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        + b"<Placemark><Point><coordinates>0,0</coordinates></Point>"
        + b"</Placemark></Document></kml>"
    )
    path = tmp_path / "hidden.kml"
    path.write_bytes(document)

    with pytest.raises(InvalidFileError) as excinfo:
        KMLProcessor().process(path)

    assert excinfo.value.code == "UNSAFE_CONTENT"


# ------------------------------------------------------------ feature cap


def test_feature_cap_fails_processing(tmp_path: Path) -> None:
    """Datasets beyond ``max_features`` fail processing with an
    explicit, safe message instead of grinding through unbounded work."""
    settings = _settings(tmp_path, max_features=1)
    app = create_app(settings=settings)

    with TestClient(app) as client:
        response = _post(client, "dense.kml", KML_TWO_FEATURES)

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "FAILED"

        info = client.get(f"/api/files/{body['id']}/")
        assert info.status_code == 200
        record = info.json()
        assert record["status"] == "FAILED"
        assert "too many features" in record["error_message"]
        assert "2 > 1" in record["error_message"]

    assert _all_records()[0].status == "FAILED"
