"""Immutable USB export planning and strictly sequential file copying."""

from __future__ import annotations

import os
import shutil
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app.models.track import Track


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
    ) -> None:
        super().__init__(message)
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes
        self.collisions = tuple(collisions)


@dataclass(frozen=True, slots=True)
class ExportTrack:
    source: Path
    filename: str
    size_bytes: int
    file_identity: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class ExportPlan:
    destination: Path
    tracks: tuple[ExportTrack, ...]
    total_bytes: int


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


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


def build_export_plan(
    tracks: Sequence[Track],
    destination: str | Path,
    *,
    available_bytes: int | None = None,
) -> ExportPlan:
    """Validate all sources, space, and root collisions before any copy starts."""

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

    snapshots: list[ExportTrack] = []
    target_names: set[str] = set()
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
        snapshots.append(
            ExportTrack(
                source=source,
                filename=track.original_filename,
                size_bytes=current_identity[2],
                file_identity=current_identity,
            )
        )

    try:
        existing = {_name_key(entry.name): entry.name for entry in root.iterdir()}
    except OSError as exc:
        raise ExportValidationError(
            "The selected USB drive could not be inspected. It may have been disconnected."
        ) from exc
    collisions = [
        existing[_name_key(snapshot.filename)]
        for snapshot in snapshots
        if _name_key(snapshot.filename) in existing
    ]
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
    return ExportPlan(root, tuple(snapshots), total_bytes)


def _copy_file_no_overwrite(source: Path, target: Path) -> None:
    """Copy bytes with exclusive destination creation to prevent overwrites."""

    with source.open("rb") as source_stream, target.open("xb") as target_stream:
        shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)


def copy_sequentially(
    plan: ExportPlan,
    cancellation: CancellationToken,
    *,
    progress: Callable[[ExportProgress], None] | None = None,
    copy_file: Callable[[Path, Path], None] | None = None,
) -> ExportResult:
    """Copy one complete file at a time in the immutable snapshot order."""

    copy_operation = copy_file or _copy_file_no_overwrite
    completed = 0
    copied_bytes = 0
    total_count = len(plan.tracks)

    for snapshot in plan.tracks:
        if cancellation.cancelled:
            return ExportResult(
                ExportStatus.CANCELLED, completed, total_count, copied_bytes
            )
        if not plan.destination.exists() or not plan.destination.is_dir():
            return ExportResult(
                ExportStatus.FAILED,
                completed,
                total_count,
                copied_bytes,
                snapshot.filename,
                "The USB destination folder is unavailable. The drive may have been disconnected.",
            )
        try:
            if _identity(snapshot.source) != snapshot.file_identity:
                raise OSError("The source file changed or disappeared during export.")
        except OSError:
            return ExportResult(
                ExportStatus.FAILED,
                completed,
                total_count,
                copied_bytes,
                snapshot.filename,
                "The source file changed, disappeared, or can no longer be read.",
            )

        target = plan.destination / snapshot.filename
        if target.exists():
            return ExportResult(
                ExportStatus.FAILED,
                completed,
                total_count,
                copied_bytes,
                snapshot.filename,
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
            copy_operation(snapshot.source, target)
            if _identity(snapshot.source) != snapshot.file_identity:
                raise OSError("The source file changed while it was being copied.")
            if not target.is_file() or target.stat().st_size != snapshot.size_bytes:
                raise OSError("Destination file size does not match the source file size.")
        except Exception as exc:
            reason = (
                "The USB destination folder is unavailable. The drive may have been disconnected."
                if not plan.destination.exists()
                else str(exc) or "The file could not be copied."
            )
            return ExportResult(
                ExportStatus.FAILED,
                completed,
                total_count,
                copied_bytes,
                snapshot.filename,
                reason,
            )

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
