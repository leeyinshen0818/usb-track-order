"""Move files to the Recycle Bin. Never deletes permanently."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QFile


def move_to_trash(path: Path) -> bool:
    """True when the file went to the Recycle Bin.

    False when the volume has no Recycle Bin (often removable drives) or the
    move failed; the file is then left untouched.
    """

    result = QFile.moveToTrash(str(path))
    # PySide6 returns bool or (bool, path_in_trash) depending on the overload.
    if isinstance(result, tuple):
        return bool(result[0])
    return bool(result)
