"""Qt worker that measures loudness off the UI thread (sources are never modified)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from app.services.loudness import (
    AnalysisProgress,
    CancellationToken,
    LoudnessCache,
    analyze_loudness,
    plan_analysis,
)


class LoudnessAnalysisWorker(QObject):
    progress = Signal(object)
    finished = Signal(object)

    def __init__(
        self, paths: Sequence[Path], cache: LoudnessCache, ffmpeg_path: str
    ) -> None:
        super().__init__()
        self.paths = tuple(paths)
        self.cache = cache
        self.ffmpeg_path = ffmpeg_path
        self.cancellation = CancellationToken()

    def request_cancel(self) -> None:
        self.cancellation.cancel()

    @Slot()
    def run(self) -> None:
        jobs, cached, failures = plan_analysis(self.paths, self.cache)
        result = analyze_loudness(
            jobs,
            self.cache,
            self.ffmpeg_path,
            self.cancellation,
            cached_count=cached,
            initial_failures=failures,
            progress=self._emit_progress,
        )
        self.finished.emit(result)

    def _emit_progress(self, update: AnalysisProgress) -> None:
        self.progress.emit(update)
