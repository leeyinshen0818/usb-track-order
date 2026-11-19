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
    sort_glyph_scale: float = 1.25  # symbol fonts draw arrows small


@dataclass(frozen=True, slots=True)
class Colors:
    # Surfaces
    window_bg: str = "#1F1F1F"
    panel_bg: str = "#272727"
    input_bg: str = "#1A1A1A"
    table_bg: str = "#232323"  # zebra rows: table_bg / table_alt_bg
    table_alt_bg: str = "#292929"
    header_bg: str = "#303030"
    header_hover_bg: str = "#393939"
    border: str = "#3A3A3A"
    # Text
    text: str = "#E6E6E6"
    text_hint: str = "#A9A9A9"
    text_notice: str = "#D2A24A"  # muted amber warning
    # Secondary (default) buttons
    button_bg: str = "#333333"
    button_hover_bg: str = "#3D3D3D"
    button_pressed_bg: str = "#2B2B2B"
    button_border: str = "#464646"
    # The single accent: Copy to USB, progress, check box, focus
    accent_bg: str = "#2F74B5"
    accent_hover_bg: str = "#2A6AA6"  # darker, so white text keeps >= 4.5:1
    accent_border: str = "#3B80C0"
    accent_text: str = "#FFFFFF"
    focus_border: str = "#2F74B5"
    # Soft accent: a pending step (Apply Numbering), quieter than Copy's
    soft_accent_bg: str = "#2C3A48"
    soft_accent_hover_bg: str = "#33424F"
    soft_accent_border: str = "#2F74B5"
    # Disabled: flat, dashed outline, dimmer than hint text (never equal to it)
    disabled_bg: str = "#272727"
    disabled_border: str = "#3A3A3A"
    disabled_text: str = "#7A7A7A"
    # Selection, progress, and the faint warm gray-green "new track" tint
    selection_bg: str = "#2F3B4A"
    selection_text: str = "#FFFFFF"
    progress_chunk: str = "#2F74B5"
    progress_text: str = "#FFFFFF"  # drawn over the chunk and the empty track
    new_row_tint: str = "#9AA88A"
    new_row_alpha: float = 0.12
    duplicate_row_tint: str = "#D2A24A"  # muted amber, same hue as warnings
    duplicate_row_alpha: float = 0.16
    sort_glyph_active: str = "#E6E6E6"
    sort_glyph_inactive: str = "#A9A9A9"
    link: str = "#D2A24A"  # the clickable "possible duplicates" notice


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
        blend(COLOR.new_row_tint, COLOR.table_bg, COLOR.new_row_alpha),
        blend(COLOR.new_row_tint, COLOR.table_alt_bg, COLOR.new_row_alpha),
    )


def duplicate_row_backgrounds() -> tuple[str, str]:
    return (
        blend(COLOR.duplicate_row_tint, COLOR.table_bg, COLOR.duplicate_row_alpha),
        blend(COLOR.duplicate_row_tint, COLOR.table_alt_bg, COLOR.duplicate_row_alpha),
    )


def text_pairs() -> list[tuple[str, str, str, float]]:
    """(name, foreground, background, minimum ratio) for every text use."""

    c = COLOR
    pairs: list[tuple[str, str, str, float]] = []
    for bg_name, bg in (
        ("window", c.window_bg),
        ("panel", c.panel_bg),
        ("input", c.input_bg),
        ("table", c.table_bg),
        ("table alt", c.table_alt_bg),
        ("header", c.header_bg),
        ("header hover", c.header_hover_bg),
        ("soft accent", c.soft_accent_bg),
        ("soft accent hover", c.soft_accent_hover_bg),
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
    for index, bg in enumerate(duplicate_row_backgrounds()):
        pairs.append((f"text on duplicate row {index}", c.text, bg, MIN_TEXT_CONTRAST))
    for bg_name, bg in (("header", c.header_bg), ("header hover", c.header_hover_bg)):
        pairs.append((f"active sort glyph on {bg_name}", c.sort_glyph_active, bg, MIN_TEXT_CONTRAST))
        pairs.append((f"inactive sort glyph on {bg_name}", c.sort_glyph_inactive, bg, MIN_TEXT_CONTRAST))
    pairs.append(("duplicates link on window", c.link, c.window_bg, MIN_TEXT_CONTRAST))
    pairs += [
        ("primary text", c.accent_text, c.accent_bg, MIN_TEXT_CONTRAST),
        ("primary hover text", c.accent_text, c.accent_hover_bg, MIN_TEXT_CONTRAST),
        ("selected row text", c.selection_text, c.selection_bg, MIN_TEXT_CONTRAST),
        ("progress text on chunk", c.progress_text, c.progress_chunk, MIN_TEXT_CONTRAST),
        ("progress text on track", c.progress_text, c.input_bg, MIN_TEXT_CONTRAST),
        ("check mark on accent", c.accent_text, c.accent_bg, MIN_TEXT_CONTRAST),
        ("status bar text", c.text_hint, c.panel_bg, MIN_TEXT_CONTRAST),
        ("disabled text", c.disabled_text, c.disabled_bg, MIN_DISABLED_CONTRAST),
    ]
    return pairs
