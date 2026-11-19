"""Canonical USB Track Order prefix handling and rename-plan construction."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from app.models.track import Track
from app.services.file_scanner import natural_sort_key
from app.services.safe_renamer import RenameAction, RenameReceipt
from app.services.track_ordering import preview_number

# The application emits "NNN " (three digits, or 1000+ without a leading zero)
# followed by the original name. Requiring that width protects titles such as
# "99 Luftballons.mp3" and "7 rings.mp3"; refusing a remainder that starts with
# a separator protects artist names such as "5566 - Song.mp3".
_CANONICAL_PREFIX = re.compile(
    r"^(?:[0-9]{3}|[1-9][0-9]{3,}) (?![-\u2013\u2014_.,&+)\]])([^ ].*)$"
)


def has_canonical_numbering(filename: str) -> bool:
    return strip_canonical_numbering(filename) != filename


def strip_canonical_numbering(filename: str) -> str:
    """Strip one application-style numeric prefix, otherwise return unchanged."""

    match = _CANONICAL_PREFIX.fullmatch(filename)
    if not match:
        return filename
    remainder = match.group(1)
    stem = remainder.rsplit(".", 1)[0] if "." in remainder else remainder
    if not stem.strip(" ."):
        return filename
    return remainder


def numbered_filename(filename: str, position: int) -> str:
    """Return the canonical filename for a one-based visual position."""

    return f"{preview_number(position)} {strip_canonical_numbering(filename)}"


def filenames_follow_order(tracks: Sequence[Track]) -> bool:
    """True when sorting the filenames by name gives the list order.

    That is what a player sorting by filename would play. False after an
    unapplied reorder, so Apply Numbering is still needed.
    """

    keys = [natural_sort_key(track.original_filename) for track in tracks]
    return all(first <= second for first, second in zip(keys, keys[1:]))


def build_apply_numbering_plan(tracks: Sequence[Track]) -> list[RenameAction]:
    actions: list[RenameAction] = []
    for position, track in enumerate(tracks, start=1):
        target = track.original_path.with_name(
            numbered_filename(track.original_filename, position)
        )
        if target.name != track.original_filename:
            actions.append(
                RenameAction(track.original_path, target, track.file_identity)
            )
    return actions


def build_remove_numbering_plan(tracks: Sequence[Track]) -> list[RenameAction]:
    actions: list[RenameAction] = []
    for track in tracks:
        filename = strip_canonical_numbering(track.original_filename)
        if filename != track.original_filename:
            actions.append(
                RenameAction(
                    track.original_path,
                    track.original_path.with_name(filename),
                    track.file_identity,
                )
            )
    return actions


def refresh_tracks_from_receipt(
    tracks: Sequence[Track], receipt: RenameReceipt
) -> list[Track]:
    """Refresh paths from a receipt without changing the current visual order."""

    renamed = {action.source: action.target for action in receipt.actions}
    return [Track.from_path(renamed.get(track.original_path, track.original_path)) for track in tracks]
