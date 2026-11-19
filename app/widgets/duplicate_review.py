"""Review duplicate groups and pick extra copies to move to the Recycle Bin."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.services.track_flags import DuplicateGroup, DuplicateTier
from app.services.usb_drives import format_bytes
from app.ui.theme import set_role, text_height

PATH_ROLE = Qt.UserRole + 1
COLUMNS = ("Filename", "Size", "Date Modified", "Match")
TIER_LABELS = {
    DuplicateTier.LIKELY: "Likely duplicate",
    DuplicateTier.SIMILAR: "Similar name",
}
REMOVE_BUTTON_TEXT = "Move to Recycle Bin"


def preselected_paths(groups: Sequence[DuplicateGroup]) -> set[Path]:
    """Only extra copies in "likely" groups; never anything "similar name"."""

    return {
        path
        for group in groups
        if group.tier == DuplicateTier.LIKELY
        for path in group.preselected
    }


class DuplicateReviewDialog(QDialog):
    def __init__(
        self, groups: Sequence[DuplicateGroup], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review Duplicates")
        line = text_height(self)
        size = QSize(line * 44, line * 28)
        screen = self.screen()
        if screen is not None:
            size = size.boundedTo(screen.availableGeometry().size())
        self.resize(size)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Songs that may be the same are listed together. Extra copies of "
            "likely duplicates are ticked for you; \"similar name\" songs are "
            "never ticked, because different songs can share a name."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, len(COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        preselected = preselected_paths(groups)
        for number, group in enumerate(groups, start=1):
            heading = QTreeWidgetItem(
                [f"Group {number} — {TIER_LABELS[group.tier].lower()}"]
            )
            heading.setFlags(Qt.ItemIsEnabled)
            heading.setFirstColumnSpanned(True)
            font = heading.font(0)
            font.setBold(True)
            heading.setFont(0, font)
            self.tree.addTopLevelItem(heading)
            for track in group.tracks:
                modified = (
                    datetime.fromtimestamp(track.modified_time).strftime("%d/%m/%Y %H:%M")
                    if track.modified_time is not None
                    else "—"
                )
                size_text = (
                    format_bytes(track.size_bytes) if track.size_bytes is not None else "—"
                )
                item = QTreeWidgetItem(
                    [track.original_filename, size_text, modified, TIER_LABELS[group.tier]]
                )
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                item.setCheckState(
                    0,
                    Qt.Checked if track.original_path in preselected else Qt.Unchecked,
                )
                item.setData(0, PATH_ROLE, str(track.original_path))
                item.setToolTip(0, str(track.original_path))
                heading.addChild(item)
            heading.setExpanded(True)
        self.tree.itemChanged.connect(self._update_button)
        layout.addWidget(self.tree, 1)

        note = set_role(
            QLabel("Ticked songs go to the Recycle Bin, so you can restore them."), "hint"
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.remove_button = buttons.addButton(REMOVE_BUTTON_TEXT, QDialogButtonBox.AcceptRole)
        buttons.button(QDialogButtonBox.Cancel).setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_button()

    def checked_paths(self) -> list[Path]:
        paths: list[Path] = []
        for top in range(self.tree.topLevelItemCount()):
            heading = self.tree.topLevelItem(top)
            for child in range(heading.childCount()):
                item = heading.child(child)
                if item.checkState(0) == Qt.Checked:
                    paths.append(Path(item.data(0, PATH_ROLE)))
        return paths

    def _update_button(self, *_args) -> None:
        self.remove_button.setEnabled(bool(self.checked_paths()))

    @classmethod
    def review(
        cls, groups: Sequence[DuplicateGroup], parent: QWidget | None = None
    ) -> list[Path]:
        """Paths the user chose to remove (empty when cancelled)."""

        dialog = cls(groups, parent)
        try:
            if dialog.exec() != QDialog.Accepted:
                return []
            return dialog.checked_paths()
        finally:
            dialog.deleteLater()
