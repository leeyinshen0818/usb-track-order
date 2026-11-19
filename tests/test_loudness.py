import json
import logging
import math
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from app.services.loudness import (
    CACHE_SAVE_INTERVAL,
    SIGNATURE_SAMPLE_BYTES,
    AnalysisJob,
    AnalysisStatus,
    CancellationToken,
    CommandResult,
    LoudnessCache,
    LoudnessMeasurement,
    LoudnessValidationError,
    analyze_loudness,
    compute_gain_db,
    discover_ffmpeg,
    fingerprint_file,
    gain_command,
    parse_ebur128_summary,
    plan_analysis,
    validate_target_lufs,
)

SUMMARY = """
[Parsed_ebur128_0 @ 0000] Summary:

  Integrated loudness:
    I:         {lufs} LUFS
    Threshold: -32.2 LUFS

  True peak:
    Peak:      {peak} dBFS
"""


def summary(lufs: float = -18.0, peak: str = "-3.0") -> str:
    return SUMMARY.format(lufs=lufs, peak=peak)


def write(path: Path, size: int, seed: int = 1) -> Path:
    path.write_bytes(bytes((seed + i) % 251 for i in range(size)))
    return path


# --- target / discovery ------------------------------------------------------


@pytest.mark.parametrize("value", [-20, -14.0, "-10.0"])
def test_target_lufs_accepts_inclusive_range(value: object) -> None:
    assert validate_target_lufs(value) == float(value)


@pytest.mark.parametrize("value", [-20.1, -9.9, "nope", float("nan"), True])
def test_target_lufs_rejects_invalid_values(value: object) -> None:
    with pytest.raises(LoudnessValidationError):
        validate_target_lufs(value)


def test_ffmpeg_discovery_prefers_bundled_binary(tmp_path: Path, monkeypatch) -> None:
    bundled = tmp_path / "ffmpeg" / "ffmpeg-bundled.exe"
    bundled.parent.mkdir()
    bundled.write_bytes(b"bundled executable")
    monkeypatch.setattr("app.services.loudness.sys._MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr("app.services.loudness.shutil.which", lambda _name: "x.exe")
    assert discover_ffmpeg() == str(bundled)


# --- fingerprint -------------------------------------------------------------


def test_fingerprint_stable_across_copy_rename_and_numbering(tmp_path: Path) -> None:
    original = write(tmp_path / "Song.mp3", 300_000)
    key = fingerprint_file(original)

    other = tmp_path / "other"
    other.mkdir()
    copied = other / "Song.mp3"
    shutil.copyfile(original, copied)
    os.utime(copied, (1_000_000, 1_000_000))
    numbered = tmp_path / "001 Song.mp3"
    original.rename(numbered)

    assert fingerprint_file(copied) == key
    assert fingerprint_file(numbered) == key
    assert key.startswith("300000:")


def test_fingerprint_changes_with_head_tail_or_size(tmp_path: Path) -> None:
    base = write(tmp_path / "a.mp3", 300_000)
    key = fingerprint_file(base)
    data = bytearray(base.read_bytes())

    data[10] ^= 0xFF
    head = tmp_path / "head.mp3"
    head.write_bytes(bytes(data))
    data[10] ^= 0xFF
    data[-10] ^= 0xFF
    tail = tmp_path / "tail.mp3"
    tail.write_bytes(bytes(data))
    longer = tmp_path / "longer.mp3"
    longer.write_bytes(base.read_bytes() + b"\0")

    keys = {fingerprint_file(p) for p in (head, tail, longer)}
    assert key not in keys and len(keys) == 3


def test_fingerprint_handles_files_smaller_than_sample(tmp_path: Path) -> None:
    small = write(tmp_path / "s.mp3", SIGNATURE_SAMPLE_BYTES // 2)
    assert fingerprint_file(small) == fingerprint_file(small)


# --- cache -------------------------------------------------------------------


def test_cache_hit_miss_and_reload(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    cache = LoudnessCache(path)
    assert cache.get("1:abc") is None
    cache.put("1:abc", LoudnessMeasurement(-18.25, -2.5))
    cache.put("2:def", LoudnessMeasurement(-70.0, -math.inf))
    assert cache.save()

    reloaded = LoudnessCache(path)
    assert reloaded.get("1:abc") == LoudnessMeasurement(-18.25, -2.5)
    assert reloaded.get("2:def") == LoudnessMeasurement(-70.0, -math.inf)
    assert reloaded.get("3:missing") is None
    assert "no entry" in reloaded.miss_reason("3:missing")


def test_cache_save_is_atomic_and_leaves_no_temp_files(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "state.json"
    cache = LoudnessCache(path)
    cache.put("1:a", LoudnessMeasurement(-14.0, -1.0))
    cache.save()
    before = path.read_text(encoding="utf-8")

    cache.put("2:b", LoudnessMeasurement(-15.0, -1.0))

    def failing_replace(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr("app.services.loudness.os.replace", failing_replace)
    assert not cache.save()
    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    monkeypatch.undo()
    assert cache.save()
    assert LoudnessCache(path).get("2:b") is not None
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_old_format_cache_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [{"fingerprint": {"size_bytes": 1}, "target_lufs": -14.0}],
            }
        ),
        encoding="utf-8",
    )
    cache = LoudnessCache(path)
    assert len(cache) == 0
    assert "old" in cache.miss_reason("1:x")


def test_corrupted_cache_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    assert len(LoudnessCache(path)) == 0


# --- analysis ----------------------------------------------------------------


def test_parse_summary_reads_last_block_and_silence() -> None:
    measured = parse_ebur128_summary(summary(-20.0, "-5.0") + summary(-22.2, "-18.5"))
    assert measured == LoudnessMeasurement(-22.2, -18.5)
    silent = parse_ebur128_summary(summary(-70.0, "-inf"))
    assert silent.true_peak_dbtp == -math.inf
    with pytest.raises(ValueError):
        parse_ebur128_summary("garbage")


def test_plan_analysis_skips_cached_dedupes_and_logs_misses(
    tmp_path: Path, caplog
) -> None:
    cache = LoudnessCache(tmp_path / "state.json")
    cached = write(tmp_path / "cached.mp3", 1000, seed=1)
    cache.put(fingerprint_file(cached), LoudnessMeasurement(-14.0, -1.0))
    new = write(tmp_path / "new.mp3", 1000, seed=2)
    duplicate = tmp_path / "copy of new.mp3"
    shutil.copyfile(new, duplicate)

    with caplog.at_level(logging.INFO, logger="usb_track_order.loudness"):
        jobs, cached_count, failures = plan_analysis(
            [cached, new, duplicate, tmp_path / "gone.mp3"], cache
        )
    assert [job.path for job in jobs] == [new]
    assert cached_count == 1
    assert len(failures) == 1
    misses = [r.getMessage() for r in caplog.records if "cache miss" in r.getMessage()]
    assert len(misses) == 3
    assert any("new.mp3" in m and "no entry for fingerprint" in m for m in misses)


def test_analysis_is_measure_only_and_never_writes_sources(tmp_path: Path) -> None:
    source = write(tmp_path / "a.mp3", 2000)
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    commands = []

    def runner(command, _token):
        commands.append(command)
        return CommandResult(0, stderr=summary(-19.5, "-4.25"))

    cache = LoudnessCache(tmp_path / "state.json")
    job = AnalysisJob(source, fingerprint_file(source))
    result = analyze_loudness([job], cache, "ffmpeg", CancellationToken(), command_runner=runner)

    assert result.status == AnalysisStatus.COMPLETED
    assert result.analyzed_count == 1
    assert cache.get(job.key) == LoudnessMeasurement(-19.5, -4.25)
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    (command,) = commands
    assert command[-3:] == ["-f", "null", "-"]
    assert "-threads" in command and command[command.index("-threads") + 1] == "1"
    assert any("ebur128" in part for part in command)
    assert LoudnessCache(tmp_path / "state.json").get(job.key) is not None


def test_analysis_uses_at_most_four_workers(tmp_path: Path) -> None:
    paths = [write(tmp_path / f"{i}.mp3", 100, seed=i) for i in range(12)]
    jobs = [AnalysisJob(path, fingerprint_file(path)) for path in paths]
    lock = threading.Lock()
    active = peak = 0

    def runner(_command, _token):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return CommandResult(0, stderr=summary())

    result = analyze_loudness(
        jobs, LoudnessCache(tmp_path / "s.json"), "ffmpeg", CancellationToken(),
        command_runner=runner,
    )
    assert result.analyzed_count == 12
    assert peak == 4


def test_analysis_saves_every_ten_files(tmp_path: Path, monkeypatch) -> None:
    jobs = []
    for i in range(CACHE_SAVE_INTERVAL * 2 + 3):
        path = write(tmp_path / f"{i}.mp3", 100, seed=i)
        jobs.append(AnalysisJob(path, fingerprint_file(path)))
    cache = LoudnessCache(tmp_path / "s.json")
    saves = []
    original_save = cache.save
    monkeypatch.setattr(cache, "save", lambda: saves.append(len(cache)) or original_save())

    analyze_loudness(
        jobs, cache, "ffmpeg", CancellationToken(),
        command_runner=lambda _c, _t: CommandResult(0, stderr=summary()),
        max_workers=1,
    )
    assert saves == [10, 20, 23]


def test_analysis_failure_is_recorded_not_cached(tmp_path: Path) -> None:
    good = write(tmp_path / "good.mp3", 100, seed=1)
    bad = write(tmp_path / "bad.mp3", 100, seed=2)
    jobs = [AnalysisJob(p, fingerprint_file(p)) for p in (good, bad)]

    def runner(command, _token):
        if "bad.mp3" in " ".join(command):
            return CommandResult(1, stderr="Invalid data found when processing input")
        return CommandResult(0, stderr=summary())

    cache = LoudnessCache(tmp_path / "s.json")
    result = analyze_loudness(jobs, cache, "ffmpeg", CancellationToken(), command_runner=runner)
    assert result.failures == {jobs[1].key: "Invalid data found when processing input"}
    assert cache.get(jobs[1].key) is None
    assert cache.get(jobs[0].key) is not None


def test_analysis_cancellation_stops_scheduling_and_saves(tmp_path: Path) -> None:
    jobs = []
    for i in range(10):
        path = write(tmp_path / f"{i}.mp3", 100, seed=i)
        jobs.append(AnalysisJob(path, fingerprint_file(path)))
    token = CancellationToken()
    calls = 0

    def runner(_command, cancellation):
        nonlocal calls
        calls += 1
        if calls == 2:
            cancellation.cancel()
            return CommandResult(-1, cancelled=True)
        return CommandResult(0, stderr=summary())

    cache_path = tmp_path / "s.json"
    result = analyze_loudness(
        jobs, LoudnessCache(cache_path), "ffmpeg", token, command_runner=runner, max_workers=1
    )
    assert result.status == AnalysisStatus.CANCELLED
    assert calls == 2
    assert result.analyzed_count == 1
    assert len(LoudnessCache(cache_path)) == 1


# --- gain --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lufs", "peak", "target", "expected"),
    [
        (-14.0, -1.0, -14.0, None),  # on target
        (-14.5, -6.0, -14.0, None),  # +0.5 LU is inside tolerance
        (-13.5, -1.0, -14.0, None),  # -0.5 LU is inside tolerance
        (-14.6, -6.0, -14.0, 0.6),  # just outside tolerance
        (-10.0, -0.1, -14.0, -4.0),  # negative gain is never capped
        (-20.0, -8.0, -14.0, 6.0),  # enough headroom
        (-20.0, -4.0, -14.0, 2.5),  # capped to keep TP <= -1.5 dBTP
        (-20.0, -1.8, -14.0, None),  # cap leaves 0.3 dB -> within tolerance
        (-20.0, -0.5, -14.0, None),  # already too hot: never boosted, never cut
        (-70.0, -math.inf, -14.0, None),  # digital silence is never boosted
    ],
)
def test_compute_gain(lufs, peak, target, expected) -> None:
    assert compute_gain_db(LoudnessMeasurement(lufs, peak), target) == expected


@pytest.mark.parametrize(
    ("name", "codec", "artwork"),
    [
        ("a.mp3", ["-c:a", "libmp3lame", "-b:a", "256k"], True),
        ("a.M4A", ["-c:a", "aac", "-b:a", "256k"], True),
        ("a.aac", ["-c:a", "aac", "-b:a", "256k"], False),
        ("a.flac", ["-c:a", "flac"], True),
        ("a.wav", ["-c:a", "pcm_s16le"], False),
    ],
)
def test_gain_command_per_format(name, codec, artwork) -> None:
    command = gain_command("ffmpeg", Path(name), Path("out"), -3.456)
    joined = " ".join(command)
    assert " ".join(codec) in joined
    assert "volume=-3.46dB" in joined
    assert "-map_metadata 0" in joined
    assert "-ar" not in command and "-ac" not in command
    assert ("0:v?" in command) == artwork
    assert command[-1] == "out"
    assert "-n" in command


def test_loud_track_is_cut_in_full_regardless_of_true_peak() -> None:
    assert compute_gain_db(LoudnessMeasurement(-8.0, -0.5), -14.0) == -6.0
    assert compute_gain_db(LoudnessMeasurement(-8.0, 3.0), -14.0) == -6.0
    assert compute_gain_db(LoudnessMeasurement(-8.0, -30.0), -14.0) == -6.0


def test_quiet_track_boost_is_capped_by_true_peak() -> None:
    # wants +8, headroom to -1.5 dBTP is only +3.5
    assert compute_gain_db(LoudnessMeasurement(-22.0, -5.0), -14.0) == 3.5


def test_quiet_track_with_peak_above_ceiling_gets_zero_gain() -> None:
    assert compute_gain_db(LoudnessMeasurement(-22.0, -1.0), -14.0) is None
    assert compute_gain_db(LoudnessMeasurement(-22.0, 0.5), -14.0) is None


def test_within_half_lu_is_plain_copy() -> None:
    for lufs in (-14.5, -14.2, -14.0, -13.8, -13.5):
        assert compute_gain_db(LoudnessMeasurement(lufs, -20.0), -14.0) is None
