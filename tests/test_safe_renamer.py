from collections.abc import Iterator
from pathlib import Path

import pytest

from app.models.track import Track
from app.services.filename_numbering import (
    build_apply_numbering_plan,
    build_remove_numbering_plan,
)
from app.services.safe_renamer import (
    RenameAction,
    RenameExecutionError,
    RenameValidationError,
    SafeRenamer,
)


def token_sequence() -> Iterator[str]:
    number = 0
    while True:
        number += 1
        yield f"testtoken{number}"


def make_tracks(folder: Path, names: list[str]) -> list[Track]:
    tracks = []
    for name in names:
        path = folder / name
        path.touch()
        tracks.append(Track.from_path(path))
    return tracks


def test_collision_with_unrelated_existing_target_is_detected(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["001 A.mp3", "A.mp3"])
    with pytest.raises(RenameValidationError, match="Target already exists"):
        SafeRenamer().execute(build_remove_numbering_plan(tracks))
    assert {path.name for path in tmp_path.iterdir()} == {"001 A.mp3", "A.mp3"}


def test_duplicate_targets_are_detected_before_changes(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["001 A.mp3", "002 A.mp3"])
    with pytest.raises(RenameValidationError, match="same target filename"):
        SafeRenamer().execute(build_remove_numbering_plan(tracks))
    assert {path.name for path in tmp_path.iterdir()} == {"001 A.mp3", "002 A.mp3"}


def test_execution_uses_two_stage_temporary_names(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["A.mp3", "B.mp3"])
    calls: list[tuple[str, str]] = []
    tokens = token_sequence()

    def recording_rename(source: Path, target: Path) -> None:
        calls.append((source.name, target.name))
        source.rename(target)

    renamer = SafeRenamer(rename=recording_rename, token_factory=lambda: next(tokens))
    renamer.execute(build_apply_numbering_plan(tracks))

    assert len(calls) == 4
    assert all(target.startswith(".__usb_track_order_tmp_") for _, target in calls[:2])
    assert [target for _, target in calls[2:]] == ["001 A.mp3", "002 B.mp3"]


def test_failure_rolls_back_every_changed_file(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["A.mp3", "B.mp3"])
    calls = 0
    tokens = token_sequence()

    def fail_at_first_final_rename(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("simulated failure")
        source.rename(target)

    renamer = SafeRenamer(
        rename=fail_at_first_final_rename,
        token_factory=lambda: next(tokens),
    )
    with pytest.raises(RenameExecutionError) as error:
        renamer.execute(build_apply_numbering_plan(tracks))

    assert error.value.rollback_complete
    assert {path.name for path in tmp_path.iterdir()} == {"A.mp3", "B.mp3"}
    assert not any(".__usb_track_order_tmp_" in path.name for path in tmp_path.iterdir())


def test_undo_apply_numbering(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["A.mp3", "B.mp3"])
    renamer = SafeRenamer()
    receipt = renamer.execute(build_apply_numbering_plan(tracks))
    renamer.execute(receipt.undo_actions())
    assert {path.name for path in tmp_path.iterdir()} == {"A.mp3", "B.mp3"}


def test_undo_remove_numbering(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["001 A.mp3", "002 B.mp3"])
    renamer = SafeRenamer()
    receipt = renamer.execute(build_remove_numbering_plan(tracks))
    renamer.execute(receipt.undo_actions())
    assert {path.name for path in tmp_path.iterdir()} == {"001 A.mp3", "002 B.mp3"}


def test_unsafe_undo_is_rejected_if_original_name_was_reused(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["A.mp3"])
    renamer = SafeRenamer()
    receipt = renamer.execute(build_apply_numbering_plan(tracks))
    (tmp_path / "A.mp3").touch()

    with pytest.raises(RenameValidationError, match="Target already exists"):
        renamer.execute(receipt.undo_actions())
    assert {path.name for path in tmp_path.iterdir()} == {"001 A.mp3", "A.mp3"}


def test_unsafe_undo_detects_externally_changed_renamed_file(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["A.mp3"])
    renamer = SafeRenamer()
    receipt = renamer.execute(build_apply_numbering_plan(tracks))
    (tmp_path / "001 A.mp3").write_bytes(b"externally replaced content")

    with pytest.raises(RenameValidationError, match="changed outside"):
        renamer.execute(receipt.undo_actions())
    assert (tmp_path / "001 A.mp3").read_bytes() == b"externally replaced content"


def test_source_changed_since_plan_creation_is_rejected(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["A.mp3"])
    plan = build_apply_numbering_plan(tracks)
    (tmp_path / "A.mp3").write_bytes(b"changed after preview")

    with pytest.raises(RenameValidationError, match="changed outside"):
        SafeRenamer().execute(plan)
    assert (tmp_path / "A.mp3").exists()


def test_missing_source_is_detected_before_any_rename(tmp_path: Path) -> None:
    source = tmp_path / "missing.mp3"
    action = RenameAction(source, tmp_path / "001 missing.mp3")
    with pytest.raises(RenameValidationError, match="Source file is missing"):
        SafeRenamer().execute([action])
    assert list(tmp_path.iterdir()) == []


def test_invalid_windows_target_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "A.mp3"
    source.touch()
    action = RenameAction(source, tmp_path / "bad?.mp3")
    with pytest.raises(RenameValidationError, match="invalid character"):
        SafeRenamer().execute([action])
    assert source.exists()
