"""Qt worker that keeps FFmpeg loudness processing off the UI thread."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot

from app.services.loudness_sync import (
    CancellationToken,
    LoudnessPlan,
    LoudnessProgress,
    synchronize_loudness,
)


class LoudnessWorker(QObject):
    progress = Signal(object)
    finished = Signal(object)

    def __init__(self, plan: LoudnessPlan) -> None:
        super().__init__()
        self.plan = plan
        self.cancellation = CancellationToken()

    def request_cancel(self) -> None:
        self.cancellation.cancel()

    @Slot()
    def run(self) -> None:
        result = synchronize_loudness(
            self.plan, self.cancellation, progress=self._emit_progress
        )
        self.finished.emit(result)

    def _emit_progress(self, update: LoudnessProgress) -> None:
        self.progress.emit(update)
