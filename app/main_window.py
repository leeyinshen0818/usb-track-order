"""Main window for local track ordering and confirmed safe renaming."""

from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import QDir, QSettings, QSize, QStandardPaths, QThread, QTimer, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app import __version__
from app.paths import data_dir, settings_ini_override
from app.services.file_scanner import (
    FileScanError,
    count_interrupted_renames,
    remove_leftover_temp_files,
    scan_folder,
)
from app.services.rename_journal import (
    clear_receipt,
    load_receipt,
    receipt_belongs_to,
    recover_interrupted_rename,
    save_receipt,
)
from app.services.recycle_bin import move_to_trash
from app.services.track_flags import (
    DuplicateGroup,
    FolderHistory,
    find_duplicate_groups,
    find_new_tracks,
    find_possible_duplicates,
)
from app.services.filename_numbering import (
    build_apply_numbering_plan,
    build_remove_numbering_plan,
    filenames_follow_order,
    refresh_tracks_from_receipt,
)
from app.services.safe_renamer import (
    RenameAction,
    RenameExecutionError,
    RenameReceipt,
    RenameValidationError,
    SafeRenamer,
)
from app.services.loudness import (
    DEFAULT_TARGET_LUFS,
    MAX_TARGET_LUFS,
    MIN_TARGET_LUFS,
    AnalysisProgress,
    AnalysisResult,
    AnalysisStatus,
    LoudnessCache,
    LoudnessValidationError,
    discover_ffmpeg,
    validate_target_lufs,
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
from app.ui.theme import (
    set_primary,
    set_role,
    set_soft_accent,
    text_height,
    text_width,
)
from app.ui.tokens import COLOR, SIZE, SPACE
from app.ui.widgets import ElidedLabel, ResponsiveRow, panel
from app.widgets.duplicate_review import DuplicateReviewDialog
from app.widgets.rename_preview import RenamePreviewDialog
from app.widgets.track_list import (
    FILENAME_COLUMN,
    MODIFIED_COLUMN,
    SIZE_COLUMN,
    TYPE_COLUMN,
    TrackListWidget,
)
from app.workers.usb_export_worker import UsbExportWorker
from app.workers.loudness_worker import LoudnessAnalysisWorker

UNIFY_VOLUME_KEY = "audio/unify_volume"
TARGET_LUFS_KEY = "audio/target_lufs"
FFMPEG_MISSING_REASON = "FFmpeg was not found, so volume cannot be measured"
CONTINUE_UNMEASURED_LABEL = "Continue (these copy at original volume, order unchanged)"


DEFAULT_STATUS_NOTE = (
    "Ordering is held in memory. Files are renamed only after confirmation."
)
ORDER_CHANGED_NOTE = "Order changed. Click Apply Numbering to rename the files."
DUPLICATES_LINK = "duplicates"
NO_RECYCLE_BIN_MESSAGE = "Please copy the songs to the computer first, then delete."


def _setting_is_true(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def unmeasured_summary(entries: list[str] | tuple[str, ...], limit: int = 15) -> str:
    """Dialog body listing tracks that will be copied at original volume."""

    shown = "\n".join(entries[:limit])
    if len(entries) > limit:
        shown += f"\n… and {len(entries) - limit} more"
    return f"{len(entries)} track(s) could not be analyzed:\n\n{shown}"


def default_settings() -> QSettings:
    """Registry settings, or an INI file in the overridden data directory."""

    ini = settings_ini_override()
    if ini is not None:
        return QSettings(str(ini), QSettings.IniFormat)
    return QSettings("USB Track Order", "USB Track Order")


SORT_COLUMNS = {
    FILENAME_COLUMN: SortField.FILENAME,
    MODIFIED_COLUMN: SortField.MODIFIED,
    SIZE_COLUMN: SortField.SIZE,
    TYPE_COLUMN: SortField.TYPE,
}


class MainWindow(QMainWindow):
    def __init__(
        self,
        settings: QSettings | None = None,
        loudness_cache: LoudnessCache | None = None,
        ffmpeg_path: str | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings or default_settings()
        self.loudness_cache = (
            loudness_cache if loudness_cache is not None else LoudnessCache()
        )
        # FFmpeg is located lazily, only when a unified-volume copy starts.
        self.ffmpeg_path = ffmpeg_path
        self.setWindowTitle(f"USB Track Order v{__version__}")
        state_dir = data_dir()
        self.rename_journal_path = state_dir / "rename_journal.json"
        self.undo_receipt_path = state_dir / "last_rename.json"
        self.folder_history = FolderHistory(state_dir / "folder_history.json")
        self.previous_folder_names: set[str] | None = None
        self.safe_renamer = SafeRenamer(journal_path=self.rename_journal_path)
        self.last_rename: RenameReceipt | None = None
        self.sort_state = SortState.custom()
        self.export_running = False
        self.export_thread: QThread | None = None
        self.export_worker: UsbExportWorker | None = None
        self.pending_export_result: ExportResult | None = None
        self.analysis_running = False
        self.analysis_thread: QThread | None = None
        self.analysis_worker: LoudnessAnalysisWorker | None = None
        self.pending_analysis_result: AnalysisResult | None = None
        self.pending_copy_plan: ExportPlan | None = None
        self.usb_destination_path: Path | None = None
        self.usb_destination_drive_id: tuple[str, int | str, int] | None = None

        page = QWidget()
        page.setObjectName("pageContent")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(SPACE.md, SPACE.md, SPACE.md, SPACE.sm)
        layout.setSpacing(SPACE.sm)

        # --- music folder --------------------------------------------------
        folder_row = QHBoxLayout()
        folder_row.setSpacing(SPACE.sm)
        self.folder_path = QLineEdit()
        self.folder_path.setReadOnly(True)
        self.folder_path.setPlaceholderText("Choose a folder containing music files")
        self.folder_path.setMinimumWidth(text_width(self.folder_path, SIZE.field_min_chars))
        self.open_button = QPushButton("Open Folder")
        self.open_button.clicked.connect(self.open_folder)
        folder_row.addWidget(QLabel("Music Folder:"))
        folder_row.addWidget(self.folder_path, 1)
        folder_row.addWidget(self.open_button)
        layout.addLayout(folder_row)

        library_status = QHBoxLayout()
        library_status.setSpacing(SPACE.md)
        self.count_label = set_role(QLabel("0 songs"), "hint")
        self.notice_label = set_role(QLabel(""), "notice")
        self.notice_label.setTextFormat(Qt.RichText)
        self.notice_label.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.notice_label.linkActivated.connect(self._notice_link_activated)
        self.duplicate_groups: list[DuplicateGroup] = []
        self.sort_label = set_role(QLabel("Sort: Custom Order"), "hint")
        library_status.addWidget(self.count_label)
        library_status.addWidget(self.notice_label)
        library_status.addStretch()
        library_status.addWidget(self.sort_label)
        layout.addLayout(library_status)

        # --- grouped list tools -------------------------------------------
        order_panel, order_layout = panel()
        self.move_top_button = QPushButton("Move Top")
        self.move_up_button = QPushButton("Move Up")
        self.move_down_button = QPushButton("Move Down")
        self.move_bottom_button = QPushButton("Move Bottom")
        order_layout.addWidget(set_role(QLabel("Order"), "caption"))
        for button in (
            self.move_top_button,
            self.move_up_button,
            self.move_down_button,
            self.move_bottom_button,
        ):
            order_layout.addWidget(button)
        order_layout.addStretch()

        numbering_panel, numbering_layout = panel()
        self.apply_numbering_button = QPushButton("Apply Numbering")
        self.remove_numbering_button = QPushButton("Remove Numbering")
        self.undo_rename_button = QPushButton("Undo Last Rename")
        numbering_layout.addWidget(set_role(QLabel("Numbering"), "caption"))
        numbering_layout.addWidget(self.apply_numbering_button)
        numbering_layout.addWidget(self.remove_numbering_button)
        numbering_layout.addWidget(self.undo_rename_button)
        numbering_layout.addStretch()

        self.tools_row = ResponsiveRow(
            [(order_panel, 0), (numbering_panel, 0)], trailing_stretch=True
        )
        layout.addWidget(self.tools_row)

        self.track_list = TrackListWidget()
        layout.addWidget(self.track_list, 1)

        # --- destination and copy (one group, one primary action) ----------
        usb_panel, usb_layout = panel(vertical=True)

        drive_part = QWidget()
        drive_layout = QHBoxLayout(drive_part)
        drive_layout.setContentsMargins(0, 0, 0, 0)
        drive_layout.setSpacing(SPACE.sm)
        self.usb_combo = QComboBox()
        self.usb_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon
        )
        self.usb_combo.setMinimumContentsLength(SIZE.combo_min_chars)
        self.usb_refresh_button = QPushButton("Refresh")
        self.usb_capacity_label = set_role(
            QLabel("No removable USB drive detected."), "hint"
        )
        drive_layout.addWidget(QLabel("USB Drive:"))
        drive_layout.addWidget(self.usb_combo, 1)
        drive_layout.addWidget(self.usb_refresh_button)
        drive_layout.addWidget(self.usb_capacity_label)

        destination_part = QWidget()
        destination_layout = QHBoxLayout(destination_part)
        destination_layout.setContentsMargins(0, 0, 0, 0)
        destination_layout.setSpacing(SPACE.sm)
        self.usb_destination_field = QLineEdit()
        self.usb_destination_field.setReadOnly(True)
        self.usb_destination_field.setPlaceholderText("Select a removable USB drive")
        self.usb_destination_field.setMinimumWidth(
            text_width(self.usb_destination_field, SIZE.field_min_chars)
        )
        self.choose_usb_folder_button = QPushButton("Choose Folder")
        destination_layout.addWidget(QLabel("Destination Folder:"))
        destination_layout.addWidget(self.usb_destination_field, 1)
        destination_layout.addWidget(self.choose_usb_folder_button)

        self.destination_row = ResponsiveRow([(drive_part, 1), (destination_part, 1)])
        usb_layout.addWidget(self.destination_row)

        copy_row = QHBoxLayout()
        copy_row.setSpacing(SPACE.sm)
        self.unify_volume_checkbox = QCheckBox("Unify volume")
        self.unify_volume_checkbox.setToolTip(
            "Measure each track and adjust the volume of the USB copies to one "
            "target loudness. Source files are never modified."
        )
        self.unify_volume_checkbox.setChecked(
            _setting_is_true(self.settings.value(UNIFY_VOLUME_KEY, False))
        )
        self.target_lufs_container = QWidget()
        target_layout = QHBoxLayout(self.target_lufs_container)
        target_layout.setContentsMargins(0, 0, 0, 0)
        target_layout.setSpacing(SPACE.sm)
        self.target_lufs = QDoubleSpinBox()
        self.target_lufs.setRange(MIN_TARGET_LUFS, MAX_TARGET_LUFS)
        self.target_lufs.setDecimals(1)
        self.target_lufs.setSingleStep(0.5)
        try:
            saved_target = validate_target_lufs(
                self.settings.value(TARGET_LUFS_KEY, DEFAULT_TARGET_LUFS)
            )
        except LoudnessValidationError:
            saved_target = DEFAULT_TARGET_LUFS
        self.target_lufs.setValue(saved_target)
        self.target_lufs.setSuffix(" LUFS")
        target_layout.addWidget(QLabel("Target Loudness:"))
        target_layout.addWidget(self.target_lufs)
        self.target_lufs_container.setVisible(self.unify_volume_checkbox.isChecked())
        self.copy_to_usb_button = set_primary(QPushButton("Copy to USB"))
        self.cancel_copy_button = QPushButton("Cancel Copy")
        self.export_progress = QProgressBar()
        self.export_progress.setRange(0, 100)
        self.export_progress.setValue(0)
        self.export_progress.setMinimumWidth(text_width(self.export_progress, 6))
        self.export_count_label = QLabel("0 / 0 tracks")
        self.export_current_label = ElidedLabel("Current: —")
        copy_row.addWidget(self.unify_volume_checkbox)
        copy_row.addWidget(self.target_lufs_container)
        copy_row.addWidget(self.export_progress, 2)
        copy_row.addWidget(self.export_count_label)
        copy_row.addWidget(self.export_current_label, 3)
        copy_row.addWidget(self.cancel_copy_button)
        copy_row.addWidget(self.copy_to_usb_button)
        usb_layout.addLayout(copy_row)
        layout.addWidget(usb_panel)

        # Safety net: on very small screens the page scrolls instead of clipping.
        scroll = QScrollArea()
        scroll.setObjectName("page")
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self.setCentralWidget(scroll)

        self.status_note = ElidedLabel(DEFAULT_STATUS_NOTE)
        self.statusBar().addWidget(self.status_note, 1)
        self._apply_window_size()

        self.move_top_button.clicked.connect(self.track_list.move_selected_top)
        self.move_up_button.clicked.connect(self.track_list.move_selected_up)
        self.move_down_button.clicked.connect(self.track_list.move_selected_down)
        self.move_bottom_button.clicked.connect(self.track_list.move_selected_bottom)
        self.apply_numbering_button.clicked.connect(self.apply_numbering)
        self.remove_numbering_button.clicked.connect(self.remove_numbering)
        self.undo_rename_button.clicked.connect(self.undo_last_rename)
        self.unify_volume_checkbox.toggled.connect(self._unify_volume_toggled)
        self.target_lufs.valueChanged.connect(self._save_target_lufs)
        self.track_list.selection_changed.connect(self._update_controls)
        self.track_list.sort_requested.connect(self.sort_by_column)
        self.track_list.manual_order_changed.connect(self.mark_custom_order)
        self.track_list.remove_requested.connect(self.remove_selected_tracks)
        self.usb_refresh_button.clicked.connect(self.refresh_usb_drives)
        self.usb_combo.currentIndexChanged.connect(self._usb_selection_changed)
        self.choose_usb_folder_button.clicked.connect(self.choose_usb_folder)
        self.copy_to_usb_button.clicked.connect(self.copy_to_usb)
        self.cancel_copy_button.clicked.connect(self.cancel_export)
        self.refresh_usb_drives()
        self._update_controls()
        QTimer.singleShot(0, self.recover_interrupted_rename)

    def _apply_window_size(self) -> None:
        """Size from font metrics, never larger than the screen's free area."""

        line = text_height(self)
        self.setMinimumSize(
            QSize(line * SIZE.window_min_width_lines, line * SIZE.window_min_height_lines)
        )
        wanted = QSize(line * SIZE.window_width_lines, line * SIZE.window_height_lines)
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry().size()
            fraction = SIZE.window_max_screen_fraction
            wanted = wanted.boundedTo(
                QSize(int(available.width() * fraction), int(available.height() * fraction))
            )
        self.resize(wanted)

    def recover_interrupted_rename(self) -> None:
        """Roll back a rename that a crash left half-done, then report it."""

        report = recover_interrupted_rename(self.rename_journal_path)
        if report is None:
            return
        if report.complete:
            QMessageBox.information(self, "Interrupted Rename Restored", report.summary())
        else:
            QMessageBox.warning(self, "Interrupted Rename — Check Files", report.summary())
        if self.folder_path.text():
            self.load_folder(self.folder_path.text())

    def _refresh_track_flags(self) -> None:
        tracks = self.track_list.tracks
        self.duplicate_groups = find_duplicate_groups(tracks)
        duplicates = find_possible_duplicates(tracks, self.duplicate_groups)
        self.track_list.set_flags(
            find_new_tracks(tracks, self.previous_folder_names), duplicates
        )
        notices = []
        interrupted = count_interrupted_renames(tracks)
        if interrupted:
            notices.append(html.escape(f"Interrupted rename detected: {interrupted} files"))
        if duplicates:
            notices.append(
                f'<a href="{DUPLICATES_LINK}" style="color: {COLOR.link};">'
                f"{len(duplicates)} possible duplicates</a>"
            )
        self.notice_label.setText(" · ".join(notices))
        self.notice_label.setToolTip("Click to review duplicates" if duplicates else "")
        self.notice_label.setVisible(bool(notices))

    def _update_count_label(self) -> None:
        count = len(self.track_list.tracks)
        self.count_label.setText(f"{count} song" if count == 1 else f"{count} songs")

    def _notice_link_activated(self, link: str) -> None:
        if link == DUPLICATES_LINK:
            self.review_duplicates()

    def _save_target_lufs(self, value: float) -> None:
        self.settings.setValue(TARGET_LUFS_KEY, float(value))

    def _unify_volume_toggled(self, checked: bool) -> None:
        self.settings.setValue(UNIFY_VOLUME_KEY, bool(checked))
        self.target_lufs_container.setVisible(checked)

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
        if self.export_running:
            return
        remove_leftover_temp_files(folder)
        try:
            tracks = scan_folder(folder)
        except FileScanError as exc:
            QMessageBox.warning(self, "Unable to Open Folder", str(exc))
            return

        shown = QDir.toNativeSeparators(str(folder))
        self.folder_path.setText(shown)
        self.folder_path.setToolTip(shown)
        self.previous_folder_names = self.folder_history.previous_names(folder)
        self.folder_history.record(folder, tracks)
        self.track_list.set_tracks(
            tracks, "No supported audio files found in this folder."
        )
        self._refresh_track_flags()
        saved = load_receipt(self.undo_receipt_path)
        self.last_rename = (
            saved if saved is not None and receipt_belongs_to(saved, folder) else None
        )
        self.sort_state = SortState.custom()
        self.sort_label.setText("Sort: Custom Order")
        self.track_list.show_sort_indicator(None, None)
        self._update_count_label()
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
        # export_running covers the whole Copy operation, including analysis.
        available = not self.export_running
        self.open_button.setEnabled(available)
        self.move_top_button.setEnabled(available and bool(rows) and not already_at_top)
        self.move_up_button.setEnabled(available and can_move_up)
        self.move_down_button.setEnabled(available and can_move_down)
        self.move_bottom_button.setEnabled(available and bool(rows) and not already_at_bottom)
        self.apply_numbering_button.setEnabled(available and track_count > 0)
        order_pending = track_count > 0 and not filenames_follow_order(
            self.track_list.tracks
        )
        set_soft_accent(self.apply_numbering_button, available and order_pending)
        self.status_note.setText(ORDER_CHANGED_NOTE if order_pending else DEFAULT_STATUS_NOTE)
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
        self.unify_volume_checkbox.setEnabled(available)
        self.target_lufs.setEnabled(available)
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
        self._update_controls()

    # --- duplicates and Recycle Bin -----------------------------------------

    def review_duplicates(self) -> None:
        if self.export_running or not self.duplicate_groups:
            return
        paths = DuplicateReviewDialog.review(self.duplicate_groups, self)
        if paths:
            self.move_tracks_to_recycle_bin(paths)

    def remove_selected_tracks(self) -> None:
        if self.export_running:
            return
        self.move_tracks_to_recycle_bin(self.track_list.selected_paths())

    def _confirm_recycle(self, count: int) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("Move to Recycle Bin")
        box.setIcon(QMessageBox.Question)
        box.setText(f"Move {count} song(s) to the Recycle Bin?")
        box.setInformativeText("You can restore them from the Recycle Bin later.")
        move_button = box.addButton("Move to Recycle Bin", QMessageBox.AcceptRole)
        cancel_button = box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(cancel_button)
        box.exec()
        return box.clickedButton() is move_button

    def move_tracks_to_recycle_bin(self, paths: list[Path]) -> None:
        """Recycle (never permanently delete), then drop the rows in memory."""

        if self.export_running or not paths:
            return
        if not self._confirm_recycle(len(paths)):
            return
        moved: list[Path] = []
        failed: Path | None = None
        for path in paths:
            if not move_to_trash(path):
                failed = path
                break
            moved.append(path)
        if failed is not None and not moved:
            QMessageBox.warning(self, "Cannot Move to Recycle Bin", NO_RECYCLE_BIN_MESSAGE)
            return
        if failed is not None:
            QMessageBox.warning(
                self,
                "Cannot Move to Recycle Bin",
                f"{len(moved)} song(s) were moved to the Recycle Bin, but "
                f'"{failed.name}" could not be moved. The remaining songs were not '
                "touched.",
            )
        self.track_list.remove_tracks(moved)
        self._refresh_track_flags()
        self._update_count_label()
        self._update_controls()
        self.statusBar().showMessage(
            f"Moved {len(moved)} song(s) to the Recycle Bin.", 10000
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
        clear_receipt(self.undo_receipt_path)
        self._refresh_track_flags()
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
        save_receipt(self.undo_receipt_path, receipt)
        self._refresh_track_flags()
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
        shown = QDir.toNativeSeparators(str(path))
        self.usb_destination_field.setText(shown)
        self.usb_destination_field.setToolTip(shown)

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
        unify = self.unify_volume_checkbox.isChecked()
        try:
            destination = validate_usb_destination(
                drive.root, self.usb_destination_path or drive.root
            )
            target = validate_target_lufs(self.target_lufs.value()) if unify else None
            # Plain plan: checks sources, collisions, and space; never needs FFmpeg.
            plan = build_export_plan(self.track_list.tracks, destination)
        except (UsbDestinationError, LoudnessValidationError) as exc:
            QMessageBox.warning(self, "Cannot Start USB Export", str(exc))
            return
        except ExportValidationError as exc:
            self._show_export_validation_error(exc)
            return
        # Files keep their names on the USB; a player sorting by name would not
        # follow the list order until Apply Numbering has renamed them.
        if not filenames_follow_order(self.track_list.tracks) and not (
            self._confirm_unapplied_order()
        ):
            return

        details = (
            f"Destination folder:\n{plan.destination}\n\n"
            f"Total size: {format_bytes(plan.total_bytes)}\n\n"
        )
        if target is not None:
            details += (
                f"Unify volume: on, target {target:.1f} LUFS. Tracks not measured "
                "before are analyzed first. Tracks more than ±0.5 LU away are "
                "adjusted on the USB copy only; source files are never modified.\n\n"
            )
        details += "Files will be copied sequentially in the exact order shown in the list."
        confirmation = QMessageBox(self)
        confirmation.setWindowTitle("Copy to USB")
        confirmation.setIcon(QMessageBox.Question)
        confirmation.setText(f"Export {len(plan.tracks)} tracks to {drive.display_name}?")
        confirmation.setInformativeText(details)
        confirmation.addButton(QMessageBox.Cancel)
        start_button = confirmation.addButton("Start Copy", QMessageBox.AcceptRole)
        confirmation.setDefaultButton(start_button)
        confirmation.exec()
        if confirmation.clickedButton() is not start_button:
            return
        if target is None:
            self._start_export(plan)
        else:
            self._start_volume_analysis(plan)

    # --- Unify volume: analysis phase inside Copy to USB -----------------------

    def _start_volume_analysis(self, plain_plan: ExportPlan) -> None:
        """Measure uncached tracks, then continue the copy with gain."""

        ffmpeg = self.ffmpeg_path or discover_ffmpeg()
        if not ffmpeg:
            entries = [
                f"{track.filename} — {FFMPEG_MISSING_REASON}"
                for track in plain_plan.tracks
            ]
            if self._confirm_unmeasured(entries):
                self._start_export(plain_plan)
            return
        self.ffmpeg_path = ffmpeg

        paths = [track.source for track in plain_plan.tracks]
        self.export_running = True
        self.analysis_running = True
        self.pending_analysis_result = None
        self.pending_copy_plan = plain_plan
        self.export_progress.setValue(0)
        self.export_count_label.setText(f"Analyzing volume 0/{len(paths)}")
        self.export_current_label.setText("Current: Checking volume cache…")

        thread = QThread(self)
        worker = LoudnessAnalysisWorker(paths, self.loudness_cache, ffmpeg)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._analysis_progressed)
        worker.finished.connect(self._analysis_result_ready)
        # Direct so closeEvent can wait on the thread without the GUI loop running.
        worker.finished.connect(thread.quit, Qt.DirectConnection)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._analysis_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self.analysis_thread = thread
        self.analysis_worker = worker
        self._update_controls()
        thread.start()

    def _analysis_progressed(self, update: AnalysisProgress) -> None:
        accounted = update.analyzed_count + update.cached_count + update.failed_count
        percent = int(accounted * 100 / update.total_count) if update.total_count else 100
        self.export_progress.setValue(min(100, percent))
        self.export_count_label.setText(
            f"Analyzing volume {accounted}/{update.total_count}"
        )
        self.export_current_label.setText(f"Current: {update.current_filename}")

    def _analysis_result_ready(self, result: AnalysisResult) -> None:
        self.pending_analysis_result = result

    def _analysis_thread_finished(self) -> None:
        result = self.pending_analysis_result
        plain_plan = self.pending_copy_plan
        self.analysis_running = False
        self.analysis_worker = None
        self.analysis_thread = None
        self.pending_analysis_result = None
        self.pending_copy_plan = None
        self.export_running = False
        self._update_controls()
        if (
            result is None
            or plain_plan is None
            or result.status == AnalysisStatus.CANCELLED
        ):
            self.export_progress.setValue(0)
            self.export_count_label.setText("0 / 0 tracks")
            self.export_current_label.setText("Current: Copy cancelled")
            self.statusBar().showMessage(
                "Copy cancelled during volume analysis. Nothing was copied; "
                "finished measurements are kept.",
                10000,
            )
            return
        self._continue_unified_copy(plain_plan, dict(result.failures))

    def _continue_unified_copy(
        self, plain_plan: ExportPlan, failures: dict[str, str]
    ) -> None:
        """Build the gain plan; tracks without a measurement need consent."""

        try:
            plan = build_export_plan(
                self.track_list.tracks,
                plain_plan.destination,
                target_lufs=validate_target_lufs(self.target_lufs.value()),
                state_cache=self.loudness_cache,
                analysis_failures=failures,
                ffmpeg_path=self.ffmpeg_path,
                allow_unmeasured=True,
            )
        except LoudnessValidationError as exc:
            QMessageBox.warning(self, "Cannot Start USB Export", str(exc))
            return
        except ExportValidationError as exc:
            self._show_export_validation_error(exc)
            return
        if plan.unmeasured and not self._confirm_unmeasured(list(plan.unmeasured)):
            self.export_current_label.setText("Current: Copy cancelled")
            return
        self._start_export(plan)

    def _confirm_unmeasured(self, entries: list[str]) -> bool:
        """Ask once whether tracks without a measurement copy at original volume."""

        box = QMessageBox(self)
        box.setWindowTitle("Some Tracks Could Not Be Analyzed")
        box.setIcon(QMessageBox.Warning)
        box.setText(unmeasured_summary(entries))
        box.setInformativeText(
            "If you continue, these tracks are copied at their original volume and "
            "keep their position. All other tracks are adjusted as usual."
        )
        continue_button = box.addButton(CONTINUE_UNMEASURED_LABEL, QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        box.exec()
        return box.clickedButton() is continue_button

    def _confirm_unapplied_order(self) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("Copy to USB")
        box.setIcon(QMessageBox.Warning)
        box.setText("Order has not been applied to filenames. Copy anyway?")
        box.setInformativeText(
            "Files are copied in the list order, but they keep their current "
            "names. Many car stereos play files sorted by name, so they may not "
            "play in this order. Click Apply Numbering first to be sure."
        )
        copy_button = box.addButton("Copy Anyway", QMessageBox.AcceptRole)
        cancel_button = box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(cancel_button)
        box.exec()
        return box.clickedButton() is copy_button

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
        if not self.export_running:
            return
        if self.analysis_running and self.analysis_worker is not None:
            self.analysis_worker.request_cancel()
            self.cancel_copy_button.setEnabled(False)
            self.statusBar().showMessage(
                "Stopping volume analysis. Nothing will be copied.", 10000
            )
            return
        if self.export_worker is None:
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
        if self.analysis_running and self.analysis_worker is not None:
            # Nothing is written during analysis: stop FFmpeg, keep results, close.
            self.pending_copy_plan = None
            self.analysis_worker.request_cancel()
            if self.analysis_thread is not None:
                self.analysis_thread.wait(10000)
            event.accept()
            return
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
