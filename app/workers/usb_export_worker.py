"""Qt bridge for running the sequential exporter off the GUI thread."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot

from app.services.usb_exporter import (
    CancellationToken,
    ExportPlan,
    ExportProgress,
    ExportResult,
    copy_sequentially,
)


class UsbExportWorker(QObject):
    progress = Signal(object)
    finished = Signal(object)

    def __init__(self, plan: ExportPlan) -> None:
        super().__init__()
        self.plan = plan
        self.cancellation = CancellationToken()

    def request_cancel(self) -> None:
        """Thread-safe request checked between complete file operations."""

        self.cancellation.cancel()

    @Slot()
    def run(self) -> None:
        result = copy_sequentially(
            self.plan,
            self.cancellation,
            progress=self._emit_progress,
        )
        self.finished.emit(result)

    def _emit_progress(self, update: ExportProgress) -> None:
        self.progress.emit(update)
