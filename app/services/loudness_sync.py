"""Fast, safe loudness normalization with rename-stable local state."""

from __future__ import annotations

import hashlib
import json
import math
import os
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

from app.models.track import Track

MIN_TARGET_LUFS = -20.0
MAX_TARGET_LUFS = -10.0
DEFAULT_TARGET_LUFS = -14.0
DEFAULT_MAX_CONCURRENCY = 6
STATE_VERSION = 1
SIGNATURE_SAMPLE_BYTES = 64 * 1024
FAST_OUTPUT_SAMPLE_RATE = 48_000
FFMPEG_THREADS_PER_JOB = 2


class LoudnessValidationError(ValueError):
    """The requested operation cannot safely be started."""


class LoudnessStatus(Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    device: int
    inode: int
    size_bytes: int
    modified_ns: int
    content_signature: str

    @property
    def cache_key(self) -> str:
        if self.inode:
            return f"id:{self.device}:{self.inode}"
        return f"sig:{self.size_bytes}:{self.content_signature}"

    def to_json(self) -> dict[str, int | str]:
        return {
            "device": self.device,
            "inode": self.inode,
            "size_bytes": self.size_bytes,
            "modified_ns": self.modified_ns,
            "content_signature": self.content_signature,
        }

    @classmethod
    def from_json(cls, value: object) -> "FileFingerprint | None":
        if not isinstance(value, dict):
            return None
        try:
            fingerprint = cls(
                device=int(value["device"]),
                inode=int(value["inode"]),
                size_bytes=int(value["size_bytes"]),
                modified_ns=int(value["modified_ns"]),
                content_signature=str(value["content_signature"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if fingerprint.size_bytes < 0 or not fingerprint.content_signature:
            return None
        return fingerprint


def default_loudness_state_path() -> Path:
    """Return a per-user application-data path without requiring Qt."""

    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"])
    elif os.environ.get("XDG_STATE_HOME"):
        root = Path(os.environ["XDG_STATE_HOME"])
    elif os.environ.get("XDG_DATA_HOME"):
        root = Path(os.environ["XDG_DATA_HOME"])
    else:
        root = Path.home() / ".local" / "share"
    return root / "USB Track Order" / "loudness_state.json"


class LoudnessStateCache:
    """Small, tolerant JSON cache for completed loudness work."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_loudness_state_path()
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[FileFingerprint, float]] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(document, dict) or document.get("version") != STATE_VERSION:
            return
        entries = document.get("entries")
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            fingerprint = FileFingerprint.from_json(entry.get("fingerprint"))
            try:
                target = float(entry["target_lufs"])
            except (KeyError, TypeError, ValueError):
                continue
            if fingerprint is None or not math.isfinite(target):
                continue
            self._entries[fingerprint.cache_key] = (fingerprint, target)

    def is_synced(self, fingerprint: FileFingerprint, target_lufs: float) -> bool:
        with self._lock:
            cached = self._entries.get(fingerprint.cache_key)
            return cached == (fingerprint, target_lufs)

    def mark_synced(
        self,
        fingerprint: FileFingerprint,
        target_lufs: float,
        *,
        previous_key: str | None = None,
    ) -> None:
        with self._lock:
            if previous_key and previous_key != fingerprint.cache_key:
                self._entries.pop(previous_key, None)
            self._entries[fingerprint.cache_key] = (fingerprint, float(target_lufs))
            self._dirty = True

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            document = {
                "version": STATE_VERSION,
                "entries": [
                    {
                        "fingerprint": fingerprint.to_json(),
                        "target_lufs": target,
                    }
                    for fingerprint, target in self._entries.values()
                ],
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
            except OSError:
                try:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                return
            self._dirty = False


@dataclass(frozen=True, slots=True)
class LoudnessTrack:
    source: Path
    filename: str
    file_identity: tuple[int, int, int, int]
    fingerprint: FileFingerprint


@dataclass(frozen=True, slots=True)
class LoudnessPlan:
    tracks: tuple[LoudnessTrack, ...]
    target_lufs: float
    ffmpeg_path: str
    selected_count: int
    skipped_count: int
    state_cache: LoudnessStateCache


@dataclass(frozen=True, slots=True)
class LoudnessProgress:
    completed_count: int
    total_count: int
    current_filename: str
    skipped_count: int = 0
    failed_count: int = 0


@dataclass(frozen=True, slots=True)
class LoudnessResult:
    status: LoudnessStatus
    completed_count: int
    total_count: int
    failed_filename: str | None = None
    reason: str | None = None
    skipped_count: int = 0
    failed_count: int = 0


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    cancelled: bool = False


class CommandRunner(Protocol):
    def __call__(
        self, command: Sequence[str], cancellation: "CancellationToken"
    ) -> CommandResult: ...


@dataclass(frozen=True, slots=True)
class _TrackResult:
    track: LoudnessTrack
    status: LoudnessStatus
    reason: str | None = None
    fingerprint: FileFingerprint | None = None


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


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


def choose_tracks(tracks: Sequence[Track], selected_rows: Sequence[int]) -> list[Track]:
    """Choose selected rows in list order, or every track when none are selected."""

    if not selected_rows:
        return list(tracks)
    selected = set(selected_rows)
    if any(not isinstance(row, int) or row < 0 or row >= len(tracks) for row in selected):
        raise LoudnessValidationError("The track selection is no longer valid.")
    return [track for row, track in enumerate(tracks) if row in selected]


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


def _identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def fingerprint_file(path: Path) -> FileFingerprint:
    """Fingerprint identity plus small content samples without reading the whole file."""

    stat = path.stat()
    digest = hashlib.blake2b(digest_size=16)
    with path.open("rb") as stream:
        digest.update(stream.read(SIGNATURE_SAMPLE_BYTES))
        if stat.st_size > SIGNATURE_SAMPLE_BYTES:
            stream.seek(max(SIGNATURE_SAMPLE_BYTES, stat.st_size - SIGNATURE_SAMPLE_BYTES))
            digest.update(stream.read(SIGNATURE_SAMPLE_BYTES))
    return FileFingerprint(
        device=stat.st_dev,
        inode=stat.st_ino,
        size_bytes=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        content_signature=digest.hexdigest(),
    )


def build_loudness_plan(
    tracks: Sequence[Track],
    selected_rows: Sequence[int],
    target_lufs: object,
    *,
    ffmpeg_path: str | None = None,
    state_cache: LoudnessStateCache | None = None,
) -> LoudnessPlan:
    target = validate_target_lufs(target_lufs)
    chosen = choose_tracks(tracks, selected_rows)
    if not chosen:
        raise LoudnessValidationError("No tracks are loaded.")
    executable = ffmpeg_path or discover_ffmpeg()
    if not executable:
        raise LoudnessValidationError(
            "FFmpeg was not found. Install FFmpeg and add it to PATH, then restart the app."
        )

    cache = state_cache or LoudnessStateCache()
    snapshots: list[LoudnessTrack] = []
    skipped_count = 0
    source_keys: set[str] = set()
    for track in chosen:
        try:
            identity = _identity(track.original_path)
        except OSError as exc:
            raise LoudnessValidationError(
                f"Track no longer exists or cannot be read: {track.original_path}"
            ) from exc
        source_key = os.path.normcase(str(track.original_path.resolve()))
        if source_key in source_keys:
            raise LoudnessValidationError(
                f"The same source file was selected more than once: {track.original_path}"
            )
        source_keys.add(source_key)
        try:
            fingerprint = fingerprint_file(track.original_path)
        except OSError as exc:
            raise LoudnessValidationError(
                f"Track no longer exists or cannot be read: {track.original_path}"
            ) from exc
        if cache.is_synced(fingerprint, target):
            skipped_count += 1
            continue
        snapshots.append(
            LoudnessTrack(
                track.original_path,
                track.original_filename,
                identity,
                fingerprint,
            )
        )
    return LoudnessPlan(
        tuple(snapshots),
        target,
        executable,
        len(chosen),
        skipped_count,
        cache,
    )


def _run_command(
    command: Sequence[str], cancellation: CancellationToken
) -> CommandResult:
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


def _normalize_command(
    ffmpeg: str,
    source: Path,
    temporary: Path,
    target: float,
) -> list[str]:
    loudnorm = f"loudnorm=I={target:.1f}:LRA=11:TP=-1.5:print_format=none"
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-filter_threads",
        "1",
        "-nostdin",
        "-n",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-map",
        "0:v?",
        "-map_metadata",
        "0",
        "-c:v",
        "copy",
        "-af",
        loudnorm,
        "-ar",
        str(FAST_OUTPUT_SAMPLE_RATE),
        "-threads",
        str(FFMPEG_THREADS_PER_JOB),
        str(temporary),
    ]


def _failure_reason(result: CommandResult, fallback: str) -> str:
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    return lines[-1] if lines else fallback


def _process_track(
    plan: LoudnessPlan,
    track: LoudnessTrack,
    cancellation: CancellationToken,
    run: CommandRunner,
    replace_file: Callable[[Path, Path], object],
) -> _TrackResult:
    """Run one isolated safe normalization pipeline."""

    temporary = track.source.with_name(
        f".{track.source.stem}.loudness-{uuid.uuid4().hex}{track.source.suffix}"
    )
    try:
        if cancellation.cancelled:
            return _TrackResult(track, LoudnessStatus.CANCELLED)
        if _identity(track.source) != track.file_identity:
            raise RuntimeError("The original file changed before processing began.")
        normalized = run(
            _normalize_command(
                plan.ffmpeg_path,
                track.source,
                temporary,
                plan.target_lufs,
            ),
            cancellation,
        )
        if normalized.cancelled or cancellation.cancelled:
            return _TrackResult(track, LoudnessStatus.CANCELLED)
        if normalized.returncode != 0:
            raise RuntimeError(
                _failure_reason(normalized, "FFmpeg normalization failed.")
            )
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError("FFmpeg did not create a valid output file.")
        if _identity(track.source) != track.file_identity:
            raise RuntimeError("The original file changed while FFmpeg was running.")
        replace_file(temporary, track.source)
        try:
            fingerprint = fingerprint_file(track.source)
        except OSError:
            fingerprint = None
        return _TrackResult(
            track, LoudnessStatus.COMPLETED, fingerprint=fingerprint
        )
    except Exception as exc:
        return _TrackResult(
            track,
            LoudnessStatus.FAILED,
            str(exc) or "Loudness synchronization failed.",
        )
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def synchronize_loudness(
    plan: LoudnessPlan,
    cancellation: CancellationToken,
    *,
    progress: Callable[[LoudnessProgress], None] | None = None,
    command_runner: CommandRunner | None = None,
    replace_file: Callable[[Path, Path], object] = os.replace,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> LoudnessResult:
    """Normalize tracks with a small pool, replacing each only after validation."""

    if (
        not isinstance(max_concurrency, int)
        or isinstance(max_concurrency, bool)
        or max_concurrency < 1
    ):
        raise ValueError("max_concurrency must be a positive integer.")
    run = command_runner or _run_command
    completed = 0
    total = plan.selected_count
    skipped = plan.skipped_count
    process_total = len(plan.tracks)
    failures: list[_TrackResult] = []
    next_track = 0
    worker_count = min(max_concurrency, process_total) if process_total else 1
    active: dict[Future[_TrackResult], LoudnessTrack] = {}

    with ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="loudness"
    ) as executor:

        def schedule_available() -> None:
            nonlocal next_track
            while (
                not cancellation.cancelled
                and len(active) < worker_count
                and next_track < process_total
            ):
                track = plan.tracks[next_track]
                next_track += 1
                future = executor.submit(
                    _process_track,
                    plan,
                    track,
                    cancellation,
                    run,
                    replace_file,
                )
                active[future] = track
                if progress:
                    progress(
                        LoudnessProgress(
                            completed,
                            total,
                            track.filename,
                            skipped,
                            len(failures),
                        )
                    )

        schedule_available()
        while active:
            finished, _pending = wait(active, return_when=FIRST_COMPLETED)
            for future in finished:
                track = active.pop(future)
                outcome = future.result()
                if outcome.status == LoudnessStatus.COMPLETED:
                    completed += 1
                    if outcome.fingerprint is not None:
                        plan.state_cache.mark_synced(
                            outcome.fingerprint,
                            plan.target_lufs,
                            previous_key=track.fingerprint.cache_key,
                        )
                elif outcome.status == LoudnessStatus.FAILED:
                    failures.append(outcome)
                if progress:
                    progress(
                        LoudnessProgress(
                            completed,
                            total,
                            track.filename,
                            skipped,
                            len(failures),
                        )
                    )
            schedule_available()

    plan.state_cache.save()
    if cancellation.cancelled:
        return LoudnessResult(
            LoudnessStatus.CANCELLED,
            completed,
            total,
            skipped_count=skipped,
            failed_count=len(failures),
        )
    if failures:
        first = failures[0]
        detail = first.reason or "Loudness synchronization failed."
        if len(failures) > 1:
            detail = f"{len(failures)} tracks failed. First error: {detail}"
        return LoudnessResult(
            LoudnessStatus.FAILED,
            completed,
            total,
            first.track.filename,
            detail,
            skipped,
            len(failures),
        )
    return LoudnessResult(
        LoudnessStatus.COMPLETED,
        completed,
        total,
        skipped_count=skipped,
    )
