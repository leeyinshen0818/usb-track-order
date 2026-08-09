"""Main window for local track ordering and confirmed safe renaming."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QStandardPaths, QThread, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.services.file_scanner import FileScanError, scan_folder
from app.services.filename_numbering import (
    build_apply_numbering_plan,
    build_remove_numbering_plan,
    refresh_tracks_from_receipt,
)
from app.services.safe_renamer import (
    RenameAction,
    RenameExecutionError,
    RenameReceipt,
    RenameValidationError,
    SafeRenamer,
)
from app.services.track_sorting import (
    SortField,
    SortState,
    next_sort_state,
    sort_tracks,
)
from app.services.usb_drives import (
    UsbDestinationError,
    UsbDrive,
    discover_removable_drives,
    format_bytes,
    validate_usb_destination,
)
from app.services.usb_exporter import (
    ExportProgress,
    ExportPlan,
    ExportResult,
    ExportStatus,
    ExportValidationError,
    build_export_plan,
)
from app.widgets.rename_preview import RenamePreviewDialog
from app.widgets.track_list import (
    FILENAME_COLUMN,
    MODIFIED_COLUMN,
    SIZE_COLUMN,
    TYPE_COLUMN,
    TrackListWidget,
)
from app.workers.usb_export_worker import UsbExportWorker

SORT_COLUMNS = {
    FILENAME_COLUMN: SortField.FILENAME,
    MODIFIED_COLUMN: SortField.MODIFIED,
    SIZE_COLUMN: SortField.SIZE,
    TYPE_COLUMN: SortField.TYPE,
}


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("USB Track Order")
        self.resize(1020, 760)
        self.setMinimumSize(760, 600)
        self.safe_renamer = SafeRenamer()
        self.last_rename: RenameReceipt | None = None
        self.sort_state = SortState.custom()
        self.export_running = False
        self.export_thread: QThread | None = None
        self.export_worker: UsbExportWorker | None = None
        self.pending_export_result: ExportResult | None = None
        self.usb_destination_path: Path | None = None
        self.usb_destination_drive_id: tuple[str, int | str, int] | None = None

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        heading = QLabel("USB Track Order")
        heading.setStyleSheet("font-size: 22px; font-weight: 600;")
        layout.addWidget(heading)
        layout.addWidget(QLabel("Local Folder"))

        folder_row = QHBoxLayout()
        self.folder_path = QLineEdit()
        self.folder_path.setReadOnly(True)
        self.folder_path.setPlaceholderText("Choose a folder containing music files")
        self.open_button = QPushButton("Open Folder")
        self.open_button.clicked.connect(self.open_folder)
        folder_row.addWidget(self.folder_path, 1)
        folder_row.addWidget(self.open_button)
        layout.addLayout(folder_row)

        library_status = QHBoxLayout()
        self.count_label = QLabel("0 songs")
        self.sort_label = QLabel("Sort: Custom Order")
        self.sort_label.setStyleSheet("color: palette(mid);")
        library_status.addWidget(self.count_label)
        library_status.addStretch()
        library_status.addWidget(self.sort_label)
        layout.addLayout(library_status)

        controls = QHBoxLayout()
        self.move_top_button = QPushButton("Move Top")
        self.move_up_button = QPushButton("Move Up")
        self.move_down_button = QPushButton("Move Down")
        self.move_bottom_button = QPushButton("Move Bottom")
        for button in (
            self.move_top_button,
            self.move_up_button,
            self.move_down_button,
            self.move_bottom_button,
        ):
            controls.addWidget(button)
        controls.addStretch()
        layout.addLayout(controls)

        rename_controls = QHBoxLayout()
        self.apply_numbering_button = QPushButton("Apply Numbering")
        self.remove_numbering_button = QPushButton("Remove Numbering")
        self.undo_rename_button = QPushButton("Undo Last Rename")
        rename_controls.addWidget(self.apply_numbering_button)
        rename_controls.addWidget(self.remove_numbering_button)
        rename_controls.addWidget(self.undo_rename_button)
        rename_controls.addStretch()
        layout.addLayout(rename_controls)

        self.track_list = TrackListWidget()
        layout.addWidget(self.track_list, 1)

        usb_heading = QLabel("USB Destination")
        usb_heading.setStyleSheet("font-weight: 600;")
        layout.addWidget(usb_heading)

        usb_row = QHBoxLayout()
        self.usb_combo = QComboBox()
        self.usb_refresh_button = QPushButton("Refresh")
        usb_row.addWidget(QLabel("USB Drive:"))
        usb_row.addWidget(self.usb_combo, 1)
        usb_row.addWidget(self.usb_refresh_button)
        layout.addLayout(usb_row)
        self.usb_capacity_label = QLabel("No removable USB drive detected.")
        self.usb_capacity_label.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.usb_capacity_label)

        destination_row = QHBoxLayout()
        self.usb_destination_field = QLineEdit()
        self.usb_destination_field.setReadOnly(True)
        self.usb_destination_field.setPlaceholderText("Select a removable USB drive")
        self.choose_usb_folder_button = QPushButton("Choose Folder")
        destination_row.addWidget(QLabel("Destination Folder:"))
        destination_row.addWidget(self.usb_destination_field, 1)
        destination_row.addWidget(self.choose_usb_folder_button)
        layout.addLayout(destination_row)

        export_controls = QHBoxLayout()
        self.copy_to_usb_button = QPushButton("Copy to USB")
        self.cancel_copy_button = QPushButton("Cancel Copy")
        export_controls.addWidget(self.copy_to_usb_button)
        export_controls.addWidget(self.cancel_copy_button)
        export_controls.addStretch()
        layout.addLayout(export_controls)

        self.export_progress = QProgressBar()
        self.export_progress.setRange(0, 100)
        self.export_progress.setValue(0)
        self.export_count_label = QLabel("0 / 0 tracks")
        self.export_current_label = QLabel("Current: —")
        self.export_current_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.export_progress)
        layout.addWidget(self.export_count_label)
        layout.addWidget(self.export_current_label)

        safety_note = QLabel(
            "Ordering is held in memory. Files are renamed only after confirmation."
        )
        safety_note.setStyleSheet("color: palette(mid);")
        layout.addWidget(safety_note)
        self.setCentralWidget(central)

        self.move_top_button.clicked.connect(self.track_list.move_selected_top)
        self.move_up_button.clicked.connect(self.track_list.move_selected_up)
        self.move_down_button.clicked.connect(self.track_list.move_selected_down)
        self.move_bottom_button.clicked.connect(self.track_list.move_selected_bottom)
        self.apply_numbering_button.clicked.connect(self.apply_numbering)
        self.remove_numbering_button.clicked.connect(self.remove_numbering)
        self.undo_rename_button.clicked.connect(self.undo_last_rename)
        self.track_list.selection_changed.connect(self._update_controls)
        self.track_list.sort_requested.connect(self.sort_by_column)
        self.track_list.manual_order_changed.connect(self.mark_custom_order)
        self.usb_refresh_button.clicked.connect(self.refresh_usb_drives)
        self.usb_combo.currentIndexChanged.connect(self._usb_selection_changed)
        self.choose_usb_folder_button.clicked.connect(self.choose_usb_folder)
        self.copy_to_usb_button.clicked.connect(self.copy_to_usb)
        self.cancel_copy_button.clicked.connect(self.cancel_export)
        self.refresh_usb_drives()
        self._update_controls()

    def open_folder(self) -> None:
        start = self.folder_path.text() or QStandardPaths.writableLocation(
            QStandardPaths.MusicLocation
        )
        selected = QFileDialog.getExistingDirectory(
            self,
            "Select Music Folder",
            start,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks,
        )
        if selected:
            self.load_folder(selected)

    def load_folder(self, folder: str | Path) -> None:
        try:
            tracks = scan_folder(folder)
        except FileScanError as exc:
            QMessageBox.warning(self, "Unable to Open Folder", str(exc))
            return

        self.folder_path.setText(str(folder))
        self.folder_path.setToolTip(str(folder))
        self.track_list.set_tracks(
            tracks, "No supported audio files found in this folder."
        )
        self.last_rename = None
        self.sort_state = SortState.custom()
        self.sort_label.setText("Sort: Custom Order")
        self.track_list.show_sort_indicator(None, None)
        count = len(tracks)
        self.count_label.setText(f"{count} song" if count == 1 else f"{count} songs")
        self._update_controls()

    def _update_controls(self) -> None:
        rows = self.track_list.selected_rows()
        track_count = len(self.track_list.tracks)
        selected = set(rows)
        can_move_up = any(row > 0 and row - 1 not in selected for row in rows)
        can_move_down = any(
            row < track_count - 1 and row + 1 not in selected for row in rows
        )
        already_at_top = rows == list(range(len(rows)))
        bottom_start = track_count - len(rows)
        already_at_bottom = rows == list(range(bottom_start, track_count))
        available = not self.export_running
        self.open_button.setEnabled(available)
        self.move_top_button.setEnabled(available and bool(rows) and not already_at_top)
        self.move_up_button.setEnabled(available and can_move_up)
        self.move_down_button.setEnabled(available and can_move_down)
        self.move_bottom_button.setEnabled(available and bool(rows) and not already_at_bottom)
        self.apply_numbering_button.setEnabled(available and track_count > 0)
        self.remove_numbering_button.setEnabled(available and track_count > 0)
        self.undo_rename_button.setEnabled(
            available
            and self.last_rename is not None
            and bool(self.last_rename.actions)
        )
        self.usb_combo.setEnabled(available and self.usb_combo.currentData() is not None)
        self.usb_refresh_button.setEnabled(available)
        self.choose_usb_folder_button.setEnabled(
            available and self.usb_combo.currentData() is not None
        )
        self.copy_to_usb_button.setEnabled(
            available
            and track_count > 0
            and self.usb_combo.currentData() is not None
            and self.usb_destination_path is not None
        )
        self.cancel_copy_button.setEnabled(self.export_running)
        self.track_list.set_order_changes_enabled(available)

    def sort_by_column(self, column: int) -> None:
        if self.export_running or column not in SORT_COLUMNS:
            return
        self.sort_state = next_sort_state(self.sort_state, SORT_COLUMNS[column])
        assert self.sort_state.field is not None
        assert self.sort_state.direction is not None
        ordered = sort_tracks(
            self.track_list.tracks,
            self.sort_state.field,
            self.sort_state.direction,
        )
        self.track_list.replace_tracks_preserving_selection(ordered)
        self.track_list.show_sort_indicator(column, self.sort_state.direction)
        self.sort_label.setText(f"Sort: {self.sort_state.label}")
        self._update_controls()

    def mark_custom_order(self) -> None:
        if self.export_running:
            return
        self.sort_state = SortState.custom()
        self.sort_label.setText("Sort: Custom Order")
        self.track_list.show_sort_indicator(None, None)

    def apply_numbering(self) -> None:
        tracks = self.track_list.tracks
        actions = build_apply_numbering_plan(tracks)
        if not actions:
            self.statusBar().showMessage("All tracks are already numbered correctly.", 8000)
            return
        if not self._validate_for_preview(actions, "apply numbering"):
            return
        unchanged = len(tracks) - len(actions)
        summary = (
            f"{len(tracks)} tracks loaded. {len(actions)} files will be renamed "
            "to match the current order."
        )
        if unchanged:
            summary += f" {unchanged} files are already correct and will remain unchanged."
        if not RenamePreviewDialog.confirm(
            title="Apply Numbering",
            summary=summary,
            action_text="Apply",
            actions=actions,
            parent=self,
        ):
            return
        receipt = self._execute_rename(actions, "apply numbering")
        if receipt is None:
            return
        self._accept_receipt(receipt)
        message = f"Successfully numbered {len(receipt.actions)} tracks."
        if unchanged:
            message += f" {unchanged} tracks were already correct."
        self.statusBar().showMessage(message, 10000)

    def remove_numbering(self) -> None:
        tracks = self.track_list.tracks
        actions = build_remove_numbering_plan(tracks)
        if not actions:
            self.statusBar().showMessage("No USB Track Order numbering was found.", 8000)
            return
        if not self._validate_for_preview(actions, "remove numbering"):
            return
        unchanged = len(tracks) - len(actions)
        summary = f"{len(tracks)} tracks loaded. {len(actions)} files contain removable numbering."
        if unchanged:
            summary += f" {unchanged} files will remain unchanged."
        if not RenamePreviewDialog.confirm(
            title="Remove Numbering",
            summary=summary,
            action_text="Remove Numbering",
            actions=actions,
            parent=self,
        ):
            return
        receipt = self._execute_rename(actions, "remove numbering")
        if receipt is None:
            return
        self._accept_receipt(receipt)
        message = f"Removed numbering from {len(receipt.actions)} tracks."
        if unchanged:
            message += f" {unchanged} tracks were unchanged."
        self.statusBar().showMessage(message, 10000)

    def undo_last_rename(self) -> None:
        if self.last_rename is None or not self.last_rename.actions:
            return
        actions = self.last_rename.undo_actions()
        if not self._validate_for_preview(actions, "undo the last rename"):
            return
        if not RenamePreviewDialog.confirm(
            title="Undo Last Rename",
            summary=f"{len(actions)} files will be restored to their previous names.",
            action_text="Undo Last Rename",
            actions=actions,
            parent=self,
        ):
            return
        receipt = self._execute_rename(actions, "undo the last rename")
        if receipt is None:
            return
        self.track_list.refresh_tracks(
            refresh_tracks_from_receipt(self.track_list.tracks, receipt)
        )
        self.last_rename = None
        self._update_controls()
        self.statusBar().showMessage(
            "Successfully restored the previous filenames.", 10000
        )

    def _validate_for_preview(self, actions: list[RenameAction], operation: str) -> bool:
        try:
            self.safe_renamer.validate(actions)
        except RenameValidationError as exc:
            QMessageBox.warning(
                self,
                "Rename Not Safe",
                f"Cannot {operation}.\n\n{exc}\n\nNo files were changed.",
            )
            return False
        return True

    def _execute_rename(
        self, actions: list[RenameAction] | tuple[RenameAction, ...], operation: str
    ) -> RenameReceipt | None:
        try:
            return self.safe_renamer.execute(actions)
        except RenameValidationError as exc:
            QMessageBox.warning(
                self,
                "Rename Not Safe",
                f"Cannot {operation}.\n\n{exc}\n\nNo files were changed.",
            )
        except RenameExecutionError as exc:
            if exc.rollback_complete:
                QMessageBox.critical(
                    self,
                    "Rename Failed",
                    f"Unable to {operation}.\n\n{exc}\n\n"
                    "Check that the files are not open and that the folder is writable.",
                )
            else:
                paths = "\n".join(str(path) for path in exc.manual_attention)
                QMessageBox.critical(
                    self,
                    "Rename Failed — Manual Attention Required",
                    f"{exc}\n\nPotentially affected paths:\n{paths}",
                )
        return None

    def _accept_receipt(self, receipt: RenameReceipt) -> None:
        self.track_list.refresh_tracks(
            refresh_tracks_from_receipt(self.track_list.tracks, receipt)
        )
        self.last_rename = receipt
        self._update_controls()

    def refresh_usb_drives(self) -> None:
        if self.export_running:
            return
        previous_root = None
        current = self.usb_combo.currentData()
        if isinstance(current, UsbDrive):
            previous_root = current.root

        self.usb_combo.blockSignals(True)
        self.usb_combo.clear()
        drives = discover_removable_drives()
        for drive in drives:
            self.usb_combo.addItem(drive.display_name, drive)
        if not drives:
            self.usb_combo.addItem("No removable USB drive detected.", None)
        elif previous_root is not None:
            for index in range(self.usb_combo.count()):
                drive = self.usb_combo.itemData(index)
                if isinstance(drive, UsbDrive) and drive.root == previous_root:
                    self.usb_combo.setCurrentIndex(index)
                    break
        self.usb_combo.blockSignals(False)
        self._usb_selection_changed()

    def _usb_selection_changed(self, _index: int = -1) -> None:
        drive = self.usb_combo.currentData()
        if isinstance(drive, UsbDrive):
            self.usb_capacity_label.setText(drive.capacity_text)
            if self.usb_destination_drive_id != drive.identity_key:
                self._set_usb_destination(drive.root, drive)
            elif self.usb_destination_path is not None:
                try:
                    destination = validate_usb_destination(
                        drive.root, self.usb_destination_path
                    )
                except UsbDestinationError:
                    self._set_usb_destination(drive.root, drive)
                else:
                    self._set_usb_destination(destination, drive)
        else:
            self.usb_capacity_label.setText("No removable USB drive detected.")
            self.usb_destination_path = None
            self.usb_destination_drive_id = None
            self.usb_destination_field.clear()
            self.usb_destination_field.setToolTip("")
        self._update_controls()

    def _set_usb_destination(self, destination: str | Path, drive: UsbDrive) -> None:
        path = Path(destination)
        self.usb_destination_path = path
        self.usb_destination_drive_id = drive.identity_key
        self.usb_destination_field.setText(str(path))
        self.usb_destination_field.setToolTip(str(path))

    def choose_usb_folder(self) -> None:
        if self.export_running:
            return
        drive = self.usb_combo.currentData()
        if not isinstance(drive, UsbDrive):
            return
        start = self.usb_destination_path or drive.root
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose Destination Folder on USB Drive",
            str(start),
            QFileDialog.ShowDirsOnly,
        )
        if not selected:
            return
        try:
            destination = validate_usb_destination(drive.root, selected)
        except UsbDestinationError as exc:
            QMessageBox.warning(self, "Invalid USB Destination", str(exc))
            return
        self._set_usb_destination(destination, drive)
        self._update_controls()

    def copy_to_usb(self) -> None:
        if self.export_running:
            return
        drive = self.usb_combo.currentData()
        if not isinstance(drive, UsbDrive):
            return
        try:
            destination = validate_usb_destination(
                drive.root, self.usb_destination_path or drive.root
            )
            plan = build_export_plan(self.track_list.tracks, destination)
        except UsbDestinationError as exc:
            QMessageBox.warning(self, "Cannot Start USB Export", str(exc))
            return
        except ExportValidationError as exc:
            self._show_export_validation_error(exc)
            return

        confirmation = QMessageBox(self)
        confirmation.setWindowTitle("Copy to USB")
        confirmation.setIcon(QMessageBox.Question)
        confirmation.setText(f"Export {len(plan.tracks)} tracks to {drive.display_name}?")
        confirmation.setInformativeText(
            f"Destination folder:\n{plan.destination}\n\n"
            f"Total size: {format_bytes(plan.total_bytes)}\n\n"
            "Files will be copied sequentially in the exact order shown in the list."
        )
        confirmation.addButton(QMessageBox.Cancel)
        start_button = confirmation.addButton("Start Copy", QMessageBox.AcceptRole)
        confirmation.setDefaultButton(start_button)
        confirmation.exec()
        if confirmation.clickedButton() is not start_button:
            return
        self._start_export(plan)

    def _show_export_validation_error(self, error: ExportValidationError) -> None:
        details = str(error)
        if error.required_bytes is not None and error.available_bytes is not None:
            details += (
                f"\n\nRequired: {format_bytes(error.required_bytes)}"
                f"\nAvailable: {format_bytes(error.available_bytes)}"
            )
        if error.collisions:
            names = "\n".join(error.collisions[:15])
            if len(error.collisions) > 15:
                names += f"\n… and {len(error.collisions) - 15} more"
            details += f"\n\nExisting files:\n{names}"
        QMessageBox.warning(self, "Cannot Start USB Export", details)

    def _start_export(self, plan: ExportPlan) -> None:
        self.export_running = True
        self.pending_export_result = None
        self.export_progress.setValue(0)
        self.export_count_label.setText(f"0 / {len(plan.tracks)} tracks")
        self.export_current_label.setText("Current: Preparing export…")

        thread = QThread(self)
        worker = UsbExportWorker(plan)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._export_progressed)
        worker.finished.connect(self._export_result_ready)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._export_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self.export_thread = thread
        self.export_worker = worker
        self._update_controls()
        thread.start()

    def cancel_export(self) -> None:
        if self.export_worker is None or not self.export_running:
            return
        self.export_worker.request_cancel()
        self.cancel_copy_button.setEnabled(False)
        self.statusBar().showMessage(
            "Cancellation requested. The current file will finish safely.", 10000
        )

    def _export_progressed(self, update: ExportProgress) -> None:
        if update.total_bytes:
            percent = int(update.copied_bytes * 100 / update.total_bytes)
        elif update.total_count:
            percent = int(update.completed_count * 100 / update.total_count)
        else:
            percent = 100
        self.export_progress.setValue(min(100, percent))
        self.export_count_label.setText(
            f"{update.completed_count} / {update.total_count} tracks"
        )
        self.export_current_label.setText(f"Current: {update.current_filename}")

    def _export_result_ready(self, result: ExportResult) -> None:
        self.pending_export_result = result

    def _export_thread_finished(self) -> None:
        result = self.pending_export_result
        self.export_running = False
        self.export_worker = None
        self.export_thread = None
        self._update_controls()
        if result is not None:
            self._show_export_result(result)
        self.refresh_usb_drives()

    def _show_export_result(self, result: ExportResult) -> None:
        if result.status == ExportStatus.COMPLETED:
            self.export_progress.setValue(100)
            self.export_count_label.setText(
                f"{result.completed_count} / {result.total_count} tracks"
            )
            self.export_current_label.setText("Current: Complete")
            self.statusBar().showMessage(
                f"USB export complete. Copied {result.completed_count} tracks.", 15000
            )
            return
        if result.status == ExportStatus.CANCELLED:
            self.export_current_label.setText("Current: Copy cancelled")
            QMessageBox.information(
                self,
                "Copy Cancelled",
                f"Copy cancelled.\n\n{result.completed_count} of "
                f"{result.total_count} tracks were copied.\n\n"
                "Completed files remain on the USB drive.",
            )
            return
        self.export_current_label.setText("Current: Export failed")
        QMessageBox.critical(
            self,
            "USB Export Failed",
            f"Copied: {result.completed_count} / {result.total_count} tracks\n\n"
            f"Failed file: {result.failed_filename or 'Unknown'}\n\n"
            f"Reason: {result.reason or 'Unknown copy error'}\n\n"
            "Already completed files remain on the USB drive.",
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.export_running:
            self.cancel_export()
            QMessageBox.information(
                self,
                "Copy in Progress",
                "Cancellation was requested. Wait for the current file to finish before closing.",
            )
            event.ignore()
            return
        event.accept()
