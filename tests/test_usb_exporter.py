from pathlib import Path

import pytest

from app.models.track import Track
from app.services.usb_exporter import (
    CancellationToken,
    ExportStatus,
    ExportValidationError,
    build_export_plan,
    copy_sequentially,
)


def make_tracks(folder: Path, names: list[str]) -> list[Track]:
    tracks = []
    for index, name in enumerate(names, start=1):
        path = folder / name
        path.write_bytes(bytes([index]) * index)
        tracks.append(Track.from_path(path))
    return tracks


def test_export_plan_validates_sources_total_and_snapshot_order(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    source.mkdir()
    destination.mkdir()
    tracks = make_tracks(source, ["C.mp3", "A.mp3", "B.mp3"])
    plan = build_export_plan(tracks, destination, available_bytes=100)
    assert [track.filename for track in plan.tracks] == ["C.mp3", "A.mp3", "B.mp3"]
    assert plan.total_bytes == 1 + 2 + 3

    tracks.reverse()
    assert [track.filename for track in plan.tracks] == ["C.mp3", "A.mp3", "B.mp3"]


def test_insufficient_space_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    source.mkdir()
    destination.mkdir()
    tracks = make_tracks(source, ["A.mp3", "B.mp3"])
    with pytest.raises(ExportValidationError, match="Not enough") as error:
        build_export_plan(tracks, destination, available_bytes=2)
    assert error.value.required_bytes == 3
    assert error.value.available_bytes == 2


def test_existing_destination_collision_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    source.mkdir()
    destination.mkdir()
    tracks = make_tracks(source, ["A.MP3"])
    (destination / "a.mp3").write_bytes(b"existing")
    with pytest.raises(ExportValidationError, match="already exist") as error:
        build_export_plan(tracks, destination, available_bytes=100)
    assert error.value.collisions == ("a.mp3",)


def test_collision_check_uses_selected_nested_folder(tmp_path: Path) -> None:
    source = tmp_path / "source"
    usb_root = tmp_path / "usb"
    destination = usb_root / "Music"
    source.mkdir()
    destination.mkdir(parents=True)
    tracks = make_tracks(source, ["A.mp3"])
    (usb_root / "A.mp3").write_bytes(b"root copy is irrelevant")
    plan = build_export_plan(tracks, destination, available_bytes=100)
    assert plan.destination == destination

    (destination / "A.mp3").write_bytes(b"nested collision")
    with pytest.raises(ExportValidationError, match="already exist"):
        build_export_plan(tracks, destination, available_bytes=100)


def test_missing_or_changed_source_is_rejected_before_copy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    source.mkdir()
    destination.mkdir()
    tracks = make_tracks(source, ["A.mp3"])
    tracks[0].original_path.unlink()
    with pytest.raises(ExportValidationError, match="no longer exists"):
        build_export_plan(tracks, destination, available_bytes=100)


def test_externally_changed_source_is_rejected_before_copy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    source.mkdir()
    destination.mkdir()
    tracks = make_tracks(source, ["A.mp3"])
    tracks[0].original_path.write_bytes(b"changed after loading")
    with pytest.raises(ExportValidationError, match="changed outside"):
        build_export_plan(tracks, destination, available_bytes=100)


def test_files_copy_strictly_in_order_without_parallelism(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    source.mkdir()
    destination.mkdir()
    tracks = make_tracks(source, ["003 三.mp3", "001 One.MP3", "002 Dua.flac"])
    plan = build_export_plan(tracks, destination, available_bytes=100)
    calls: list[str] = []
    active = False

    def instrumented_copy(source_path: Path, target_path: Path) -> None:
        nonlocal active
        assert not active
        active = True
        calls.append(source_path.name)
        target_path.write_bytes(source_path.read_bytes())
        active = False

    result = copy_sequentially(
        plan, CancellationToken(), copy_file=instrumented_copy
    )
    assert result.status == ExportStatus.COMPLETED
    assert calls == ["003 三.mp3", "001 One.MP3", "002 Dua.flac"]
    assert {path.name for path in destination.iterdir()} == set(calls)


def test_sequential_export_copies_directly_into_selected_nested_folder(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    usb_root = tmp_path / "usb"
    destination = usb_root / "Car Music"
    source.mkdir()
    destination.mkdir(parents=True)
    tracks = make_tracks(source, ["001 A.mp3", "002 B.MP3"])
    plan = build_export_plan(tracks, destination, available_bytes=100)
    result = copy_sequentially(plan, CancellationToken())
    assert result.status == ExportStatus.COMPLETED
    assert {path.name for path in destination.iterdir()} == {
        "001 A.mp3",
        "002 B.MP3",
    }
    assert list(usb_root.glob("*.mp3")) == []


def test_size_mismatch_stops_export(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    source.mkdir()
    destination.mkdir()
    tracks = make_tracks(source, ["A.mp3", "B.mp3"])
    plan = build_export_plan(tracks, destination, available_bytes=100)

    def wrong_size(_source: Path, target: Path) -> None:
        target.write_bytes(b"wrong-size")

    result = copy_sequentially(plan, CancellationToken(), copy_file=wrong_size)
    assert result.status == ExportStatus.FAILED
    assert result.completed_count == 0
    assert result.failed_filename == "A.mp3"
    assert "size does not match" in (result.reason or "")


def test_copy_stops_on_failure_and_keeps_completed_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    source.mkdir()
    destination.mkdir()
    tracks = make_tracks(source, ["A.mp3", "B.mp3", "C.mp3"])
    plan = build_export_plan(tracks, destination, available_bytes=100)

    def fail_second(source_path: Path, target_path: Path) -> None:
        if source_path.name == "B.mp3":
            raise OSError("simulated copy failure")
        target_path.write_bytes(source_path.read_bytes())

    result = copy_sequentially(plan, CancellationToken(), copy_file=fail_second)
    assert result.status == ExportStatus.FAILED
    assert result.completed_count == 1
    assert (destination / "A.mp3").exists()
    assert not (destination / "C.mp3").exists()


def test_missing_source_mid_export_stops_without_skipping(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    source.mkdir()
    destination.mkdir()
    tracks = make_tracks(source, ["A.mp3", "B.mp3"])
    plan = build_export_plan(tracks, destination, available_bytes=100)

    def progress(update) -> None:
        if update.completed_count == 1:
            (source / "B.mp3").unlink()

    result = copy_sequentially(plan, CancellationToken(), progress=progress)
    assert result.status == ExportStatus.FAILED
    assert result.completed_count == 1
    assert not (destination / "B.mp3").exists()


def test_cancellation_stops_before_next_file_and_keeps_completed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    source.mkdir()
    destination.mkdir()
    tracks = make_tracks(source, ["A.mp3", "B.mp3", "C.mp3"])
    plan = build_export_plan(tracks, destination, available_bytes=100)
    cancellation = CancellationToken()

    def progress(update) -> None:
        if update.completed_count == 1:
            cancellation.cancel()

    result = copy_sequentially(plan, cancellation, progress=progress)
    assert result.status == ExportStatus.CANCELLED
    assert result.completed_count == 1
    assert {path.name for path in destination.iterdir()} == {"A.mp3"}


def test_destination_disappearing_is_reported(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    unplugged = tmp_path / "unplugged"
    source.mkdir()
    destination.mkdir()
    tracks = make_tracks(source, ["A.mp3", "B.mp3"])
    plan = build_export_plan(tracks, destination, available_bytes=100)

    def progress(update) -> None:
        if update.completed_count == 1:
            destination.rename(unplugged)

    result = copy_sequentially(plan, CancellationToken(), progress=progress)
    assert result.status == ExportStatus.FAILED
    assert result.completed_count == 1
    assert "disconnected" in (result.reason or "")
    assert (unplugged / "A.mp3").exists()


def test_progress_reports_start_and_completion_for_each_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    source.mkdir()
    destination.mkdir()
    tracks = make_tracks(source, ["A.mp3", "B.mp3"])
    plan = build_export_plan(tracks, destination, available_bytes=100)
    updates = []
    result = copy_sequentially(
        plan, CancellationToken(), progress=updates.append
    )
    assert result.status == ExportStatus.COMPLETED
    assert [(update.completed_count, update.current_filename) for update in updates] == [
        (0, "A.mp3"),
        (1, "A.mp3"),
        (1, "B.mp3"),
        (2, "B.mp3"),
    ]
