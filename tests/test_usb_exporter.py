import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.models.track import Track
from app.services.loudness import (
    CommandResult,
    LoudnessCache,
    LoudnessMeasurement,
    analyze_loudness,
    discover_ffmpeg,
    fingerprint_file,
    plan_analysis,
)
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


# --- .part writes ------------------------------------------------------------


def test_files_are_written_as_part_then_renamed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    source.mkdir()
    destination.mkdir()
    tracks = make_tracks(source, ["A.mp3", "B.mp3"])
    plan = build_export_plan(tracks, destination, available_bytes=100)
    written: list[str] = []

    def copy(source_path: Path, target_path: Path) -> None:
        written.append(target_path.name)
        assert not (destination / source_path.name).exists()
        target_path.write_bytes(source_path.read_bytes())

    result = copy_sequentially(plan, CancellationToken(), copy_file=copy)
    assert result.status == ExportStatus.COMPLETED
    assert written == ["A.mp3.part", "B.mp3.part"]
    assert sorted(p.name for p in destination.iterdir()) == ["A.mp3", "B.mp3"]


def test_failed_copy_removes_part_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    source.mkdir()
    destination.mkdir()
    tracks = make_tracks(source, ["A.mp3"])
    plan = build_export_plan(tracks, destination, available_bytes=100)

    def half_then_fail(_source: Path, target: Path) -> None:
        target.write_bytes(b"x")
        raise OSError("device error")

    result = copy_sequentially(plan, CancellationToken(), copy_file=half_then_fail)
    assert result.status == ExportStatus.FAILED
    assert list(destination.iterdir()) == []


def test_cancel_during_default_copy_removes_part_file(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    source.mkdir()
    destination.mkdir()
    big = source / "Big.mp3"
    big.write_bytes(b"\1" * (3 * 1024 * 1024))
    plan = build_export_plan([Track.from_path(big)], destination, available_bytes=10**9)
    token = CancellationToken()
    real_open = Path.open

    def open_and_cancel(self, mode="r", *args, **kwargs):
        stream = real_open(self, mode, *args, **kwargs)
        if mode == "xb":
            token.cancel()
        return stream

    monkeypatch.setattr(Path, "open", open_and_cancel)
    result = copy_sequentially(plan, token)
    assert result.status == ExportStatus.CANCELLED
    assert list(destination.iterdir()) == []


def test_stale_part_file_is_a_collision(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    source.mkdir()
    destination.mkdir()
    tracks = make_tracks(source, ["A.mp3"])
    (destination / "A.mp3.part").write_bytes(b"stale")
    with pytest.raises(ExportValidationError, match="already exist"):
        build_export_plan(tracks, destination, available_bytes=100)


# --- loudness gain -----------------------------------------------------------


def gain_runner(log: list[str] | None = None, delay=lambda name: 0.0):
    def run(command, cancellation):
        source = Path(command[command.index("-i") + 1])
        output = Path(command[-1])
        time.sleep(delay(source.name))
        if cancellation.cancelled:
            return CommandResult(-1, cancelled=True)
        if log is not None:
            log.append(source.name)
        volume = next(part for part in command if part.startswith("volume="))
        output.write_bytes(source.read_bytes() + volume.encode())
        return CommandResult(0)

    return run


def loud_setup(tmp_path: Path, names: list[str], measurements):
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    source.mkdir()
    destination.mkdir()
    tracks = make_tracks(source, names)
    cache = LoudnessCache(tmp_path / "state.json")
    for track, measurement in zip(tracks, measurements):
        if measurement is not None:
            cache.put(fingerprint_file(track.original_path), measurement)
    return tracks, destination, cache


def test_missing_analysis_blocks_export_with_reason(tmp_path: Path) -> None:
    tracks, destination, cache = loud_setup(
        tmp_path, ["A.mp3", "B.mp3", "C.mp3"],
        [LoudnessMeasurement(-14, -1), None, None],
    )
    failures = {fingerprint_file(tracks[1].original_path): "Invalid data found"}
    with pytest.raises(ExportValidationError) as error:
        build_export_plan(
            tracks, destination, available_bytes=100, target_lufs=-14.0,
            state_cache=cache, analysis_failures=failures, ffmpeg_path="ffmpeg",
        )
    assert error.value.missing_analysis == (
        "B.mp3 — Invalid data found",
        "C.mp3 — not analyzed yet",
    )
    assert list(destination.iterdir()) == []


def test_within_tolerance_is_plain_copy_and_others_get_gain(tmp_path: Path) -> None:
    tracks, destination, cache = loud_setup(
        tmp_path, ["A.mp3", "B.mp3"],
        [LoudnessMeasurement(-14.4, -3.0), LoudnessMeasurement(-11.0, -0.5)],
    )
    plan = build_export_plan(
        tracks, destination, available_bytes=100, target_lufs=-14.0,
        state_cache=cache, ffmpeg_path="ffmpeg",
    )
    assert [t.gain_db for t in plan.tracks] == [None, -3.0]
    encoded: list[str] = []
    result = copy_sequentially(
        plan, CancellationToken(), command_runner=gain_runner(encoded),
        temp_root=tmp_path,
    )
    assert result.status == ExportStatus.COMPLETED
    assert encoded == ["B.mp3"]
    assert (destination / "A.mp3").read_bytes() == tracks[0].original_path.read_bytes()
    assert (destination / "B.mp3").read_bytes().endswith(b"volume=-3.00dB")
    assert tracks[1].original_path.read_bytes() == b"\2\2"


def test_output_fingerprint_is_cached_so_usb_copies_hit(tmp_path: Path) -> None:
    tracks, destination, cache = loud_setup(
        tmp_path, ["A.mp3"], [LoudnessMeasurement(-20.0, -10.0)]
    )
    plan = build_export_plan(
        tracks, destination, available_bytes=100, target_lufs=-14.0,
        state_cache=cache, ffmpeg_path="ffmpeg",
    )
    copy_sequentially(plan, CancellationToken(), command_runner=gain_runner())
    reloaded = LoudnessCache(tmp_path / "state.json")
    measured = reloaded.get(fingerprint_file(destination / "A.mp3"))
    assert measured == LoudnessMeasurement(-14.0, -4.0)


def test_parallel_prepare_but_sequential_ordered_writes(tmp_path: Path) -> None:
    names = [f"{i:03d}.mp3" for i in range(12)]
    tracks, destination, cache = loud_setup(
        tmp_path, names, [LoudnessMeasurement(-20.0, -10.0)] * 12
    )
    plan = build_export_plan(
        tracks, destination, available_bytes=1000, target_lufs=-14.0,
        state_cache=cache, ffmpeg_path="ffmpeg",
    )
    lock = threading.Lock()
    active = peak = 0
    base = gain_runner(delay=lambda name: 0.05 if name == "000.mp3" else 0.005)

    def runner(command, token):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            return base(command, token)
        finally:
            with lock:
                active -= 1

    writes: list[str] = []
    writing = False

    def copy(source_path: Path, target_path: Path) -> None:
        nonlocal writing
        assert not writing
        writing = True
        writes.append(target_path.name)
        target_path.write_bytes(source_path.read_bytes())
        writing = False

    result = copy_sequentially(
        plan, CancellationToken(), copy_file=copy, command_runner=runner,
        temp_root=tmp_path,
    )
    assert result.status == ExportStatus.COMPLETED
    assert writes == [f"{name}.part" for name in names]
    assert 1 < peak <= 4


def test_ready_queue_is_bounded_to_eight(tmp_path: Path) -> None:
    names = [f"{i:03d}.mp3" for i in range(20)]
    tracks, destination, cache = loud_setup(
        tmp_path, names, [LoudnessMeasurement(-20.0, -10.0)] * 20
    )
    plan = build_export_plan(
        tracks, destination, available_bytes=1000, target_lufs=-14.0,
        state_cache=cache, ffmpeg_path="ffmpeg",
    )
    started: list[str] = []
    seen_before_first_write: list[int] = []

    def copy(source_path: Path, target_path: Path) -> None:
        if not seen_before_first_write:
            time.sleep(0.3)  # let preparation run ahead of the first write
            seen_before_first_write.append(len(started))
        target_path.write_bytes(source_path.read_bytes())

    result = copy_sequentially(
        plan, CancellationToken(), copy_file=copy, command_runner=gain_runner(started),
        temp_root=tmp_path,
    )
    assert result.status == ExportStatus.COMPLETED
    assert seen_before_first_write == [8]


def test_cancel_cleans_temp_and_part_files(tmp_path: Path) -> None:
    names = [f"{i:03d}.mp3" for i in range(10)]
    tracks, destination, cache = loud_setup(
        tmp_path, names, [LoudnessMeasurement(-20.0, -10.0)] * 10
    )
    plan = build_export_plan(
        tracks, destination, available_bytes=1000, target_lufs=-14.0,
        state_cache=cache, ffmpeg_path="ffmpeg",
    )
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    token = CancellationToken()

    def progress(update) -> None:
        if update.completed_count == 2:
            token.cancel()

    result = copy_sequentially(
        plan, token, progress=progress,
        command_runner=gain_runner(delay=lambda _n: 0.01), temp_root=temp_root,
    )
    assert result.status == ExportStatus.CANCELLED
    assert result.completed_count == 2
    assert sorted(p.name for p in destination.iterdir()) == ["000.mp3", "001.mp3"]
    assert list(temp_root.iterdir()) == []


def test_encode_failure_stops_export_and_cleans_up(tmp_path: Path) -> None:
    tracks, destination, cache = loud_setup(
        tmp_path, ["A.mp3", "B.mp3"],
        [LoudnessMeasurement(-14.0, -1.0), LoudnessMeasurement(-20.0, -10.0)],
    )
    plan = build_export_plan(
        tracks, destination, available_bytes=100, target_lufs=-14.0,
        state_cache=cache, ffmpeg_path="ffmpeg",
    )
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    result = copy_sequentially(
        plan, CancellationToken(),
        command_runner=lambda _c, _t: CommandResult(1, stderr="Encoder exploded"),
        temp_root=temp_root,
    )
    assert result.status == ExportStatus.FAILED
    assert result.failed_filename == "B.mp3"
    assert "Encoder exploded" in (result.reason or "")
    assert sorted(p.name for p in destination.iterdir()) == ["A.mp3"]
    assert list(temp_root.iterdir()) == []


FFMPEG = discover_ffmpeg()


@pytest.mark.skipif(FFMPEG is None, reason="FFmpeg not available")
def test_real_ffmpeg_analysis_and_gain_end_to_end(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "usb"
    source.mkdir()
    destination.mkdir()
    song = source / "Quiet.mp3"
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=3:sample_rate=44100", "-af", "volume=-10dB",
         "-c:a", "libmp3lame", "-b:a", "128k", "-metadata", "title=Quiet", str(song)],
        check=True,
    )
    original = song.read_bytes()
    cache = LoudnessCache(tmp_path / "state.json")
    jobs, _cached, _failures = plan_analysis([song], cache)
    assert analyze_loudness(jobs, cache, FFMPEG, CancellationToken()).analyzed_count == 1
    measured = cache.get(fingerprint_file(song))
    assert measured is not None and measured.integrated_lufs < -25

    plan = build_export_plan(
        [Track.from_path(song)], destination, available_bytes=10**9,
        target_lufs=-14.0, state_cache=cache, ffmpeg_path=FFMPEG,
    )
    assert plan.tracks[0].gain_db is not None
    result = copy_sequentially(plan, CancellationToken())
    assert result.status == ExportStatus.COMPLETED
    assert song.read_bytes() == original

    exported = destination / "Quiet.mp3"
    probe = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(exported)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stderr
    assert "44100 Hz" in probe and "Quiet" in probe and "256 kb/s" in probe

    # A file copied back from the USB hits the cache.
    jobs, cached, _ = plan_analysis([exported], LoudnessCache(tmp_path / "state.json"))
    assert jobs == [] and cached == 1

    # The real loudness of the output matches the predicted value.
    fresh = LoudnessCache(tmp_path / "fresh.json")
    jobs, _, _ = plan_analysis([exported], fresh)
    analyze_loudness(jobs, fresh, FFMPEG, CancellationToken())
    actual = fresh.get(fingerprint_file(exported))
    predicted = cache.get(fingerprint_file(exported))
    assert actual is not None and predicted is not None
    assert abs(actual.integrated_lufs - predicted.integrated_lufs) < 1.0
    assert actual.true_peak_dbtp <= -1.0
