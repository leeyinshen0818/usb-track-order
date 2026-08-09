"""Main window for local track ordering and confirmed safe renaming."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
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
from app.widgets.rename_preview import RenamePreviewDialog
from app.widgets.track_list import TrackListWidget


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("USB Track Order")
        self.resize(820, 580)
        self.setMinimumSize(620, 420)
        self.safe_renamer = SafeRenamer()
        self.last_rename: RenameReceipt | None = None

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

        self.count_label = QLabel("0 songs")
        layout.addWidget(self.count_label)

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
        self.move_top_button.setEnabled(bool(rows) and not already_at_top)
        self.move_up_button.setEnabled(can_move_up)
        self.move_down_button.setEnabled(can_move_down)
        self.move_bottom_button.setEnabled(bool(rows) and not already_at_bottom)
        self.apply_numbering_button.setEnabled(track_count > 0)
        self.remove_numbering_button.setEnabled(track_count > 0)
        self.undo_rename_button.setEnabled(
            self.last_rename is not None and bool(self.last_rename.actions)
        )

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
