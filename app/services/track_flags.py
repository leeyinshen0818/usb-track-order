"""Read-only hints for the track list: new songs and possible duplicates.

Nothing here touches files or blocks any operation; results only drive a row
highlight, a small marker, and one-line summaries.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path

from pypinyin import lazy_pinyin

from app.models.track import Track
from app.services.filename_numbering import (
    has_canonical_numbering,
    strip_canonical_numbering,
)

# Windows Explorer copy suffixes ("Song - Copy", "Song - Copy (2)", "Song (1)",
# Chinese "Song - 副本"), checked on the stem without numbering.
_COPY_SUFFIX = re.compile(
    r"(?:\s*-\s*(?:copy|副本)(?:\s*\(\d+\))?|\s*\(\d+\))$", re.IGNORECASE
)
_DASHES = re.compile(r"\s*[-‐-―−_]+\s*")
_SPACES = re.compile(r"\s+")


def _stem(filename: str) -> str:
    base = strip_canonical_numbering(filename)
    stem, dot, _extension = base.rpartition(".")
    return stem if dot and stem else base


def has_copy_suffix(filename: str) -> bool:
    return bool(_COPY_SUFFIX.search(_stem(filename)))


def normalized_title(filename: str) -> str:
    """Artist + title key ignoring numbering, extension, copy suffixes, case,
    width, dash/space style, and traditional vs simplified Chinese."""

    stem = unicodedata.normalize("NFKC", _stem(filename))
    while True:
        trimmed = _COPY_SUFFIX.sub("", stem)
        if trimmed == stem:
            break
        stem = trimmed
    stem = _DASHES.sub(" - ", stem)
    stem = _SPACES.sub(" ", stem).strip()
    phonetic = " ".join(lazy_pinyin(stem, errors="default"))
    return _SPACES.sub(" ", phonetic).strip().casefold()


def find_possible_duplicates(tracks: Sequence[Track]) -> dict[Path, str]:
    """Map each flagged track path to a short human-readable reason."""

    groups: dict[str, list[Track]] = defaultdict(list)
    for track in tracks:
        groups[normalized_title(track.original_filename)].append(track)
    flagged: dict[Path, str] = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        for track in group:
            others = ", ".join(
                other.original_filename for other in group if other is not track
            )
            flagged[track.original_path] = f"Same artist/title as: {others}"
    for track in tracks:
        if track.original_path not in flagged and has_copy_suffix(track.original_filename):
            flagged[track.original_path] = "Name looks like a copy"
    return flagged


def name_key(filename: str) -> str:
    return strip_canonical_numbering(filename).casefold()


def find_new_tracks(
    tracks: Iterable[Track], previous_names: set[str] | None
) -> set[Path]:
    """Tracks without app numbering, or whose name (numbering ignored) was not
    present at the previous load of the same folder."""

    new: set[Path] = set()
    for track in tracks:
        if not has_canonical_numbering(track.original_filename):
            new.add(track.original_path)
        elif previous_names is not None and name_key(track.original_filename) not in previous_names:
            new.add(track.original_path)
    return new


class FolderHistory:
    """Remembers the names seen at the last load of each folder (JSON file)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @staticmethod
    def _folder_key(folder: str | Path) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(folder)))

    def _read(self) -> dict[str, list[str]]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        folders = document.get("folders") if isinstance(document, dict) else None
        return folders if isinstance(folders, dict) else {}

    def previous_names(self, folder: str | Path) -> set[str] | None:
        names = self._read().get(self._folder_key(folder))
        if not isinstance(names, list):
            return None
        return {name for name in names if isinstance(name, str)}

    def record(self, folder: str | Path, tracks: Iterable[Track]) -> None:
        folders = self._read()
        folders[self._folder_key(folder)] = sorted(
            {name_key(track.original_filename) for track in tracks}
        )
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps({"version": 1, "folders": folders}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
