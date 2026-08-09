"""Canonical USB Track Order prefix handling and rename-plan construction."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from app.models.track import Track
from app.services.safe_renamer import RenameAction, RenameReceipt
from app.services.track_ordering import preview_number

# The application always emits at least three digits. Requiring that emitted
# width also protects legitimate titles such as "99 Luftballons.mp3".
_CANONICAL_PREFIX = re.compile(r"^[0-9]{3,} ([^ ].*)$")


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
