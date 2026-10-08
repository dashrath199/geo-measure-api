"""Local filesystem storage for uploaded bytes.

Safety properties:

- Internal names are server-generated UUIDs plus a whitelisted extension —
  client-supplied names never reach the filesystem, eliminating path
  traversal through the filename.
- Files are created exclusively (``xb``), so an existing file can never
  be overwritten.
- The root directory is created on demand and everything is written
  strictly inside it.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from app.exceptions import StorageError

logger = logging.getLogger(__name__)

# Retries if the (astronomically unlikely) UUID collides with a file.
_MAX_ATTEMPTS = 3


class LocalStore:
    """Stores blobs in a single configured root directory.

    Args:
        root: Directory that holds stored uploads.
    """

    def __init__(self, root: str | Path) -> None:
        """Bind the store to its root directory.

        Args:
            root: Directory that holds stored uploads.
        """
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """Return the storage root directory."""
        return self._root

    def save(self, data: bytes, suffix: str) -> Path:
        """Persist bytes under a fresh UUID-based name.

        Args:
            data: Raw file content.
            suffix: Whitelisted extension including the dot (``.zip``).

        Returns:
            The path the data was written to.

        Raises:
            StorageError: If the storage root cannot be created or the
                bytes cannot be written (disk full, permissions, root
                occupied by a non-directory, exhausted name retries).
        """
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(
                f"Could not create storage directory: {exc}"
            ) from exc
        for _ in range(_MAX_ATTEMPTS):
            path = self._root / f"{uuid.uuid4().hex}{suffix}"
            try:
                with path.open("xb") as handle:
                    handle.write(data)
            except FileExistsError:
                continue
            except OSError as exc:
                # Best-effort cleanup: a partial write must not leave
                # an orphaned file behind a failed upload.
                try:
                    path.unlink()
                except OSError:
                    pass
                raise StorageError(
                    f"Could not write stored file '{path.name}': {exc}"
                ) from exc
            logger.info("Stored upload as %s (%d bytes)", path.name, len(data))
            return path
        raise StorageError("Could not allocate a unique storage name")

    def delete(self, path: Path) -> None:
        """Remove a stored file, ignoring absence.

        Cleanup must never mask the error that triggered it, so any
        other filesystem failure is logged and swallowed.

        Args:
            path: Path returned by :meth:`save`.
        """
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("Could not remove stored upload %s: %s", path, exc)
            return
        logger.info("Removed stored upload %s", path)
