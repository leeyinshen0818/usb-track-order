"""Read-only discovery of supported audio files in a single folder."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TypeAlias

from app.models.track import Track

SUPPORTED_AUDIO_EXTENSIONS = frozenset({".mp3", ".flac", ".wav", ".m4a", ".aac"})
NaturalPart: TypeAlias = tuple[int, int | str]


class FileScanError(RuntimeError):
    """A folder could not be safely scanned."""


def is_supported_audio(path: str | Path) -> bool:
    """Return whether a path has a supported extension, ignoring case."""

    return Path(path).suffix.casefold() in SUPPORTED_AUDIO_EXTENSIONS


def natural_sort_key(filename: str) -> tuple[NaturalPart, ...]:
    """Build a case-insensitive key that compares digit runs numerically."""

    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", filename)
        if part
    )


def scan_folder(folder: str | Path) -> list[Track]:
    """Return supported files directly inside *folder* in natural order.

    Entries that disappear during the scan are ignored. No file or directory is
    opened for writing, and subdirectories are never traversed.
    """

    directory = Path(folder)
    if not directory.exists():
        raise FileScanError("The selected folder no longer exists.")
    if not directory.is_dir():
        raise FileScanError("The selected path is not a folder.")

    try:
        entries = list(directory.iterdir())
    except PermissionError as exc:
        raise FileScanError("Permission was denied while reading this folder.") from exc
    except OSError as exc:
        raise FileScanError(f"This folder could not be read: {exc}") from exc

    tracks: list[Track] = []
    for entry in entries:
        try:
            if entry.is_file() and is_supported_audio(entry):
                tracks.append(Track.from_path(entry))
        except OSError:
            # A disappearing or temporarily inaccessible individual entry should
            # not prevent the rest of the folder from loading.
            continue

    return sorted(
        tracks,
        key=lambda track: (
            natural_sort_key(track.original_filename),
            track.original_filename.casefold(),
            track.original_filename,
        ),
    )
