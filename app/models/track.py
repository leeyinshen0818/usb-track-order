"""Track data kept in memory while arranging a folder."""

from __future__ import annotations

import os
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
    extension: str = ""
    size_bytes: int | None = None
    modified_time: float | None = None
    created_time: float | None = None

    @classmethod
    def from_path(cls, path: Path) -> "Track":
        try:
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            size_bytes = stat.st_size
            modified_time = stat.st_mtime
            created_time = stat.st_ctime if os.name == "nt" else None
        except OSError:
            identity = None
            size_bytes = None
            modified_time = None
            created_time = None
        return cls(
            original_path=path,
            original_filename=path.name,
            file_identity=identity,
            extension=path.suffix,
            size_bytes=size_bytes,
            modified_time=modified_time,
            created_time=created_time,
        )

    @property
    def file_type(self) -> str:
        return self.extension.removeprefix(".").upper()
