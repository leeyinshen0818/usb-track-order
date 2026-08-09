"""Stable, deterministic in-memory sorting for the visible track list."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from pypinyin import lazy_pinyin

from app.models.track import Track
from app.services.file_scanner import natural_sort_key
from app.services.filename_numbering import strip_canonical_numbering


class SortField(Enum):
    FILENAME = "filename"
    MODIFIED = "modified"
    SIZE = "size"
    TYPE = "type"


class SortDirection(Enum):
    ASCENDING = "ascending"
    DESCENDING = "descending"


_DEFAULT_DIRECTIONS = {
    SortField.FILENAME: SortDirection.ASCENDING,
    SortField.MODIFIED: SortDirection.DESCENDING,
    SortField.SIZE: SortDirection.DESCENDING,
    SortField.TYPE: SortDirection.ASCENDING,
}


@dataclass(frozen=True, slots=True)
class SortState:
    field: SortField | None = None
    direction: SortDirection | None = None

    @classmethod
    def custom(cls) -> "SortState":
        return cls()

    @property
    def is_custom(self) -> bool:
        return self.field is None

    @property
    def label(self) -> str:
        if self.field is None or self.direction is None:
            return "Custom Order"
        if self.field == SortField.FILENAME:
            return "Filename A → Z" if self.direction == SortDirection.ASCENDING else "Filename Z → A"
        if self.field == SortField.MODIFIED:
            return (
                "Date Modified — Oldest First"
                if self.direction == SortDirection.ASCENDING
                else "Date Modified — Newest First"
            )
        if self.field == SortField.SIZE:
            return "Size — Smallest First" if self.direction == SortDirection.ASCENDING else "Size — Largest First"
        return "Type A → Z" if self.direction == SortDirection.ASCENDING else "Type Z → A"


def next_sort_state(current: SortState, field: SortField) -> SortState:
    if current.field != field or current.direction is None:
        return SortState(field, _DEFAULT_DIRECTIONS[field])
    direction = (
        SortDirection.DESCENDING
        if current.direction == SortDirection.ASCENDING
        else SortDirection.ASCENDING
    )
    return SortState(field, direction)


def _filename_key(track: Track) -> tuple:
    base = strip_canonical_numbering(track.original_filename)
    # Transliteration makes Chinese names participate in the same alphabetical
    # order as Latin names: 林俊杰 -> linjunjie, 周杰伦 -> zhoujielun.
    phonetic_name = "".join(lazy_pinyin(base, errors="default")).casefold()
    return (
        natural_sort_key(phonetic_name),
        phonetic_name,
        base.casefold(),
        track.original_filename.casefold(),
        str(track.original_path).casefold(),
    )


def _tie_key(track: Track) -> tuple:
    """Provide deterministic ordering when a primary metadata value ties."""

    return _filename_key(track)


def sort_tracks(
    tracks: Sequence[Track], field: SortField, direction: SortDirection
) -> list[Track]:
    """Return a new sorted list without touching any file on disk."""

    reverse = direction == SortDirection.DESCENDING
    if field == SortField.FILENAME:
        return sorted(tracks, key=_filename_key, reverse=reverse)

    if field == SortField.MODIFIED:
        primary = lambda track: track.modified_time if track.modified_time is not None else float("-inf")
    elif field == SortField.SIZE:
        primary = lambda track: track.size_bytes if track.size_bytes is not None else -1
    else:
        primary = lambda track: track.file_type.casefold()

    # Sorting twice uses Python's stability: the natural filename key remains a
    # deterministic ascending tie-breaker in either primary direction.
    deterministic = sorted(tracks, key=_tie_key)
    return sorted(deterministic, key=primary, reverse=reverse)
