"""Immutable USB export planning and strictly sequential file writing.

Per-track loudness gain is prepared in parallel into a local temporary folder,
but files are written to the USB drive one at a time in list order (FAT32
directory order matters to car stereos). Each file is written as
``<name>.part`` with exclusive creation, verified, then renamed.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app.models.track import Track
from app.services.loudness import (
    CancellationToken,
    CommandRunner,
    LoudnessCache,
    LoudnessMeasurement,
    compute_gain_db,
    discover_ffmpeg,
    failure_reason,
    fingerprint_file,
    gain_command,
    run_command,
)

__all__ = [
    "CancellationToken",
    "ExportPlan",
    "ExportProgress",
    "ExportResult",
    "ExportStatus",
    "ExportTrack",
    "ExportValidationError",
    "PART_SUFFIX",
    "build_export_plan",
    "copy_sequentially",
]


PART_SUFFIX = ".part"
PREPARE_WORKERS = 4
MAX_READY_FILES = 8
COPY_CHUNK_BYTES = 1024 * 1024


def _identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _name_key(name: str) -> str:
    return name.casefold()


class ExportValidationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        required_bytes: int | None = None,
        available_bytes: int | None = None,
        collisions: Sequence[str] = (),
        missing_analysis: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes
        self.collisions = tuple(collisions)
        self.missing_analysis = tuple(missing_analysis)


@dataclass(frozen=True, slots=True)
class ExportTrack:
    source: Path
    filename: str
    size_bytes: int
    file_identity: tuple[int, int, int, int]
    source_key: str | None = None
    measurement: LoudnessMeasurement | None = None
    gain_db: float | None = None  # None = plain byte copy


@dataclass(frozen=True, slots=True)
class ExportPlan:
    destination: Path
    tracks: tuple[ExportTrack, ...]
    total_bytes: int
    target_lufs: float | None = None
    ffmpeg_path: str | None = None
    state_cache: LoudnessCache | None = None

    @property
    def normalized_count(self) -> int:
        return sum(1 for track in self.tracks if track.gain_db is not None)


class ExportStatus(Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ExportProgress:
    completed_count: int
    total_count: int
    current_filename: str
    copied_bytes: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class ExportResult:
    status: ExportStatus
    completed_count: int
    total_count: int
    copied_bytes: int
    failed_filename: str | None = None
    reason: str | None = None


def build_export_plan(
    tracks: Sequence[Track],
    destination: str | Path,
    *,
    available_bytes: int | None = None,
    target_lufs: float | None = None,
    state_cache: LoudnessCache | None = None,
    analysis_failures: Mapping[str, str] | None = None,
    ffmpeg_path: str | None = None,
) -> ExportPlan:
    """Validate sources, loudness data, space, and collisions before any write.

    With ``target_lufs`` set, every track must have a cached measurement; a
    track that is missing one blocks the export (it is never copied silently
    un-normalized). Without ``target_lufs`` every track is a plain copy.
    """

    root = Path(destination)
    if not root.exists() or not root.is_dir():
        raise ExportValidationError(
            "The selected USB drive is unavailable or has been disconnected."
        )
    if not os.access(root, os.W_OK):
        raise ExportValidationError(
            "The selected USB drive is not writable with the current permissions."
        )
    if not tracks:
        raise ExportValidationError("No tracks are loaded for export.")
    if target_lufs is not None and state_cache is None:
        raise ValueError("A loudness cache is required when target_lufs is set.")

    snapshots: list[ExportTrack] = []
    target_names: set[str] = set()
    missing: list[str] = []
    for track in tracks:
        source = track.original_path
        try:
            current_identity = _identity(source)
        except OSError as exc:
            raise ExportValidationError(
                f'Source file no longer exists or cannot be read:\n{source}\n\n'
                "Reload the folder and try again."
            ) from exc
        if not source.is_file():
            raise ExportValidationError(
                f'Source is no longer a regular file:\n{source}\n\nReload the folder and try again.'
            )
        if not os.access(source, os.R_OK):
            raise ExportValidationError(f'Source file cannot be read:\n{source}')
        if track.file_identity is not None and current_identity != track.file_identity:
            raise ExportValidationError(
                f'Source file changed outside the application:\n{source}\n\n'
                "Reload the folder and try again."
            )
        target_key = _name_key(track.original_filename)
        if target_key in target_names:
            raise ExportValidationError(
                f'Duplicate destination filename in export list: "{track.original_filename}"'
            )
        target_names.add(target_key)

        source_key: str | None = None
        measurement: LoudnessMeasurement | None = None
        gain: float | None = None
        if target_lufs is not None:
            assert state_cache is not None
            try:
                source_key = fingerprint_file(source)
            except OSError as exc:
                raise ExportValidationError(
                    f"Source file cannot be read:\n{source}"
                ) from exc
            measurement = state_cache.get(source_key)
            if measurement is None:
                reason = (analysis_failures or {}).get(source_key, "not analyzed yet")
                missing.append(f"{track.original_filename} — {reason}")
            else:
                gain = compute_gain_db(measurement, target_lufs)
        snapshots.append(
            ExportTrack(
                source=source,
                filename=track.original_filename,
                size_bytes=current_identity[2],
                file_identity=current_identity,
                source_key=source_key,
                measurement=measurement,
                gain_db=gain,
            )
        )

    if missing:
        shown = "\n".join(missing[:15])
        if len(missing) > 15:
            shown += f"\n… and {len(missing) - 15} more"
        raise ExportValidationError(
            f"{len(missing)} track(s) have no loudness analysis, so they cannot be "
            "copied at the target loudness. Nothing was copied.\n\n"
            f"{shown}\n\n"
            "Wait for analysis to finish, or replace/remove the failing files and "
            "reload the folder.",
            missing_analysis=missing,
        )

    executable = ffmpeg_path
    if any(snapshot.gain_db is not None for snapshot in snapshots):
        executable = executable or discover_ffmpeg()
        if not executable:
            raise ExportValidationError(
                "FFmpeg was not found, so loudness gain cannot be applied."
            )

    try:
        existing = {_name_key(entry.name): entry.name for entry in root.iterdir()}
    except OSError as exc:
        raise ExportValidationError(
            "The selected USB drive could not be inspected. It may have been disconnected."
        ) from exc
    collisions: list[str] = []
    for snapshot in snapshots:
        for name in (snapshot.filename, snapshot.filename + PART_SUFFIX):
            if _name_key(name) in existing:
                collisions.append(existing[_name_key(name)])
    if collisions:
        shown = ", ".join(collisions[:10])
        if len(collisions) > 10:
            shown += f", and {len(collisions) - 10} more"
        raise ExportValidationError(
            f"Destination files already exist: {shown}", collisions=collisions
        )

    total_bytes = sum(snapshot.size_bytes for snapshot in snapshots)
    if available_bytes is None:
        try:
            available_bytes = shutil.disk_usage(root).free
        except OSError as exc:
            raise ExportValidationError(
                "Unable to read free space from the selected USB drive."
            ) from exc
    if total_bytes > available_bytes:
        raise ExportValidationError(
            "Not enough free space on the USB drive.",
            required_bytes=total_bytes,
            available_bytes=available_bytes,
        )
    return ExportPlan(
        root,
        tuple(snapshots),
        total_bytes,
        target_lufs,
        executable,
        state_cache if target_lufs is not None else None,
    )


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


class _Cancelled(Exception):
    pass


class _LinkedToken:
    """Cancelled when either the user or the export loop asks to stop."""

    def __init__(self, outer: CancellationToken) -> None:
        self._outer = outer
        self._inner = CancellationToken()

    def cancel(self) -> None:
        self._inner.cancel()

    @property
    def cancelled(self) -> bool:
        return self._outer.cancelled or self._inner.cancelled


@dataclass(frozen=True, slots=True)
class _Prepared:
    path: Path
    size_bytes: int
    output_key: str | None
    output_measurement: LoudnessMeasurement | None
    temporary: bool


def _prepare(
    plan: ExportPlan,
    index: int,
    snapshot: ExportTrack,
    temp_dir: Path | None,
    cancellation: _LinkedToken,
    run: CommandRunner,
) -> _Prepared:
    if snapshot.gain_db is None:
        output_measurement = snapshot.measurement
        return _Prepared(
            snapshot.source,
            snapshot.size_bytes,
            snapshot.source_key,
            output_measurement,
            temporary=False,
        )
    assert temp_dir is not None and plan.ffmpeg_path is not None
    assert snapshot.measurement is not None
    if cancellation.cancelled:
        raise _Cancelled
    if _identity(snapshot.source) != snapshot.file_identity:
        raise OSError("The source file changed or disappeared during export.")
    output = temp_dir / f"{index:05d}{snapshot.source.suffix.lower()}"
    try:
        result = run(
            gain_command(plan.ffmpeg_path, snapshot.source, output, snapshot.gain_db),
            cancellation,  # type: ignore[arg-type]
        )
        if result.cancelled or cancellation.cancelled:
            raise _Cancelled
        if result.returncode != 0:
            raise RuntimeError(
                "Applying loudness gain failed: "
                + failure_reason(result, "FFmpeg reported an error.")
            )
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("FFmpeg did not create a valid output file.")
        if _identity(snapshot.source) != snapshot.file_identity:
            raise OSError("The source file changed while loudness gain was applied.")
        measured = snapshot.measurement
        output_measurement = LoudnessMeasurement(
            measured.integrated_lufs + snapshot.gain_db,
            measured.true_peak_dbtp + snapshot.gain_db,
        )
        return _Prepared(
            output,
            output.stat().st_size,
            fingerprint_file(output),
            output_measurement,
            temporary=True,
        )
    except BaseException:
        _unlink_quietly(output)
        raise


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _make_default_copy(
    cancellation: CancellationToken,
) -> Callable[[Path, Path], None]:
    def copy(source: Path, target: Path) -> None:
        """Exclusive-create copy that stops between chunks when cancelled."""

        with source.open("rb") as source_stream, target.open("xb") as target_stream:
            while True:
                if cancellation.cancelled:
                    raise _Cancelled
                chunk = source_stream.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                target_stream.write(chunk)
            target_stream.flush()
            os.fsync(target_stream.fileno())

    return copy


def copy_sequentially(
    plan: ExportPlan,
    cancellation: CancellationToken,
    *,
    progress: Callable[[ExportProgress], None] | None = None,
    copy_file: Callable[[Path, Path], None] | None = None,
    command_runner: CommandRunner | None = None,
    temp_root: str | Path | None = None,
    max_workers: int = PREPARE_WORKERS,
    max_ready: int = MAX_READY_FILES,
) -> ExportResult:
    """Write one complete file at a time in the immutable snapshot order."""

    copy_operation = copy_file or _make_default_copy(cancellation)
    run = command_runner or run_command
    completed = 0
    copied_bytes = 0
    total_count = len(plan.tracks)
    prep_token = _LinkedToken(cancellation)
    temp_dir: Path | None = None
    if any(track.gain_db is not None for track in plan.tracks):
        temp_dir = Path(
            tempfile.mkdtemp(prefix="usb_track_order_export_", dir=temp_root)
        )
    executor = ThreadPoolExecutor(
        max_workers=max(1, max_workers), thread_name_prefix="export-prepare"
    )
    futures: dict[int, Future[_Prepared]] = {}
    next_submit = 0

    def top_up(current: int) -> None:
        nonlocal next_submit
        while next_submit < total_count and next_submit < current + max(1, max_ready):
            futures[next_submit] = executor.submit(
                _prepare,
                plan,
                next_submit,
                plan.tracks[next_submit],
                temp_dir,
                prep_token,
                run,
            )
            next_submit += 1

    def await_prepared(index: int) -> _Prepared:
        future = futures.pop(index)
        while True:
            if cancellation.cancelled:
                raise _Cancelled
            try:
                return future.result(timeout=0.1)
            except FutureTimeoutError:
                continue

    def failed(snapshot: ExportTrack, reason: str) -> ExportResult:
        return ExportResult(
            ExportStatus.FAILED,
            completed,
            total_count,
            copied_bytes,
            snapshot.filename,
            reason,
        )

    def cancelled() -> ExportResult:
        return ExportResult(ExportStatus.CANCELLED, completed, total_count, copied_bytes)

    try:
        for index, snapshot in enumerate(plan.tracks):
            if cancellation.cancelled:
                return cancelled()
            top_up(index)
            if not plan.destination.exists() or not plan.destination.is_dir():
                return failed(
                    snapshot,
                    "The USB destination folder is unavailable. The drive may have been disconnected.",
                )
            try:
                if _identity(snapshot.source) != snapshot.file_identity:
                    raise OSError("The source file changed or disappeared during export.")
            except OSError:
                return failed(
                    snapshot,
                    "The source file changed, disappeared, or can no longer be read.",
                )

            target = plan.destination / snapshot.filename
            part = plan.destination / (snapshot.filename + PART_SUFFIX)
            if target.exists() or part.exists():
                return failed(
                    snapshot,
                    "A destination file appeared after export validation; it was not overwritten.",
                )
            if progress:
                progress(
                    ExportProgress(
                        completed,
                        total_count,
                        snapshot.filename,
                        copied_bytes,
                        plan.total_bytes,
                    )
                )
            try:
                prepared = await_prepared(index)
            except _Cancelled:
                return cancelled()
            except Exception as exc:
                return failed(snapshot, str(exc) or "The file could not be prepared.")

            try:
                free = shutil.disk_usage(plan.destination).free
            except OSError:
                free = None
            if free is not None and prepared.size_bytes > free:
                return failed(
                    snapshot,
                    "Not enough free space on the USB drive for this file "
                    f"({prepared.size_bytes:,} bytes needed, {free:,} available).",
                )

            part_created = False
            try:
                part_created = True
                copy_operation(prepared.path, part)
                if _identity(snapshot.source) != snapshot.file_identity:
                    raise OSError("The source file changed while it was being copied.")
                if not part.is_file() or part.stat().st_size != prepared.size_bytes:
                    raise OSError(
                        "Destination file size does not match the source file size."
                    )
                if target.exists():
                    raise OSError(
                        "A destination file appeared during export; it was not overwritten."
                    )
                os.rename(part, target)
                part_created = False
            except _Cancelled:
                _unlink_quietly(part)
                return cancelled()
            except Exception as exc:
                if part_created:
                    _unlink_quietly(part)
                reason = (
                    "The USB destination folder is unavailable. The drive may have been disconnected."
                    if not plan.destination.exists()
                    else str(exc) or "The file could not be copied."
                )
                return failed(snapshot, reason)
            finally:
                if prepared.temporary:
                    _unlink_quietly(prepared.path)

            if (
                plan.state_cache is not None
                and prepared.output_key is not None
                and prepared.output_measurement is not None
            ):
                plan.state_cache.put(prepared.output_key, prepared.output_measurement)

            completed += 1
            copied_bytes += snapshot.size_bytes
            if progress:
                progress(
                    ExportProgress(
                        completed,
                        total_count,
                        snapshot.filename,
                        copied_bytes,
                        plan.total_bytes,
                    )
                )
        return ExportResult(ExportStatus.COMPLETED, completed, total_count, copied_bytes)
    finally:
        prep_token.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
        if plan.state_cache is not None:
            plan.state_cache.save()
