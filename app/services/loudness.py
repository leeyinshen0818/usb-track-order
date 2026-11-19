"""Measure-only loudness analysis, content-keyed cache, and export gain planning.

Source files are never modified. Analysis decodes each file once with FFmpeg's
``ebur128`` filter and records integrated loudness and true peak. Gain is applied
only to the copies written to the USB drive (see ``usb_exporter``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from app.paths import data_dir

logger = logging.getLogger("usb_track_order.loudness")

MIN_TARGET_LUFS = -20.0
MAX_TARGET_LUFS = -10.0
DEFAULT_TARGET_LUFS = -14.0
STATE_VERSION = 2
SIGNATURE_SAMPLE_BYTES = 64 * 1024
ANALYSIS_WORKERS = 4
CACHE_SAVE_INTERVAL = 10
GAIN_TOLERANCE_LU = 0.5
TRUE_PEAK_CEILING_DBTP = -1.5
LOSSY_BITRATE = "256k"


class LoudnessValidationError(ValueError):
    """The requested loudness value or operation is not valid."""


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


# --------------------------------------------------------------------------- #
# Target validation and FFmpeg discovery
# --------------------------------------------------------------------------- #


def validate_target_lufs(value: object) -> float:
    """Return a finite target inside the UI's supported inclusive range."""

    if isinstance(value, bool):
        raise LoudnessValidationError("Target loudness must be a number.")
    try:
        target = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise LoudnessValidationError("Target loudness must be a number.") from exc
    if not math.isfinite(target):
        raise LoudnessValidationError("Target loudness must be a finite number.")
    if not MIN_TARGET_LUFS <= target <= MAX_TARGET_LUFS:
        raise LoudnessValidationError(
            f"Target loudness must be between {MIN_TARGET_LUFS:.1f} and "
            f"{MAX_TARGET_LUFS:.1f} LUFS."
        )
    return target


def discover_ffmpeg() -> str | None:
    """Find bundled FFmpeg first, then development-install and PATH fallbacks."""

    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root is not None:
        ffmpeg_dir = Path(bundle_root) / "ffmpeg"
        for candidate in sorted(ffmpeg_dir.glob("ffmpeg*.exe")):
            if candidate.is_file():
                return str(candidate)
    try:
        from imageio_ffmpeg import get_ffmpeg_exe

        packaged = Path(get_ffmpeg_exe())
        if packaged.is_file():
            return str(packaged)
    except (ImportError, OSError, RuntimeError):
        pass
    return shutil.which("ffmpeg")


# --------------------------------------------------------------------------- #
# Command execution
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    cancelled: bool = False


class CommandRunner(Protocol):
    def __call__(
        self, command: Sequence[str], cancellation: CancellationToken
    ) -> CommandResult: ...


def run_command(command: Sequence[str], cancellation: CancellationToken) -> CommandResult:
    """Run a hidden child process, terminating it promptly on cancellation."""

    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
    except OSError as exc:
        return CommandResult(-1, stderr=str(exc))

    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.1)
            return CommandResult(process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            if not cancellation.cancelled:
                continue
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            return CommandResult(process.returncode, stdout, stderr, cancelled=True)


def failure_reason(result: CommandResult, fallback: str) -> str:
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    return lines[-1] if lines else fallback


# --------------------------------------------------------------------------- #
# Fingerprint and cache
# --------------------------------------------------------------------------- #


def fingerprint_file(path: Path) -> str:
    """Content key: size + hash of the first and last 64 KB.

    Deliberately excludes path, NTFS file ID, and timestamps so that copies,
    renames, and numbering changes still hit the cache.
    """

    size = path.stat().st_size
    digest = hashlib.blake2b(digest_size=16)
    with path.open("rb") as stream:
        digest.update(stream.read(SIGNATURE_SAMPLE_BYTES))
        if size > SIGNATURE_SAMPLE_BYTES:
            stream.seek(max(SIGNATURE_SAMPLE_BYTES, size - SIGNATURE_SAMPLE_BYTES))
            digest.update(stream.read(SIGNATURE_SAMPLE_BYTES))
    return f"{size}:{digest.hexdigest()}"


@dataclass(frozen=True, slots=True)
class LoudnessMeasurement:
    integrated_lufs: float
    true_peak_dbtp: float  # -inf for digital silence


def default_loudness_state_path() -> Path:
    """Cache file inside the (overridable) per-user data directory."""

    return data_dir() / "loudness_state.json"


def _peak_to_json(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _peak_from_json(value: object) -> float:
    if value is None:
        return -math.inf
    peak = float(value)  # type: ignore[arg-type]
    if math.isnan(peak):
        raise ValueError("NaN peak")
    return peak


class LoudnessCache:
    """Thread-safe JSON cache of measurements keyed by content fingerprint.

    Documents from older versions are ignored (not migrated) and overwritten on
    the next save.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_loudness_state_path()
        self._lock = threading.Lock()
        self._entries: dict[str, LoudnessMeasurement] = {}
        self._dirty = False
        self.load_note = "loaded"
        self._load()

    def _load(self) -> None:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.load_note = "cache file does not exist yet"
            return
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self.load_note = f"cache file unreadable ({type(exc).__name__})"
            return
        if not isinstance(document, dict) or document.get("version") != STATE_VERSION:
            self.load_note = "cache file is an old or unknown format and was ignored"
            return
        entries = document.get("entries")
        if not isinstance(entries, dict):
            self.load_note = "cache file has no valid entries"
            return
        for key, value in entries.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            try:
                lufs = float(value["lufs"])
                peak = _peak_from_json(value.get("true_peak"))
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(lufs):
                continue
            self._entries[key] = LoudnessMeasurement(lufs, peak)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def get(self, key: str) -> LoudnessMeasurement | None:
        with self._lock:
            return self._entries.get(key)

    def miss_reason(self, key: str) -> str:
        with self._lock:
            count = len(self._entries)
        return f"no entry for fingerprint {key} ({count} cached; {self.load_note})"

    def put(self, key: str, measurement: LoudnessMeasurement) -> None:
        with self._lock:
            if self._entries.get(key) == measurement:
                return
            self._entries[key] = measurement
            self._dirty = True

    def save(self) -> bool:
        """Atomically persist the cache (temp file + replace). Returns success."""

        with self._lock:
            if not self._dirty:
                return True
            document = {
                "version": STATE_VERSION,
                "entries": {
                    key: {
                        "lufs": round(entry.integrated_lufs, 2),
                        "true_peak": _peak_to_json(round(entry.true_peak_dbtp, 2)),
                    }
                    for key, entry in self._entries.items()
                },
            }
            temporary: Path | None = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_name(
                    f".{self.path.name}.{uuid.uuid4().hex}.tmp"
                )
                temporary.write_text(
                    json.dumps(document, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                os.replace(temporary, self.path)
            except OSError as exc:
                logger.warning("Could not save loudness cache %s: %s", self.path, exc)
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
                return False
            self._dirty = False
            return True


# --------------------------------------------------------------------------- #
# Analysis (measure only)
# --------------------------------------------------------------------------- #

_INTEGRATED_RE = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS\s*$", re.MULTILINE)
_PEAK_RE = re.compile(r"^\s*Peak:\s*(-?inf|-?\d+(?:\.\d+)?)\s*dBFS\s*$", re.MULTILINE)


def analysis_command(ffmpeg: str, source: Path) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-nostats",
        "-nostdin",
        "-threads",
        "1",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-filter:a",
        "ebur128=peak=true:framelog=quiet",
        "-threads",
        "1",
        "-f",
        "null",
        "-",
    ]


def parse_ebur128_summary(stderr: str) -> LoudnessMeasurement:
    """Read the final summary block printed by FFmpeg's ebur128 filter."""

    integrated = _INTEGRATED_RE.findall(stderr)
    peaks = _PEAK_RE.findall(stderr)
    if not integrated or not peaks:
        raise ValueError("FFmpeg did not report a loudness summary.")
    lufs = float(integrated[-1])
    peak_text = peaks[-1]
    peak = -math.inf if peak_text.endswith("inf") else float(peak_text)
    return LoudnessMeasurement(lufs, peak)


@dataclass(frozen=True, slots=True)
class AnalysisJob:
    path: Path
    key: str


@dataclass(frozen=True, slots=True)
class AnalysisProgress:
    analyzed_count: int
    cached_count: int
    failed_count: int
    total_count: int
    current_filename: str


class AnalysisStatus(Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    status: AnalysisStatus
    analyzed_count: int
    cached_count: int
    total_count: int
    failures: dict[str, str]  # fingerprint key -> reason

    @property
    def failed_count(self) -> int:
        return len(self.failures)


def plan_analysis(
    paths: Sequence[Path], cache: LoudnessCache
) -> tuple[list[AnalysisJob], int, dict[str, str]]:
    """Split paths into uncached jobs, a cached count, and fingerprint failures.

    Duplicate content (same key) is analyzed once. A miss reason is logged for
    every uncached file.
    """

    jobs: list[AnalysisJob] = []
    cached = 0
    failures: dict[str, str] = {}
    seen: set[str] = set()
    for path in paths:
        try:
            key = fingerprint_file(path)
        except OSError as exc:
            reason = f"cannot read file ({exc.strerror or exc})"
            logger.info("Loudness cache miss for %s: %s", path.name, reason)
            failures[f"path:{path}"] = reason
            continue
        if cache.get(key) is not None:
            cached += 1
            continue
        logger.info("Loudness cache miss for %s: %s", path.name, cache.miss_reason(key))
        if key in seen:
            continue
        seen.add(key)
        jobs.append(AnalysisJob(path, key))
    return jobs, cached, failures


def _measure(
    ffmpeg: str,
    job: AnalysisJob,
    cancellation: CancellationToken,
    run: CommandRunner,
) -> LoudnessMeasurement | str | None:
    """Return a measurement, a failure reason, or None when cancelled."""

    if cancellation.cancelled:
        return None
    result = run(analysis_command(ffmpeg, job.path), cancellation)
    if result.cancelled or cancellation.cancelled:
        return None
    if result.returncode != 0:
        return failure_reason(result, "FFmpeg could not decode the file.")
    try:
        measurement = parse_ebur128_summary(result.stderr)
    except ValueError as exc:
        return str(exc)
    try:
        if fingerprint_file(job.path) != job.key:
            return "The file changed while it was being analyzed."
    except OSError:
        return "The file disappeared while it was being analyzed."
    return measurement


def analyze_loudness(
    jobs: Sequence[AnalysisJob],
    cache: LoudnessCache,
    ffmpeg: str,
    cancellation: CancellationToken,
    *,
    cached_count: int = 0,
    initial_failures: dict[str, str] | None = None,
    progress: Callable[[AnalysisProgress], None] | None = None,
    command_runner: CommandRunner | None = None,
    max_workers: int = ANALYSIS_WORKERS,
    save_interval: int = CACHE_SAVE_INTERVAL,
) -> AnalysisResult:
    """Measure jobs in a bounded pool; cache is saved periodically and at the end."""

    run = command_runner or run_command
    failures = dict(initial_failures or {})
    analyzed = 0
    unsaved = 0
    total = len(jobs) + cached_count + len(failures)
    pending = list(jobs)
    pending.reverse()
    active: dict[Future[LoudnessMeasurement | str | None], AnalysisJob] = {}

    def report(name: str) -> None:
        if progress:
            progress(AnalysisProgress(analyzed, cached_count, len(failures), total, name))

    try:
        with ThreadPoolExecutor(
            max_workers=max(1, max_workers), thread_name_prefix="loudness"
        ) as executor:

            def fill() -> None:
                while pending and len(active) < max_workers and not cancellation.cancelled:
                    job = pending.pop()
                    active[executor.submit(_measure, ffmpeg, job, cancellation, run)] = job
                    report(job.path.name)

            fill()
            while active:
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    job = active.pop(future)
                    outcome = future.result()
                    if isinstance(outcome, LoudnessMeasurement):
                        cache.put(job.key, outcome)
                        analyzed += 1
                        unsaved += 1
                        if unsaved >= save_interval:
                            cache.save()
                            unsaved = 0
                    elif isinstance(outcome, str):
                        logger.warning(
                            "Loudness analysis failed for %s: %s", job.path.name, outcome
                        )
                        failures[job.key] = outcome
                    report(job.path.name)
                fill()
    finally:
        cache.save()

    status = AnalysisStatus.CANCELLED if cancellation.cancelled else AnalysisStatus.COMPLETED
    return AnalysisResult(status, analyzed, cached_count, total, failures)


# --------------------------------------------------------------------------- #
# Export gain
# --------------------------------------------------------------------------- #


def compute_gain_db(
    measurement: LoudnessMeasurement,
    target_lufs: float,
    *,
    tolerance_lu: float = GAIN_TOLERANCE_LU,
    true_peak_ceiling: float = TRUE_PEAK_CEILING_DBTP,
) -> float | None:
    """Return the gain to apply, or None when a plain copy is close enough.

    Positive gain is capped so the predicted true peak stays at or below the
    ceiling. Silent tracks (no finite peak) are never boosted.
    """

    gain = target_lufs - measurement.integrated_lufs
    if gain > 0:
        peak = measurement.true_peak_dbtp
        headroom = true_peak_ceiling - peak if math.isfinite(peak) else 0.0
        gain = max(0.0, min(gain, headroom))
    gain = round(gain, 2)
    if abs(gain) <= tolerance_lu:
        return None
    return gain


def gain_command(ffmpeg: str, source: Path, output: Path, gain_db: float) -> list[str]:
    """Apply a fixed gain, re-encoding in the source's family without resampling."""

    suffix = source.suffix.casefold()
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-n",
        "-threads",
        "1",
        "-i",
        str(source),
        "-map",
        "0:a:0",
    ]
    keeps_artwork = suffix in {".mp3", ".m4a", ".flac"}
    if keeps_artwork:
        command += ["-map", "0:v?", "-c:v", "copy"]
    command += ["-map_metadata", "0", "-filter:a", f"volume={gain_db:.2f}dB"]
    if suffix == ".mp3":
        command += ["-c:a", "libmp3lame", "-b:a", LOSSY_BITRATE, "-id3v2_version", "3"]
    elif suffix in {".m4a", ".aac"}:
        command += ["-c:a", "aac", "-b:a", LOSSY_BITRATE]
    elif suffix == ".flac":
        command += ["-c:a", "flac"]
    elif suffix == ".wav":
        command += ["-c:a", "pcm_s16le"]
    else:
        raise ValueError(f"Unsupported audio format for gain: {source.suffix}")
    command += ["-threads", "1", str(output)]
    return command
