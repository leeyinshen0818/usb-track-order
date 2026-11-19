"""Applies the calm dark theme. Every value comes from ``app.ui.tokens``."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory, QWidget

from app.ui.tokens import COLOR, FONT, FONT_FAMILY, RADIUS, SPACE

# Dynamic properties read by the stylesheet.
ROLE_PROPERTY = "role"  # "hint" | "notice" | "caption"
PRIMARY_PROPERTY = "primary"
PANEL_NAME = "panel"


def configure_high_dpi() -> None:
    """Plain Qt scaling with fractional factors (125 %, 150 %) kept exact.

    Must run before the QApplication is created.
    """

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )


def text_height(widget: QWidget, padding: int = 0) -> int:
    """Height of one line of the widget's font plus vertical padding."""

    return QFontMetrics(widget.font()).height() + padding


def text_width(widget: QWidget, characters: int) -> int:
    """Width of ``characters`` average characters in the widget's font."""

    return QFontMetrics(widget.font()).averageCharWidth() * characters


def set_role(widget: QWidget, role: str) -> QWidget:
    widget.setProperty(ROLE_PROPERTY, role)
    return widget


def set_primary(widget: QWidget) -> QWidget:
    widget.setProperty(PRIMARY_PROPERTY, True)
    return widget


def build_palette() -> QPalette:
    c = COLOR
    palette = QPalette()
    entries = {
        QPalette.Window: c.window_bg,
        QPalette.WindowText: c.text,
        QPalette.Base: c.input_bg,
        QPalette.AlternateBase: c.table_alt_bg,
        QPalette.Text: c.text,
        QPalette.Button: c.button_bg,
        QPalette.ButtonText: c.text,
        QPalette.ToolTipBase: c.panel_bg,
        QPalette.ToolTipText: c.text,
        QPalette.PlaceholderText: c.text_hint,
        QPalette.Highlight: c.selection_bg,
        QPalette.HighlightedText: c.selection_text,
        QPalette.Mid: c.border,
        QPalette.Dark: c.input_bg,
        QPalette.Light: c.button_hover_bg,
        QPalette.Link: c.focus_border,
    }
    for role, color in entries.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor(c.disabled_text))
    palette.setColor(QPalette.Disabled, QPalette.Button, QColor(c.disabled_bg))
    return palette


def build_indicator_palette() -> QPalette:
    """Fusion outlines check boxes and spin boxes with Window.darker(); a light
    Window role keeps that outline visible on the dark panels."""

    palette = build_palette()
    palette.setColor(QPalette.Window, QColor(COLOR.text_hint))
    return palette


def build_stylesheet() -> str:
    c, s, r = COLOR, SPACE, RADIUS
    return f"""
QMainWindow, QScrollArea#page, QWidget#pageContent {{
    background: {c.window_bg};
}}
QFrame#{PANEL_NAME} {{
    background: {c.panel_bg};
    border: 1px solid {c.border};
    border-radius: {r.md}px;
}}
QFrame#{PANEL_NAME} QLabel {{
    background: transparent;
}}
QLabel[{ROLE_PROPERTY}="hint"] {{ color: {c.text_hint}; }}
QLabel[{ROLE_PROPERTY}="notice"] {{ color: {c.text_notice}; }}
QLabel[{ROLE_PROPERTY}="caption"] {{
    color: {c.text_hint};
    font-size: {FONT.caption}pt;
    font-weight: 600;
}}
QPushButton {{
    background: {c.button_bg};
    color: {c.text};
    border: 1px solid {c.button_border};
    border-radius: {r.sm}px;
    padding: {s.xs}px {s.md}px;
}}
QPushButton:hover {{ background: {c.button_hover_bg}; }}
QPushButton:pressed {{ background: {c.button_pressed_bg}; }}
QPushButton:focus {{ border-color: {c.focus_border}; }}
QPushButton[{PRIMARY_PROPERTY}="true"] {{
    background: {c.accent_bg};
    color: {c.accent_text};
    border-color: {c.accent_border};
    font-weight: 600;
}}
QPushButton[{PRIMARY_PROPERTY}="true"]:hover {{ background: {c.accent_hover_bg}; }}
QPushButton:disabled, QPushButton[{PRIMARY_PROPERTY}="true"]:disabled {{
    background: {c.disabled_bg};
    color: {c.disabled_text};
    border: 1px dashed {c.disabled_border};
    font-weight: normal;
}}
QLineEdit, QComboBox {{
    background: {c.input_bg};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: {r.sm}px;
    padding: {s.xs // 2}px {s.sm}px;
    selection-background-color: {c.selection_bg};
    selection-color: {c.selection_text};
}}
QLineEdit:focus, QComboBox:focus {{
    border-color: {c.focus_border};
}}
QLineEdit:disabled, QComboBox:disabled {{
    background: {c.disabled_bg};
    color: {c.disabled_text};
    border: 1px dashed {c.disabled_border};
}}
QComboBox QAbstractItemView {{
    background: {c.panel_bg};
    color: {c.text};
    selection-background-color: {c.selection_bg};
    selection-color: {c.selection_text};
}}
QCheckBox {{ color: {c.text}; spacing: {s.sm}px; }}
QCheckBox:disabled {{ color: {c.disabled_text}; }}
QTableView {{
    background: {c.input_bg};
    alternate-background-color: {c.table_alt_bg};
    color: {c.text};
    gridline-color: {c.border};
    border: 1px solid {c.border};
    border-radius: {r.sm}px;
    selection-background-color: {c.selection_bg};
    selection-color: {c.selection_text};
}}
QHeaderView::section {{
    background: {c.header_bg};
    color: {c.text};
    padding: {s.xs}px {s.sm}px;
    border: none;
    border-right: 1px solid {c.border};
    border-bottom: 1px solid {c.border};
    font-weight: 600;
}}
QProgressBar {{
    background: {c.input_bg};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: {r.sm}px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {c.progress_chunk};
    border-radius: {r.sm}px;
}}
QStatusBar {{ background: {c.panel_bg}; color: {c.text_hint}; }}
QStatusBar QLabel {{ color: {c.text_hint}; }}
QToolTip {{
    background: {c.panel_bg};
    color: {c.text};
    border: 1px solid {c.border};
    padding: {s.xs}px;
}}
"""


def apply_theme(app: QApplication) -> None:
    """Same look on every PC: Fusion style, token palette, font, stylesheet."""

    fusion = QStyleFactory.create("Fusion")
    if fusion is not None:
        app.setStyle(fusion)
    app.setPalette(build_palette())
    for class_name in ("QCheckBox", "QAbstractSpinBox"):
        app.setPalette(build_indicator_palette(), class_name)
    font = QFont(FONT_FAMILY)
    font.setPointSizeF(FONT.body)
    app.setFont(font)
    app.setStyleSheet(build_stylesheet())
