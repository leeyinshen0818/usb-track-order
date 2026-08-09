"""Pure in-memory ordering operations shared by the UI and tests."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TypeVar

T = TypeVar("T")


def preview_number(position: int) -> str:
    """Format a one-based position with at least three digits."""

    if position < 1:
        raise ValueError("position must be at least 1")
    return f"{position:03d}"


def _valid_indices(indices: Iterable[int], length: int) -> list[int]:
    return sorted({index for index in indices if 0 <= index < length})


def move_top(items: Sequence[T], indices: Iterable[int]) -> tuple[list[T], list[int]]:
    selected = _valid_indices(indices, len(items))
    selected_set = set(selected)
    moved = [items[index] for index in selected]
    remainder = [item for index, item in enumerate(items) if index not in selected_set]
    return moved + remainder, list(range(len(moved)))


def move_bottom(items: Sequence[T], indices: Iterable[int]) -> tuple[list[T], list[int]]:
    selected = _valid_indices(indices, len(items))
    selected_set = set(selected)
    moved = [items[index] for index in selected]
    remainder = [item for index, item in enumerate(items) if index not in selected_set]
    start = len(remainder)
    return remainder + moved, list(range(start, start + len(moved)))


def move_up(items: Sequence[T], indices: Iterable[int]) -> tuple[list[T], list[int]]:
    """Move every selected run up by one unselected row."""

    result = list(items)
    selected = set(_valid_indices(indices, len(result)))
    for index in range(1, len(result)):
        if index in selected and index - 1 not in selected:
            result[index - 1], result[index] = result[index], result[index - 1]
            selected.remove(index)
            selected.add(index - 1)
    return result, sorted(selected)


def move_down(items: Sequence[T], indices: Iterable[int]) -> tuple[list[T], list[int]]:
    """Move every selected run down by one unselected row."""

    result = list(items)
    selected = set(_valid_indices(indices, len(result)))
    for index in range(len(result) - 2, -1, -1):
        if index in selected and index + 1 not in selected:
            result[index + 1], result[index] = result[index], result[index + 1]
            selected.remove(index)
            selected.add(index + 1)
    return result, sorted(selected)


def move_to_insertion(
    items: Sequence[T], indices: Iterable[int], insertion_index: int
) -> tuple[list[T], list[int]]:
    """Move selected rows before an original-list insertion point.

    This models a table drag/drop: ``insertion_index == len(items)`` means the
    end. Selected rows retain their relative order regardless of contiguity.
    """

    selected = _valid_indices(indices, len(items))
    if not selected:
        return list(items), []

    selected_set = set(selected)
    bounded_target = max(0, min(insertion_index, len(items)))
    adjusted_target = bounded_target - sum(
        1 for index in selected if index < bounded_target
    )
    moved = [items[index] for index in selected]
    remainder = [item for index, item in enumerate(items) if index not in selected_set]
    result = remainder[:adjusted_target] + moved + remainder[adjusted_target:]
    new_indices = list(range(adjusted_target, adjusted_target + len(moved)))
    return result, new_indices
