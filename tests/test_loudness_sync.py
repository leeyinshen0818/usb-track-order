from pathlib import Path
import threading
import time

import pytest

from app.models.track import Track
from app.services.loudness_sync import (
    CancellationToken,
    CommandResult,
    LoudnessStateCache,
    LoudnessStatus,
    LoudnessValidationError,
    build_loudness_plan,
    choose_tracks,
    discover_ffmpeg,
    synchronize_loudness,
    validate_target_lufs,
)


def make_tracks(folder: Path, names: list[str]) -> list[Track]:
    tracks = []
    for index, name in enumerate(names):
        path = folder / name
        path.write_bytes(f"original-{index}".encode())
        tracks.append(Track.from_path(path))
    return tracks


def cache_for(folder: Path, name: str = "loudness-state.json") -> LoudnessStateCache:
    return LoudnessStateCache(folder / name)


@pytest.mark.parametrize("value", [-20, -14.0, "-10.0"])
def test_target_lufs_accepts_inclusive_range(value: object) -> None:
    assert validate_target_lufs(value) == float(value)


@pytest.mark.parametrize("value", [-20.1, -9.9, "nope", float("nan"), True])
def test_target_lufs_rejects_invalid_values(value: object) -> None:
    with pytest.raises(LoudnessValidationError):
        validate_target_lufs(value)


def test_ffmpeg_discovery_prefers_bundled_binary(
    tmp_path: Path, monkeypatch
) -> None:
    bundled = tmp_path / "ffmpeg" / "ffmpeg-bundled.exe"
    bundled.parent.mkdir()
    bundled.write_bytes(b"bundled executable")
    monkeypatch.setattr(
        "app.services.loudness_sync.sys._MEIPASS", str(tmp_path), raising=False
    )
    monkeypatch.setattr(
        "app.services.loudness_sync.shutil.which", lambda _name: "path-ffmpeg.exe"
    )
    assert discover_ffmpeg() == str(bundled)


def test_selected_tracks_or_all_tracks_keep_list_order(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["C.mp3", "A.flac", "B.wav"])
    assert [track.original_filename for track in choose_tracks(tracks, [2, 0])] == [
        "C.mp3",
        "B.wav",
    ]
    assert choose_tracks(tracks, []) == tracks


def successful_runner(command, _cancellation) -> CommandResult:
    if "-n" in command:
        Path(command[-1]).write_bytes(b"normalized-audio")
    return CommandResult(0)


def test_uses_temp_file_and_replaces_only_after_successful_output(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["Song.mp3"])
    source = tracks[0].original_path
    original = source.read_bytes()
    plan = build_loudness_plan(
        tracks, [], -14, ffmpeg_path="ffmpeg", state_cache=cache_for(tmp_path)
    )
    replace_calls = []

    def checked_replace(temporary: Path, destination: Path) -> None:
        assert destination == source
        assert temporary.parent == source.parent
        assert temporary.suffix == source.suffix
        assert temporary != source
        assert source.read_bytes() == original
        assert temporary.read_bytes() == b"normalized-audio"
        replace_calls.append((temporary.name, destination.name))
        temporary.replace(destination)

    result = synchronize_loudness(
        plan,
        CancellationToken(),
        command_runner=successful_runner,
        replace_file=checked_replace,
    )
    assert result.status == LoudnessStatus.COMPLETED
    assert source.read_bytes() == b"normalized-audio"
    assert replace_calls[0][1] == "Song.mp3"
    assert not list(tmp_path.glob(".*.loudness-*"))


def test_ffmpeg_failure_preserves_original_and_removes_temp(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["Song.m4a"])
    source = tracks[0].original_path
    original = source.read_bytes()
    plan = build_loudness_plan(
        tracks, [], -14, ffmpeg_path="ffmpeg", state_cache=cache_for(tmp_path)
    )

    def failing_runner(command, _cancellation) -> CommandResult:
        if "-n" in command:
            Path(command[-1]).write_bytes(b"partial")
            return CommandResult(1, stderr="simulated encode failure")
        raise AssertionError("no additional FFmpeg command should run")

    result = synchronize_loudness(
        plan, CancellationToken(), command_runner=failing_runner
    )
    assert result.status == LoudnessStatus.FAILED
    assert source.read_bytes() == original
    assert not list(tmp_path.glob(".*.loudness-*"))


def test_filename_extension_and_order_are_preserved(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["003 Three.AAC", "001 One.mp3", "002 Two.FLAC"])
    names_before = [track.original_filename for track in tracks]
    plan = build_loudness_plan(
        tracks, [], -14, ffmpeg_path="ffmpeg", state_cache=cache_for(tmp_path)
    )
    result = synchronize_loudness(
        plan, CancellationToken(), command_runner=successful_runner
    )
    assert result.status == LoudnessStatus.COMPLETED
    assert [track.original_filename for track in tracks] == names_before
    assert [snapshot.filename for snapshot in plan.tracks] == names_before
    assert [path.name for path in (track.original_path for track in tracks)] == names_before


def test_cancellation_stops_before_next_track(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["A.mp3", "B.mp3"])
    plan = build_loudness_plan(
        tracks, [], -14, ffmpeg_path="ffmpeg", state_cache=cache_for(tmp_path)
    )
    cancellation = CancellationToken()
    started = []

    def runner(command, token) -> CommandResult:
        if "-i" in command:
            source = Path(command[command.index("-i") + 1])
            started.append(source.name)
        return successful_runner(command, token)

    def progress(update) -> None:
        if update.completed_count == 1:
            cancellation.cancel()

    result = synchronize_loudness(
        plan,
        cancellation,
        progress=progress,
        command_runner=runner,
        max_concurrency=1,
    )
    assert result.status == LoudnessStatus.CANCELLED
    assert result.completed_count == 1
    assert "B.mp3" not in started
    assert tracks[1].original_path.read_bytes() == b"original-1"


def test_cancellation_during_ffmpeg_preserves_original(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["Song.flac"])
    source = tracks[0].original_path
    original = source.read_bytes()
    plan = build_loudness_plan(
        tracks, [], -14, ffmpeg_path="ffmpeg", state_cache=cache_for(tmp_path)
    )

    def cancelling_runner(command, cancellation) -> CommandResult:
        Path(command[-1]).write_bytes(b"interrupted-output")
        cancellation.cancel()
        return CommandResult(1, stderr="terminated", cancelled=True)

    result = synchronize_loudness(
        plan, CancellationToken(), command_runner=cancelling_runner
    )
    assert result.status == LoudnessStatus.CANCELLED
    assert source.read_bytes() == original
    assert not list(tmp_path.glob(".*.loudness-*"))


def test_default_worker_pool_never_exceeds_six_tracks(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, [f"Track-{index}.flac" for index in range(7)])
    plan = build_loudness_plan(
        tracks, [], -14, ffmpeg_path="ffmpeg", state_cache=cache_for(tmp_path)
    )
    lock = threading.Lock()
    release_workers = threading.Event()
    active = 0
    maximum_active = 0

    def runner(command, token) -> CommandResult:
        nonlocal active, maximum_active
        if "-n" in command:
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
                if active == 6:
                    release_workers.set()
            release_workers.wait(timeout=2)
            time.sleep(0.02)
            with lock:
                active -= 1
            Path(command[-1]).write_bytes(b"normalized-audio")
        return CommandResult(0)

    result = synchronize_loudness(
        plan, CancellationToken(), command_runner=runner
    )
    assert result.status == LoudnessStatus.COMPLETED
    assert result.completed_count == 7
    assert maximum_active == 6


def test_cancellation_aborts_active_workers_and_schedules_no_more(
    tmp_path: Path,
) -> None:
    tracks = make_tracks(
        tmp_path,
        ["A.mp3", "B.mp3", "C.mp3", "D.mp3", "E.mp3", "F.mp3", "G.mp3"],
    )
    originals = {track.original_filename: track.original_path.read_bytes() for track in tracks}
    plan = build_loudness_plan(
        tracks, [], -14, ffmpeg_path="ffmpeg", state_cache=cache_for(tmp_path)
    )
    cancellation = CancellationToken()
    six_active = threading.Event()
    lock = threading.Lock()
    active = 0
    started: set[str] = set()
    result_holder = []

    def runner(command, _token) -> CommandResult:
        nonlocal active
        if "-n" not in command:
            raise AssertionError("unexpected FFmpeg command")
        source = Path(command[command.index("-i") + 1])
        with lock:
            started.add(source.name)
            active += 1
            if active == 6:
                six_active.set()
        assert six_active.wait(timeout=2)
        deadline = time.monotonic() + 2
        while not cancellation.cancelled and time.monotonic() < deadline:
            time.sleep(0.005)
        assert cancellation.cancelled
        return CommandResult(1, stderr="terminated", cancelled=True)

    thread = threading.Thread(
        target=lambda: result_holder.append(
            synchronize_loudness(plan, cancellation, command_runner=runner)
        )
    )
    thread.start()
    assert six_active.wait(timeout=2)
    cancellation.cancel()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert result_holder[0].status == LoudnessStatus.CANCELLED
    assert started == {"A.mp3", "B.mp3", "C.mp3", "D.mp3", "E.mp3", "F.mp3"}
    assert {
        track.original_filename: track.original_path.read_bytes() for track in tracks
    } == originals


def test_track_failure_is_isolated_from_other_workers(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["A.wav", "B.wav", "C.wav"])
    failed_original = tracks[1].original_path.read_bytes()
    plan = build_loudness_plan(
        tracks, [], -14, ffmpeg_path="ffmpeg", state_cache=cache_for(tmp_path)
    )

    def runner(command, token) -> CommandResult:
        if "-n" in command:
            source = Path(command[command.index("-i") + 1])
            if source.name == "B.wav":
                Path(command[-1]).write_bytes(b"partial")
                return CommandResult(1, stderr="B failed")
            Path(command[-1]).write_bytes(b"valid-normalized-audio")
        return CommandResult(0)

    result = synchronize_loudness(
        plan, CancellationToken(), command_runner=runner
    )
    assert result.status == LoudnessStatus.FAILED
    assert result.completed_count == 2
    assert result.failed_filename == "B.wav"
    assert tracks[0].original_path.read_bytes() == b"valid-normalized-audio"
    assert tracks[1].original_path.read_bytes() == failed_original
    assert tracks[2].original_path.read_bytes() == b"valid-normalized-audio"
    assert not list(tmp_path.glob(".*.loudness-*"))


def test_never_synced_file_is_processed(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["New.mp3"])
    plan = build_loudness_plan(
        tracks, [], -14, ffmpeg_path="ffmpeg", state_cache=cache_for(tmp_path)
    )
    assert [track.filename for track in plan.tracks] == ["New.mp3"]
    result = synchronize_loudness(
        plan, CancellationToken(), command_runner=successful_runner
    )
    assert result.completed_count == 1
    assert result.skipped_count == 0


def test_fast_mode_uses_one_ffmpeg_command_per_track(tmp_path: Path) -> None:
    tracks = make_tracks(tmp_path, ["Song.mp3"])
    plan = build_loudness_plan(
        tracks, [], -14, ffmpeg_path="ffmpeg", state_cache=cache_for(tmp_path)
    )
    commands = []

    def runner(command, token) -> CommandResult:
        commands.append(command)
        return successful_runner(command, token)

    result = synchronize_loudness(plan, CancellationToken(), command_runner=runner)
    assert result.status == LoudnessStatus.COMPLETED
    assert len(commands) == 1
    filter_value = commands[0][commands[0].index("-af") + 1]
    assert "loudnorm=I=-14.0" in filter_value
    assert "measured_I" not in filter_value
    assert commands[0][commands[0].index("-ar") + 1] == "48000"
    assert commands[0][commands[0].index("-filter_threads") + 1] == "1"
    assert commands[0][commands[0].index("-threads") + 1] == "2"


def test_same_file_and_target_is_skipped_after_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    tracks = make_tracks(tmp_path, ["Song.mp3"])
    first = build_loudness_plan(
        tracks,
        [],
        -14,
        ffmpeg_path="ffmpeg",
        state_cache=LoudnessStateCache(state_path),
    )
    synchronize_loudness(first, CancellationToken(), command_runner=successful_runner)

    reloaded_tracks = [Track.from_path(tracks[0].original_path)]
    restarted_cache = LoudnessStateCache(state_path)
    second = build_loudness_plan(
        reloaded_tracks,
        [],
        -14,
        ffmpeg_path="ffmpeg",
        state_cache=restarted_cache,
    )
    assert second.tracks == ()
    result = synchronize_loudness(
        second,
        CancellationToken(),
        command_runner=lambda *_args: pytest.fail("FFmpeg must not run"),
    )
    assert result.completed_count == 0
    assert result.skipped_count == 1


def test_same_file_with_different_target_is_processed(tmp_path: Path) -> None:
    cache = cache_for(tmp_path)
    tracks = make_tracks(tmp_path, ["Song.flac"])
    first = build_loudness_plan(
        tracks, [], -14, ffmpeg_path="ffmpeg", state_cache=cache
    )
    synchronize_loudness(first, CancellationToken(), command_runner=successful_runner)

    second = build_loudness_plan(
        [Track.from_path(tracks[0].original_path)],
        [],
        -12,
        ffmpeg_path="ffmpeg",
        state_cache=cache,
    )
    assert len(second.tracks) == 1
    assert second.skipped_count == 0
    synchronize_loudness(second, CancellationToken(), command_runner=successful_runner)
    at_new_target = build_loudness_plan(
        [Track.from_path(tracks[0].original_path)],
        [],
        -12,
        ffmpeg_path="ffmpeg",
        state_cache=cache,
    )
    assert at_new_target.tracks == ()
    assert at_new_target.skipped_count == 1


def test_externally_changed_file_is_processed_again(tmp_path: Path) -> None:
    cache = cache_for(tmp_path)
    tracks = make_tracks(tmp_path, ["Song.wav"])
    first = build_loudness_plan(
        tracks, [], -14, ffmpeg_path="ffmpeg", state_cache=cache
    )
    synchronize_loudness(first, CancellationToken(), command_runner=successful_runner)
    tracks[0].original_path.write_bytes(b"externally-changed-audio")

    second = build_loudness_plan(
        tracks, [], -14, ffmpeg_path="ffmpeg", state_cache=cache
    )
    assert len(second.tracks) == 1
    assert second.skipped_count == 0


def test_rename_keeps_synced_recognition(tmp_path: Path) -> None:
    cache = cache_for(tmp_path)
    tracks = make_tracks(tmp_path, ["Song.aac"])
    first = build_loudness_plan(
        tracks, [], -14, ffmpeg_path="ffmpeg", state_cache=cache
    )
    synchronize_loudness(first, CancellationToken(), command_runner=successful_runner)
    renamed = tmp_path / "001 Song.aac"
    tracks[0].original_path.rename(renamed)

    second = build_loudness_plan(
        [Track.from_path(renamed)],
        [],
        -14,
        ffmpeg_path="ffmpeg",
        state_cache=cache,
    )
    assert second.tracks == ()
    assert second.skipped_count == 1


def test_corrupted_cache_is_ignored_safely(tmp_path: Path) -> None:
    state_path = tmp_path / "broken.json"
    state_path.write_text("{ definitely not JSON", encoding="utf-8")
    tracks = make_tracks(tmp_path, ["Song.m4a"])
    plan = build_loudness_plan(
        tracks,
        [],
        -14,
        ffmpeg_path="ffmpeg",
        state_cache=LoudnessStateCache(state_path),
    )
    assert len(plan.tracks) == 1


def test_failed_processing_does_not_update_cache(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    tracks = make_tracks(tmp_path, ["Song.mp3"])
    plan = build_loudness_plan(
        tracks,
        [],
        -14,
        ffmpeg_path="ffmpeg",
        state_cache=LoudnessStateCache(state_path),
    )

    def fail(command, _token) -> CommandResult:
        Path(command[-1]).write_bytes(b"partial")
        return CommandResult(1, stderr="failed")

    result = synchronize_loudness(plan, CancellationToken(), command_runner=fail)
    assert result.status == LoudnessStatus.FAILED
    retry = build_loudness_plan(
        tracks,
        [],
        -14,
        ffmpeg_path="ffmpeg",
        state_cache=LoudnessStateCache(state_path),
    )
    assert len(retry.tracks) == 1


def test_skipped_tracks_are_counted_with_processed_tracks(tmp_path: Path) -> None:
    cache = cache_for(tmp_path)
    first_track = make_tracks(tmp_path, ["A.mp3"])
    first = build_loudness_plan(
        first_track, [], -14, ffmpeg_path="ffmpeg", state_cache=cache
    )
    synchronize_loudness(first, CancellationToken(), command_runner=successful_runner)
    second_track = make_tracks(tmp_path, ["B.mp3"])
    mixed = [Track.from_path(first_track[0].original_path), second_track[0]]
    plan = build_loudness_plan(
        mixed, [], -14, ffmpeg_path="ffmpeg", state_cache=cache
    )
    result = synchronize_loudness(
        plan, CancellationToken(), command_runner=successful_runner
    )
    assert result.total_count == 2
    assert result.completed_count == 1
    assert result.skipped_count == 1
    assert result.failed_count == 0
