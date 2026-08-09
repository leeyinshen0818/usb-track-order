"""Table widget with stable multi-selection and multi-row drag/drop."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable

from PySide6.QtCore import (
    QAbstractTableModel,
    QByteArray,
    QItemSelectionModel,
    QMimeData,
    QModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QDrag, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
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

ROWS_MIME_TYPE = "application/x-usb-track-order-rows"


class TrackTableModel(QAbstractTableModel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tracks: list[Track] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.tracks)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else 2

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):  # type: ignore[override]
        if not index.isValid() or not 0 <= index.row() < len(self.tracks):
            return None
        if role == Qt.DisplayRole:
            if index.column() == 0:
                return preview_number(index.row() + 1)
            return self.tracks[index.row()].original_filename
        if role == Qt.TextAlignmentRole and index.column() == 0:
            return int(Qt.AlignCenter)
        if role == Qt.ToolTipRole and index.column() == 1:
            return str(self.tracks[index.row()].original_path)
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return ("#", "Filename")[section]
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


class TrackTableView(QTableView):
    rows_dropped = Signal(list, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
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
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)

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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = TrackTableModel(self)
        self.table = TrackTableView(self)
        self.table.setModel(self.model)
        self.table.rows_dropped.connect(self._drop_rows)
        self.table.selectionModel().selectionChanged.connect(
            lambda _selected, _deselected: self.selection_changed.emit()
        )

        self.empty_label = QLabel("No music folder selected.", self)
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet("color: palette(mid); font-size: 14px; padding: 32px;")

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
        self.model.replace_tracks(tracks)
        self._select_rows(new_rows)

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
        self.model.replace_tracks(tracks)
        self._select_rows(new_rows)
