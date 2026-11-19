"""Small layout helpers that keep text unclipped at any scale factor."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStyle,
    QStyleOption,
    QVBoxLayout,
    QWidget,
)

from app.ui.theme import PANEL_NAME
from app.ui.tokens import SPACE


class ElidedLabel(QLabel):
    """One-line label that elides long text ("…") instead of widening the page.

    ``text()`` still returns the full text, which is also the tooltip.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        super().setText(text)
        self.setToolTip(text)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        metrics = QFontMetrics(self.font())
        margins = self.contentsMargins()
        return QSize(
            metrics.horizontalAdvance("M…") + margins.left() + margins.right(),
            metrics.height() + margins.top() + margins.bottom(),
        )

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        option = QStyleOption()
        option.initFrom(self)
        self.style().drawPrimitive(QStyle.PE_Widget, option, painter, self)
        rect = self.contentsRect()
        elided = QFontMetrics(self.font()).elidedText(
            self.text(), Qt.ElideMiddle, rect.width()
        )
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(rect, int(Qt.AlignLeft | Qt.AlignVCenter), elided)


class ResponsiveRow(QWidget):
    """Lays its children side by side, or stacks them when too narrow.

    Lets a row of grouped controls fit a 1366x768 screen at 150 % without
    clipping or horizontal scrolling.
    """

    def __init__(
        self,
        widgets: Sequence[tuple[QWidget, int]],
        *,
        trailing_stretch: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._widgets = [widget for widget, _stretch in widgets]
        self._row = QBoxLayout(QBoxLayout.LeftToRight, self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(SPACE.sm)
        for widget, stretch in widgets:
            self._row.addWidget(widget, stretch)
        self._trailing_stretch = trailing_stretch
        if trailing_stretch:
            self._row.addStretch(1)

    @property
    def stacked(self) -> bool:
        return self._row.direction() == QBoxLayout.TopToBottom

    def _side_by_side_width(self) -> int:
        visible = [w for w in self._widgets if not w.isHidden()]
        return sum(w.sizeHint().width() for w in visible) + self._row.spacing() * max(
            0, len(visible) - 1
        )

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        width = max(
            (w.minimumSizeHint().width() for w in self._widgets if not w.isHidden()),
            default=0,
        )
        return QSize(width, self._row.minimumSize().height())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        direction = (
            QBoxLayout.TopToBottom
            if event.size().width() < self._side_by_side_width()
            else QBoxLayout.LeftToRight
        )
        if direction != self._row.direction():
            self._row.setDirection(direction)
            self.updateGeometry()


def panel(*, vertical: bool = False) -> tuple[QFrame, QBoxLayout]:
    """A low-key bordered group for related controls."""

    frame = QFrame()
    frame.setObjectName(PANEL_NAME)
    layout: QBoxLayout = QVBoxLayout(frame) if vertical else QHBoxLayout(frame)
    layout.setContentsMargins(SPACE.sm, SPACE.sm, SPACE.sm, SPACE.sm)
    layout.setSpacing(SPACE.sm)
    return frame, layout
