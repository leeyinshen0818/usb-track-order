import os
import re
from dataclasses import fields

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from app.ui.theme import build_stylesheet
from app.ui.tokens import (
    COLOR,
    MIN_TEXT_CONTRAST,
    blend,
    contrast_ratio,
    text_pairs,
)


def test_contrast_helper_matches_known_values() -> None:
    assert contrast_ratio("#FFFFFF", "#000000") == pytest.approx(21.0)
    assert contrast_ratio("#777777", "#FFFFFF") == pytest.approx(4.48, abs=0.01)
    assert contrast_ratio("#123456", "#123456") == pytest.approx(1.0)
    assert blend("#FFFFFF", "#000000", 0.5) == "#808080"


@pytest.mark.parametrize("name, foreground, background, minimum", text_pairs())
def test_every_token_text_pair_meets_contrast(name, foreground, background, minimum) -> None:
    assert contrast_ratio(foreground, background) >= minimum, name


def test_informational_text_uses_full_minimum() -> None:
    informational = [p for p in text_pairs() if p[0].startswith(("hint", "notice", "status"))]
    assert informational
    assert all(minimum >= MIN_TEXT_CONTRAST for *_rest, minimum in informational)


def test_disabled_controls_are_distinct() -> None:
    assert COLOR.disabled_text.casefold() != COLOR.text_hint.casefold()
    assert contrast_ratio(COLOR.disabled_text, COLOR.text_hint) > 1.3
    assert COLOR.disabled_bg != COLOR.button_bg
    assert COLOR.disabled_border != COLOR.button_border
    assert "dashed" in build_stylesheet()


def test_stylesheet_has_no_untokenized_values() -> None:
    sheet = build_stylesheet()
    allowed_colors = {
        value.casefold()
        for value in (getattr(COLOR, f.name) for f in fields(COLOR))
        if isinstance(value, str)
    }
    colors = re.findall(r"#[0-9A-Fa-f]{6}", sheet)
    assert colors
    for color in colors:
        assert color.casefold() in allowed_colors, color


def test_stylesheet_keeps_row_tints_visible() -> None:
    # Any QTableView::item rule makes Qt's stylesheet engine paint item
    # backgrounds itself and ignore the model's new/duplicate row tints.
    assert "::item" not in build_stylesheet()


@pytest.mark.parametrize(
    "dpr, expected", [(1.0, 1.0), (1.25, 1.0), (1.5, 0.85), (1.75, 0.85), (2.0, 0.85)]
)
def test_ui_scale_table(dpr: float, expected: float) -> None:
    from app.ui.tokens import ui_scale_for

    assert ui_scale_for(dpr) == pytest.approx(expected)
