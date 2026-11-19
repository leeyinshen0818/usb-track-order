"""Read-only hints for the track list: new songs and possible duplicates.

Nothing here modifies files or blocks any operation; results drive row tints,
a small marker, one-line summaries, and the duplicate review dialog.
"""

from __future__ import annotations

import json
import math
import os
import re
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pypinyin import Style, lazy_pinyin

from app.models.track import Track
from app.services.filename_numbering import (
    has_canonical_numbering,
    strip_canonical_numbering,
)
from app.services.loudness import fingerprint_file

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
    width, dash/space style, and traditional vs simplified Chinese.

    Pinyin keeps tone numbers, so 周杰倫/周杰伦 still match but homophones
    with different tones (想你 xiang3 / 像你 xiang4) do not.
    """

    stem = unicodedata.normalize("NFKC", _stem(filename))
    while True:
        trimmed = _COPY_SUFFIX.sub("", stem)
        if trimmed == stem:
            break
        stem = trimmed
    stem = _DASHES.sub(" - ", stem)
    stem = _SPACES.sub(" ", stem).strip()
    phonetic = " ".join(lazy_pinyin(stem, style=Style.TONE3, errors="default"))
    return _SPACES.sub(" ", phonetic).strip().casefold()


class DuplicateTier(Enum):
    LIKELY = "likely duplicate"
    SIMILAR = "similar name"


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    tier: DuplicateTier
    tracks: tuple[Track, ...]  # list order
    preselected: frozenset[Path]  # extra copies suggested for removal


def _base_name_key(filename: str) -> tuple[str, str]:
    """(stem without numbering/copy suffixes, extension), case-insensitive."""

    base = strip_canonical_numbering(unicodedata.normalize("NFKC", filename))
    stem, dot, extension = base.rpartition(".")
    if not (dot and stem):
        stem, extension = base, ""
    while True:
        trimmed = _COPY_SUFFIX.sub("", stem)
        if trimmed == stem:
            break
        stem = trimmed
    return _SPACES.sub(" ", stem).strip().casefold(), extension.casefold()


def _size_of(track: Track) -> int | None:
    if track.size_bytes is not None:
        return track.size_bytes
    try:
        return track.original_path.stat().st_size
    except OSError:
        return None


def find_duplicate_groups(
    tracks: Sequence[Track],
    fingerprint: Callable[[Path], str] = fingerprint_file,
) -> list[DuplicateGroup]:
    """Two tiers of duplicates; every track appears in at most one group.

    Likely: identical content (size + fingerprint), or a copy-suffixed name
    ("Song - Copy", "Song (1)") whose base name exists with the same extension.
    The kept track is the unsuffixed, earliest-modified one; the rest are
    preselected. Similar name: same artist/title key but not a likely
    duplicate; nothing is preselected. Only same-size files are hashed.
    """

    count = len(tracks)
    parent = list(range(count))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        parent[find(first)] = find(second)

    linked: set[int] = set()

    by_size: dict[int, list[int]] = defaultdict(list)
    for index, track in enumerate(tracks):
        size = _size_of(track)
        if size is not None:
            by_size[size].append(index)
    for same_size in by_size.values():
        if len(same_size) < 2:
            continue
        by_content: dict[str, list[int]] = defaultdict(list)
        for index in same_size:
            try:
                by_content[fingerprint(tracks[index].original_path)].append(index)
            except OSError:
                continue
        for same in by_content.values():
            for index in same[1:]:
                union(same[0], index)
                linked.update((same[0], index))

    suffixed = [has_copy_suffix(track.original_filename) for track in tracks]
    originals: dict[tuple[str, str], int] = {}
    for index, track in enumerate(tracks):
        if not suffixed[index]:
            originals.setdefault(_base_name_key(track.original_filename), index)
    for index, track in enumerate(tracks):
        if suffixed[index]:
            original = originals.get(_base_name_key(track.original_filename))
            if original is not None:
                union(original, index)
                linked.update((original, index))

    ordered: list[tuple[int, DuplicateGroup]] = []
    components: dict[int, list[int]] = defaultdict(list)
    for index in sorted(linked):
        components[find(index)].append(index)
    for members in components.values():
        keep = min(
            members,
            key=lambda i: (
                suffixed[i],
                tracks[i].modified_time if tracks[i].modified_time is not None else math.inf,
                i,
            ),
        )
        ordered.append((members[0],
            DuplicateGroup(
                DuplicateTier.LIKELY,
                tuple(tracks[i] for i in members),
                frozenset(tracks[i].original_path for i in members if i != keep),
            ),
        ))

    by_title: dict[str, list[int]] = defaultdict(list)
    for index, track in enumerate(tracks):
        if index not in linked:
            by_title[normalized_title(track.original_filename)].append(index)
    for members in by_title.values():
        if len(members) > 1:
            ordered.append((members[0],
                DuplicateGroup(
                    DuplicateTier.SIMILAR,
                    tuple(tracks[i] for i in members),
                    frozenset(),
                ),
            ))
    ordered.sort(key=lambda item: item[0])
    return [group for _first, group in ordered]


def find_possible_duplicates(
    tracks: Sequence[Track],
    groups: Sequence[DuplicateGroup] | None = None,
) -> dict[Path, str]:
    """Map each flagged track path to a short human-readable reason."""

    if groups is None:
        groups = find_duplicate_groups(tracks)
    flagged: dict[Path, str] = {}
    for group in groups:
        label = (
            "Likely duplicate of" if group.tier == DuplicateTier.LIKELY else "Similar name to"
        )
        for track in group.tracks:
            others = ", ".join(
                other.original_filename for other in group.tracks if other is not track
            )
            flagged[track.original_path] = f"{label}: {others}"
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
