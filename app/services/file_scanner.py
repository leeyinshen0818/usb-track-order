"""Read-only discovery of supported audio files in a single folder."""

from __future__ import annotations

import fnmatch
import logging
import re
from pathlib import Path
from typing import TypeAlias

from app.models.track import Track
from app.services.safe_renamer import RENAME_TEMP_PREFIX

SUPPORTED_AUDIO_EXTENSIONS = frozenset({".mp3", ".flac", ".wav", ".m4a", ".aac"})
NaturalPart: TypeAlias = tuple[int, int | str]

# Disposable leftovers from interrupted v1 loudness runs and USB exports.
# Rename temp files (".__usb_track_order_tmp_*") are NOT disposable: they hold
# real songs when a rename was interrupted, so they are listed, never deleted.
TEMP_FILE_PATTERNS = (".*loudness-*", "*.part")

logger = logging.getLogger("usb_track_order.scanner")


class FileScanError(RuntimeError):
    """A folder could not be safely scanned."""


def is_supported_audio(path: str | Path) -> bool:
    """Return whether a path has a supported extension, ignoring case."""

    return Path(path).suffix.casefold() in SUPPORTED_AUDIO_EXTENSIONS


def is_temp_file(name: str) -> bool:
    """Return whether *name* matches one of the app's temporary-file patterns."""

    folded = name.casefold()
    return any(fnmatch.fnmatchcase(folded, pattern) for pattern in TEMP_FILE_PATTERNS)


def is_interrupted_rename_file(name: str) -> bool:
    """A file left behind by an interrupted two-stage rename (holds a real song)."""

    return name.casefold().startswith(RENAME_TEMP_PREFIX)


def is_ignored_file(name: str) -> bool:
    """Dot-files and disposable temp files are not listed as tracks.

    Interrupted-rename files are the exception: they stay visible so no song
    silently disappears from the list.
    """

    if is_interrupted_rename_file(name):
        return False
    return name.startswith(".") or is_temp_file(name)


def count_interrupted_renames(tracks: list[Track]) -> int:
    return sum(1 for track in tracks if is_interrupted_rename_file(track.original_filename))


def remove_leftover_temp_files(folder: str | Path) -> list[Path]:
    """Delete regular files directly in *folder* that match TEMP_FILE_PATTERNS.

    Returns the paths that were removed. Failures are logged and skipped.
    """

    removed: list[Path] = []
    try:
        entries = list(Path(folder).iterdir())
    except OSError:
        return removed
    for entry in entries:
        if not is_temp_file(entry.name):
            continue
        try:
            if not entry.is_file() or entry.is_symlink():
                continue
            entry.unlink()
        except OSError as exc:
            logger.warning("Could not remove leftover temp file %s: %s", entry, exc)
            continue
        logger.info("Removed leftover temp file %s", entry)
        removed.append(entry)
    return removed


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
            if is_ignored_file(entry.name):
                continue
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
