"""Table widget with stable multi-selection, multi-row drag/drop, sortable
headers that show their sort state, and a remove request (menu / Delete key)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import datetime

from PySide6.QtCore import (
    QEvent,
    QAbstractTableModel,
    QByteArray,
    QItemSelectionModel,
    QMimeData,
    QModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QDrag, QDropEvent, QFontMetrics, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QMenu,
    QStackedLayout,
    QTableView,
    QWidget,
)

from app.models.track import Track
from app.services.track_ordering import (
    move_bottom,
    move_down,
    move_to_insertion,
    move_top,
    move_up,
    preview_number,
)
from app.services.track_sorting import SortDirection
from app.services.usb_drives import format_bytes
from app.ui.theme import set_role
from app.ui.tokens import COLOR, FONT, SIZE, SPACE

ROWS_MIME_TYPE = "application/x-usb-track-order-rows"
ORDER_COLUMN = 0
FILENAME_COLUMN = 1
MODIFIED_COLUMN = 2
SIZE_COLUMN = 3
TYPE_COLUMN = 4
HEADERS = ("#", "Filename", "Date Modified", "Size", "Type")
DUPLICATE_MARKER = "≈"  # "≈" beside the position number
SORTABLE_COLUMNS = frozenset({FILENAME_COLUMN, MODIFIED_COLUMN, SIZE_COLUMN, TYPE_COLUMN})
SORT_GLYPH_INACTIVE = "\u21c5"  # ⇅
SORT_GLYPH_ASCENDING = "\u25b2"  # ▲
SORT_GLYPH_DESCENDING = "\u25bc"  # ▼
SORT_TOOLTIP = "Click to sort"
REMOVE_ACTION_TEXT = "Move to Recycle Bin"
EMPTY_FOLDER_MESSAGE = "No supported audio files found in this folder."


class TrackTableModel(QAbstractTableModel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tracks: list[Track] = []
        self.new_paths: set = set()
        self.duplicate_reasons: dict = {}

    def set_flags(self, new_paths: set, duplicate_reasons: dict) -> None:
        self.new_paths = set(new_paths)
        self.duplicate_reasons = dict(duplicate_reasons)
        if self.tracks:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self.tracks) - 1, len(HEADERS) - 1),
                [Qt.DisplayRole, Qt.BackgroundRole, Qt.ToolTipRole],
            )

    def is_new(self, row: int) -> bool:
        return self.tracks[row].original_path in self.new_paths

    def duplicate_reason(self, row: int) -> str | None:
        return self.duplicate_reasons.get(self.tracks[row].original_path)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.tracks)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):  # type: ignore[override]
        if not index.isValid() or not 0 <= index.row() < len(self.tracks):
            return None
        if role == Qt.DisplayRole:
            track = self.tracks[index.row()]
            if index.column() == ORDER_COLUMN:
                number = preview_number(index.row() + 1)
                if self.duplicate_reason(index.row()):
                    return f"{number} {DUPLICATE_MARKER}"
                return number
            if index.column() == FILENAME_COLUMN:
                return track.original_filename
            if index.column() == MODIFIED_COLUMN:
                if track.modified_time is None:
                    return "—"
                return datetime.fromtimestamp(track.modified_time).strftime("%d/%m/%Y %H:%M")
            if index.column() == SIZE_COLUMN:
                return "—" if track.size_bytes is None else format_bytes(track.size_bytes)
            if index.column() == TYPE_COLUMN:
                return track.file_type
        if role == Qt.TextAlignmentRole and index.column() in (ORDER_COLUMN, SIZE_COLUMN):
            return int(Qt.AlignCenter)
        if role == Qt.BackgroundRole:
            # Duplicate tint wins over the "new" tint; both stay low-key.
            if self.duplicate_reason(index.row()):
                color = QColor(COLOR.duplicate_row_tint)
                color.setAlphaF(COLOR.duplicate_row_alpha)
                return color
            if self.is_new(index.row()):
                color = QColor(COLOR.new_row_tint)
                color.setAlphaF(COLOR.new_row_alpha)
                return color
        if role == Qt.ToolTipRole and index.column() in (ORDER_COLUMN, FILENAME_COLUMN):
            notes = []
            if index.column() == FILENAME_COLUMN:
                notes.append(str(self.tracks[index.row()].original_path))
            if self.is_new(index.row()):
                notes.append("New: not numbered yet or added since the last load")
            reason = self.duplicate_reason(index.row())
            if reason:
                notes.append(f"Possible duplicate — {reason}")
            return "\n".join(notes) or None
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return HEADERS[section] if 0 <= section < len(HEADERS) else None
        if role == Qt.ToolTipRole and orientation == Qt.Horizontal:
            return SORT_TOOLTIP if section in SORTABLE_COLUMNS else None
        return super().headerData(section, orientation, role)

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        flags = super().flags(index)
        if index.isValid():
            return flags | Qt.ItemIsDragEnabled
        return flags | Qt.ItemIsDropEnabled

    def replace_tracks(self, tracks: Iterable[Track]) -> None:
        self.beginResetModel()
        self.tracks = list(tracks)
        self.endResetModel()


class TrackHeaderView(QHeaderView):
    """Header that marks every sortable column: muted ⇅ when inactive, bright
    ▲/▼ when active. Qt's own sort arrow stays hidden."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Horizontal, parent)
        self.sort_column: int | None = None
        self.sort_order: Qt.SortOrder | None = None
        self.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setSortIndicatorShown(False)
        self.setMouseTracking(True)

    def set_sort_state(self, column: int | None, order: Qt.SortOrder | None) -> None:
        self.sort_column, self.sort_order = column, order
        self.viewport().update()

    def sort_glyph(self, section: int) -> str | None:
        if section not in SORTABLE_COLUMNS:
            return None
        if section != self.sort_column or self.sort_order is None:
            return SORT_GLYPH_INACTIVE
        if self.sort_order == Qt.AscendingOrder:
            return SORT_GLYPH_ASCENDING
        return SORT_GLYPH_DESCENDING

    def _glyph_font(self):
        font = self.font()
        font.setPointSizeF(font.pointSizeF() * FONT.sort_glyph_scale)
        return font

    def _glyph_space(self) -> int:
        metrics = QFontMetrics(self._glyph_font())
        widest = max(
            metrics.horizontalAdvance(glyph)
            for glyph in (SORT_GLYPH_INACTIVE, SORT_GLYPH_ASCENDING, SORT_GLYPH_DESCENDING)
        )
        return widest + SPACE.sm

    def sectionSizeFromContents(self, logicalIndex: int):  # noqa: N802
        size = super().sectionSizeFromContents(logicalIndex)
        if logicalIndex in SORTABLE_COLUMNS:
            size.setWidth(size.width() + self._glyph_space())
        return size

    def paintSection(self, painter, rect, logicalIndex: int) -> None:  # noqa: N802
        painter.save()
        super().paintSection(painter, rect, logicalIndex)
        painter.restore()
        glyph = self.sort_glyph(logicalIndex)
        if glyph is None:
            return
        active = glyph != SORT_GLYPH_INACTIVE
        painter.save()
        painter.setFont(self._glyph_font())
        painter.setPen(
            QColor(COLOR.sort_glyph_active if active else COLOR.sort_glyph_inactive)
        )
        painter.drawText(
            rect.adjusted(0, 0, -SPACE.sm, 0), int(Qt.AlignRight | Qt.AlignVCenter), glyph
        )
        painter.restore()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        super().mouseMoveEvent(event)
        section = self.logicalIndexAt(event.position().toPoint())
        if self.sectionsClickable() and section in SORTABLE_COLUMNS:
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.unsetCursor()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.unsetCursor()
        super().leaveEvent(event)


class TrackTableView(QTableView):
    rows_dropped = Signal(list, int)
    sort_requested = Signal(int)
    remove_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.removal_enabled = True
        self.setHorizontalHeader(TrackHeaderView(self))
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(False)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(False)
        header.sectionClicked.connect(self.sort_requested.emit)
        header.setHighlightSections(False)
        self.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.ElideMiddle)
        self._apply_row_metrics()

    def setModel(self, model) -> None:  # noqa: N802
        super().setModel(model)
        self._apply_column_modes()

    def _apply_column_modes(self) -> None:
        """Filename fills the width; other columns fit their contents.

        Applied after setModel: setting a model resets the header's sections.
        """

        header = self.horizontalHeader()
        for column in range(header.count()):
            header.setSectionResizeMode(
                column,
                QHeaderView.Stretch
                if column == FILENAME_COLUMN
                else QHeaderView.ResizeToContents,
            )

    def row_height(self) -> int:
        return QFontMetrics(self.font()).height() + SIZE.row_padding

    def _apply_row_metrics(self) -> None:
        """Compact rows sized from the font, so any scale factor stays crisp."""

        height = self.row_height()
        rows = self.verticalHeader()
        rows.setMinimumSectionSize(height)
        rows.setDefaultSectionSize(height)
        header_height = self.horizontalHeader().sizeHint().height()
        self.setMinimumHeight(header_height + height * SIZE.min_table_rows + 2 * self.frameWidth())

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._apply_row_metrics()

    def _has_selection(self) -> bool:
        return bool(self.selectionModel() and self.selectionModel().selectedRows())

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        action = menu.addAction(REMOVE_ACTION_TEXT)
        action.setEnabled(self.removal_enabled and self._has_selection())
        action.triggered.connect(self.remove_requested.emit)
        menu.exec(event.globalPos())

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key_Delete and event.modifiers() == Qt.NoModifier:
            if self.removal_enabled and self._has_selection():
                self.remove_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def startDrag(self, supported_actions: Qt.DropActions) -> None:  # noqa: N802
        rows = sorted({index.row() for index in self.selectionModel().selectedRows()})
        if not rows:
            return
        mime_data = QMimeData()
        mime_data.setData(ROWS_MIME_TYPE, QByteArray(json.dumps(rows).encode("utf-8")))
        drag = QDrag(self)
        drag.setMimeData(mime_data)
        drag.exec(Qt.MoveAction)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.source() is self and event.mimeData().hasFormat(ROWS_MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.source() is self and event.mimeData().hasFormat(ROWS_MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        if event.source() is not self or not event.mimeData().hasFormat(ROWS_MIME_TYPE):
            event.ignore()
            return
        try:
            rows = json.loads(bytes(event.mimeData().data(ROWS_MIME_TYPE)).decode("utf-8"))
        except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            event.ignore()
            return

        position = event.position().toPoint()
        target_index = self.indexAt(position)
        if target_index.isValid():
            rect = self.visualRect(target_index)
            insertion_row = target_index.row() + int(position.y() >= rect.center().y())
        else:
            insertion_row = self.model().rowCount()
        self.rows_dropped.emit(rows, insertion_row)
        event.setDropAction(Qt.MoveAction)
        event.accept()


class TrackListWidget(QWidget):
    selection_changed = Signal()
    sort_requested = Signal(int)
    manual_order_changed = Signal()
    remove_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = TrackTableModel(self)
        self.table = TrackTableView(self)
        self.table.setModel(self.model)
        self.table.rows_dropped.connect(self._drop_rows)
        self.table.sort_requested.connect(self.sort_requested.emit)
        self.table.remove_requested.connect(self.remove_requested.emit)
        self.table.selectionModel().selectionChanged.connect(
            lambda _selected, _deselected: self.selection_changed.emit()
        )

        self.empty_label = QLabel("No music folder selected.", self)
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setMargin(SPACE.lg)
        set_role(self.empty_label, "hint")

        self.stack = QStackedLayout(self)
        self.stack.addWidget(self.empty_label)
        self.stack.addWidget(self.table)

    @property
    def tracks(self) -> list[Track]:
        return list(self.model.tracks)

    def set_tracks(self, tracks: Iterable[Track], empty_message: str) -> None:
        loaded = list(tracks)
        self.model.replace_tracks(loaded)
        self.empty_label.setText(empty_message)
        self.stack.setCurrentWidget(self.table if loaded else self.empty_label)
        self.selection_changed.emit()

    def refresh_tracks(self, tracks: Iterable[Track]) -> None:
        """Replace renamed Track snapshots while retaining row order/selection."""

        selected = self.selected_rows()
        self.model.replace_tracks(tracks)
        self._select_rows(selected)

    def replace_tracks_preserving_selection(self, tracks: Iterable[Track]) -> None:
        """Follow selected track paths when their row positions change."""

        selected_paths = {
            self.model.tracks[row].original_path for row in self.selected_rows()
        }
        loaded = list(tracks)
        self.model.replace_tracks(loaded)
        self._select_rows(
            row
            for row, track in enumerate(loaded)
            if track.original_path in selected_paths
        )

    def set_flags(self, new_paths: set, duplicate_reasons: dict) -> None:
        self.model.set_flags(new_paths, duplicate_reasons)

    def show_sort_indicator(
        self, column: int | None, direction: SortDirection | None
    ) -> None:
        header = self.table.horizontalHeader()
        if column is None or direction is None:
            header.set_sort_state(None, None)
            return
        order = (
            Qt.AscendingOrder
            if direction == SortDirection.ASCENDING
            else Qt.DescendingOrder
        )
        header.set_sort_state(column, order)

    def set_order_changes_enabled(self, enabled: bool) -> None:
        self.table.setDragEnabled(enabled)
        self.table.setAcceptDrops(enabled)
        self.table.horizontalHeader().setSectionsClickable(enabled)
        self.table.removal_enabled = enabled

    def selected_paths(self) -> list:
        return [self.model.tracks[row].original_path for row in self.selected_rows()]

    def remove_tracks(self, paths: Iterable) -> None:
        """Drop rows in memory, keeping the order of the rest; # renumbers."""

        removed = set(paths)
        remaining = [t for t in self.model.tracks if t.original_path not in removed]
        self.model.replace_tracks(remaining)
        self.table.selectionModel().clearSelection()
        if not remaining:
            self.empty_label.setText(EMPTY_FOLDER_MESSAGE)
            self.stack.setCurrentWidget(self.empty_label)
        self.selection_changed.emit()

    def selected_rows(self) -> list[int]:
        return sorted(index.row() for index in self.table.selectionModel().selectedRows())

    def _select_rows(self, rows: Iterable[int]) -> None:
        selected = list(rows)
        selection_model = self.table.selectionModel()
        selection_model.clearSelection()
        for row in selected:
            index = self.model.index(row, 0)
            selection_model.select(
                index,
                QItemSelectionModel.SelectionFlag.Select
                | QItemSelectionModel.SelectionFlag.Rows,
            )
        if selected:
            current = self.model.index(selected[0], 0)
            selection_model.setCurrentIndex(
                current, QItemSelectionModel.SelectionFlag.NoUpdate
            )
            self.table.scrollTo(current)

    def _apply_order(
        self, operation: Callable[[list[Track], list[int]], tuple[list[Track], list[int]]]
    ) -> None:
        rows = self.selected_rows()
        if not rows:
            return
        tracks, new_rows = operation(self.model.tracks, rows)
        changed = tracks != self.model.tracks
        self.model.replace_tracks(tracks)
        self._select_rows(new_rows)
        if changed:
            self.manual_order_changed.emit()

    def move_selected_top(self) -> None:
        self._apply_order(move_top)

    def move_selected_up(self) -> None:
        self._apply_order(move_up)

    def move_selected_down(self) -> None:
        self._apply_order(move_down)

    def move_selected_bottom(self) -> None:
        self._apply_order(move_bottom)

    def _drop_rows(self, rows: list[int], insertion_row: int) -> None:
        tracks, new_rows = move_to_insertion(self.model.tracks, rows, insertion_row)
        changed = tracks != self.model.tracks
        self.model.replace_tracks(tracks)
        self._select_rows(new_rows)
        if changed:
            self.manual_order_changed.emit()
