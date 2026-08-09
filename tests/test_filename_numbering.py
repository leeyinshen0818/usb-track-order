from pathlib import Path

import pytest

from app.models.track import Track
from app.services.filename_numbering import (
    build_apply_numbering_plan,
    build_remove_numbering_plan,
    numbered_filename,
    refresh_tracks_from_receipt,
    strip_canonical_numbering,
)
from app.services.safe_renamer import RenameReceipt, SafeRenamer


def make_tracks(folder: Path, names: list[str]) -> list[Track]:
    tracks = []
    for name in names:
        path = folder / name
        path.touch()
        tracks.append(Track.from_path(path))
    return tracks


def names_in(folder: Path) -> set[str]:
    return {path.name for path in folder.iterdir()}


def test_apply_numbering_basic_case(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["A.mp3", "B.mp3", "C.mp3"])
    receipt = SafeRenamer().execute(build_apply_numbering_plan(tracks))
    assert names_in(tmp_path) == {"001 A.mp3", "002 B.mp3", "003 C.mp3"}
    assert len(receipt.actions) == 3


def test_apply_numbering_uses_custom_visual_order(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["C.mp3", "A.mp3", "B.mp3"])
    SafeRenamer().execute(build_apply_numbering_plan(tracks))
    assert names_in(tmp_path) == {"001 C.mp3", "002 A.mp3", "003 B.mp3"}


def test_renumbering_does_not_stack_prefixes(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["002 B.mp3", "001 A.mp3"])
    SafeRenamer().execute(build_apply_numbering_plan(tracks))
    assert names_in(tmp_path) == {"001 B.mp3", "002 A.mp3"}
    assert all("001 001" not in name and "002 002" not in name for name in names_in(tmp_path))


def test_remove_numbering_and_leave_unnumbered_unchanged(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["001 A.mp3", "002 B.mp3", "C.mp3"])
    plan = build_remove_numbering_plan(tracks)
    SafeRenamer().execute(plan)
    assert names_in(tmp_path) == {"A.mp3", "B.mp3", "C.mp3"}
    assert len(plan) == 2


@pytest.mark.parametrize(
    "filename", ["1987.mp3", "24K Magic.mp3", "99 Luftballons.mp3", "01-Song.mp3", "01. Song.mp3"]
)
def test_legitimate_or_noncanonical_numeric_names_are_protected(filename: str) -> None:
    assert strip_canonical_numbering(filename) == filename


def test_unicode_filename_is_preserved(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["晴天.mp3", "Malam Ini.flac"])
    SafeRenamer().execute(build_apply_numbering_plan(tracks))
    assert names_in(tmp_path) == {"001 晴天.mp3", "002 Malam Ini.flac"}


def test_extension_casing_is_preserved(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["Song.MP3"])
    SafeRenamer().execute(build_apply_numbering_plan(tracks))
    assert names_in(tmp_path) == {"001 Song.MP3"}


def test_numbering_expands_beyond_999() -> None:
    assert numbered_filename("Song.mp3", 999) == "999 Song.mp3"
    assert numbered_filename("Song.mp3", 1000) == "1000 Song.mp3"
    assert numbered_filename("Song.mp3", 1001) == "1001 Song.mp3"


def test_no_op_apply_numbering(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["001 A.mp3", "002 B.mp3"])
    assert build_apply_numbering_plan(tracks) == []


def test_no_op_remove_numbering(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["A.mp3", "1987.mp3"])
    assert build_remove_numbering_plan(tracks) == []


def test_refresh_after_rename_keeps_visual_order(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["C.mp3", "A.mp3", "B.mp3"])
    receipt = RenameReceipt(tuple(build_apply_numbering_plan(tracks)))
    refreshed = refresh_tracks_from_receipt(tracks, receipt)
    assert [track.original_filename for track in refreshed] == [
        "001 C.mp3",
        "002 A.mp3",
        "003 B.mp3",
    ]

