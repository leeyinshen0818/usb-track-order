"""Compact confirmation dialog for a potentially large rename operation."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.services.safe_renamer import RenameAction


def _preview_lines(actions: Sequence[RenameAction]) -> list[str]:
    if len(actions) <= 15:
        shown = list(actions)
        omitted = False
    else:
        shown = [*actions[:10], *actions[-5:]]
        omitted = True

    lines = [f"{action.source.name}  →  {action.target.name}" for action in shown]
    if omitted:
        lines.insert(10, f"… {len(actions) - 15} more changes …")
    return lines


class RenamePreviewDialog(QDialog):
    def __init__(
        self,
        *,
        title: str,
        summary: str,
        action_text: str,
        actions: Sequence[RenameAction],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(680, 430)

        layout = QVBoxLayout(self)
        summary_label = QLabel(summary)
        summary_label.setWordWrap(True)
        layout.addWidget(summary_label)

        preview = QPlainTextEdit()
        preview.setReadOnly(True)
        preview.setPlainText("\n".join(_preview_lines(actions)))
        layout.addWidget(preview, 1)

        note = QLabel("Files will be renamed only after you confirm this operation.")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        apply_button = buttons.addButton(action_text, QDialogButtonBox.AcceptRole)
        apply_button.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @classmethod
    def confirm(
        cls,
        *,
        title: str,
        summary: str,
        action_text: str,
        actions: Sequence[RenameAction],
        parent: QWidget | None = None,
    ) -> bool:
        dialog = cls(
            title=title,
            summary=summary,
            action_text=action_text,
            actions=actions,
            parent=parent,
        )
        return dialog.exec() == QDialog.Accepted
