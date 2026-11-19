"""Design tokens: the single source of spacing, radii, font sizes, and colors.

Sizes are logical pixels (Qt scales them by the device pixel ratio) and font
sizes are points, so the UI looks the same at 100 %, 125 %, and 150 %.
Text heights are never fixed here: widgets derive them from font metrics.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Spacing:
    xs: int = 4
    sm: int = 8
    md: int = 12
    lg: int = 16


@dataclass(frozen=True, slots=True)
class Radii:
    sm: int = 4
    md: int = 6


@dataclass(frozen=True, slots=True)
class FontSizes:
    """Point sizes. Body text stays large enough for older eyes."""

    body: float = 10.0
    caption: float = 10.0


@dataclass(frozen=True, slots=True)
class Colors:
    # Surfaces
    window_bg: str = "#1E2226"
    panel_bg: str = "#262B30"
    input_bg: str = "#181B1F"
    table_alt_bg: str = "#20242A"
    header_bg: str = "#2C3238"
    border: str = "#3B434B"
    # Text
    text: str = "#E8EAED"
    text_hint: str = "#AEB6BF"
    text_notice: str = "#E9C46A"
    # Secondary (default) buttons
    button_bg: str = "#353C44"
    button_hover_bg: str = "#404851"
    button_pressed_bg: str = "#2C3238"
    button_border: str = "#56606B"
    # Primary action: one subtle accent
    accent_bg: str = "#2D5F8F"
    accent_hover_bg: str = "#356CA0"
    accent_border: str = "#5B8CBB"
    accent_text: str = "#FFFFFF"
    focus_border: str = "#7FA8D1"
    # Disabled: flat, dashed outline, dimmer than hint text (never equal to it)
    disabled_bg: str = "#22262B"
    disabled_border: str = "#4A525B"
    disabled_text: str = "#7E8790"
    # Selection, progress, and the calm "new track" row tint
    selection_bg: str = "#3A5878"
    selection_text: str = "#FFFFFF"
    progress_chunk: str = "#2D5F8F"
    new_row_tint: str = "#4F87BF"
    new_row_alpha: float = 0.16


@dataclass(frozen=True, slots=True)
class Sizes:
    """Ratios, not pixels: multiplied by font-metric heights at runtime."""

    row_padding: int = 6  # added to the font height for table rows
    min_table_rows: int = 5  # below this, the page scrolls instead
    window_width_lines: int = 64  # default window size in text-line heights
    window_height_lines: int = 40
    combo_min_chars: int = 18
    field_min_chars: int = 12


SPACE = Spacing()
RADIUS = Radii()
FONT = FontSizes()
COLOR = Colors()
SIZE = Sizes()

FONT_FAMILY = "Segoe UI"
MIN_TEXT_CONTRAST = 4.5
MIN_DISABLED_CONTRAST = 3.0


# --------------------------------------------------------------------------- #
# Contrast (WCAG 2.x)
# --------------------------------------------------------------------------- #


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"Expected #RRGGBB, got {color!r}")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def blend(top: str, bottom: str, alpha: float) -> str:
    """Composite ``top`` at ``alpha`` over an opaque ``bottom`` color."""

    t, b = hex_to_rgb(top), hex_to_rgb(bottom)
    mixed = (round(tc * alpha + bc * (1 - alpha)) for tc, bc in zip(t, b))
    return "#" + "".join(f"{c:02X}" for c in mixed)


def relative_luminance(color: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in hex_to_rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (relative_luminance(foreground), relative_luminance(background)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def new_row_backgrounds() -> tuple[str, str]:
    """The tinted "new track" row over both plain and alternate rows."""

    return (
        blend(COLOR.new_row_tint, COLOR.input_bg, COLOR.new_row_alpha),
        blend(COLOR.new_row_tint, COLOR.table_alt_bg, COLOR.new_row_alpha),
    )


def text_pairs() -> list[tuple[str, str, str, float]]:
    """(name, foreground, background, minimum ratio) for every text use."""

    c = COLOR
    pairs: list[tuple[str, str, str, float]] = []
    for bg_name, bg in (
        ("window", c.window_bg),
        ("panel", c.panel_bg),
        ("input", c.input_bg),
        ("table alt", c.table_alt_bg),
        ("header", c.header_bg),
        ("button", c.button_bg),
        ("button hover", c.button_hover_bg),
        ("button pressed", c.button_pressed_bg),
    ):
        pairs.append((f"text on {bg_name}", c.text, bg, MIN_TEXT_CONTRAST))
    for bg_name, bg in (
        ("window", c.window_bg),
        ("panel", c.panel_bg),
        ("input", c.input_bg),
    ):
        pairs.append((f"hint on {bg_name}", c.text_hint, bg, MIN_TEXT_CONTRAST))
        pairs.append((f"notice on {bg_name}", c.text_notice, bg, MIN_TEXT_CONTRAST))
    for index, bg in enumerate(new_row_backgrounds()):
        pairs.append((f"text on new row {index}", c.text, bg, MIN_TEXT_CONTRAST))
    pairs += [
        ("primary text", c.accent_text, c.accent_bg, MIN_TEXT_CONTRAST),
        ("primary hover text", c.accent_text, c.accent_hover_bg, MIN_TEXT_CONTRAST),
        ("selected row text", c.selection_text, c.selection_bg, MIN_TEXT_CONTRAST),
        ("progress text on chunk", c.text, c.progress_chunk, MIN_TEXT_CONTRAST),
        ("status bar text", c.text_hint, c.panel_bg, MIN_TEXT_CONTRAST),
        ("disabled text", c.disabled_text, c.disabled_bg, MIN_DISABLED_CONTRAST),
    ]
    return pairs
