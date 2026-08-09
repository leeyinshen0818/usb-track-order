import pytest

from app.services.track_ordering import (
    move_bottom,
    move_down,
    move_to_insertion,
    move_top,
    move_up,
    preview_number,
)


def test_move_top_single_selection() -> None:
    assert move_top(list("ABCDE"), [2]) == (list("CABDE"), [0])


def test_move_bottom_single_selection() -> None:
    assert move_bottom(list("ABCDE"), [1]) == (list("ACDEB"), [4])


def test_move_up_single_selection() -> None:
    assert move_up(list("ABCDE"), [2]) == (list("ACBDE"), [1])


def test_move_down_single_selection() -> None:
    assert move_down(list("ABCDE"), [1]) == (list("ACBDE"), [2])


def test_adjacent_selection_moves_as_a_block_up() -> None:
    assert move_up(list("ABCDE"), [2, 3]) == (list("ACDBE"), [1, 2])


def test_adjacent_selection_moves_as_a_block_down() -> None:
    assert move_down(list("ABCDE"), [1, 2]) == (list("ADBCE"), [2, 3])


def test_move_top_preserves_non_contiguous_relative_order() -> None:
    assert move_top(list("ABCDEF"), [1, 3]) == (list("BDACEF"), [0, 1])


def test_move_bottom_preserves_relative_order() -> None:
    assert move_bottom(list("ABCDEF"), [1, 2]) == (list("ADEFBC"), [4, 5])


def test_non_contiguous_move_up_is_predictable() -> None:
    assert move_up(list("ABCDEF"), [1, 3]) == (list("BADC EF".replace(" ", "")), [0, 2])


def test_non_contiguous_move_down_is_predictable() -> None:
    assert move_down(list("ABCDEF"), [1, 3]) == (list("ACBEDF"), [2, 4])


def test_selection_at_boundary_does_not_reverse_or_wrap() -> None:
    assert move_up(list("ABCDE"), [0, 2]) == (list("ACBDE"), [0, 1])
    assert move_down(list("ABCDE"), [2, 4]) == (list("ABDCE"), [3, 4])


def test_drag_drop_preserves_selected_order() -> None:
    assert move_to_insertion(list("ABCDEF"), [1, 3], 6) == (
        list("ACEFBD"),
        [4, 5],
    )


@pytest.mark.parametrize(
    ("position", "expected"), [(1, "001"), (9, "009"), (45, "045"), (125, "125"), (1000, "1000")]
)
def test_numbering_preview(position: int, expected: str) -> None:
    assert preview_number(position) == expected
