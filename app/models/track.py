"""Track data kept in memory while arranging a folder."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Track:
    """A snapshot of an audio file's current disk identity.

    Fields retain their Phase 1 names for compatibility. A new immutable Track
    is created after every successful Phase 2 rename.
    """

    original_path: Path
    original_filename: str
    file_identity: tuple[int, int, int, int] | None = None

    @classmethod
    def from_path(cls, path: Path) -> "Track":
        try:
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        except OSError:
            identity = None
        return cls(
            original_path=path,
            original_filename=path.name,
            file_identity=identity,
        )
