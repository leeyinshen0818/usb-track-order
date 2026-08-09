import os
from pathlib import Path

import pytest

from app.services.usb_drives import (
    DRIVE_REMOVABLE,
    UsbDestinationError,
    UsbDrive,
    discover_removable_drives,
    format_bytes,
    validate_usb_destination,
)


class FakeDriveApi:
    def __init__(self, roots: list[str], types: dict[str, int]) -> None:
        self.roots = roots
        self.types = types

    def logical_drive_roots(self) -> list[str]:
        return self.roots

    def drive_type(self, root: str) -> int:
        return self.types[root]

    def volume_label(self, root: str) -> str:
        return {"E:\\": "KINGSTON", "F:\\": ""}.get(root, "System")

    def volume_serial(self, root: str) -> int:
        return {"E:\\": 1001, "F:\\": 1002}.get(root, 999)

    def disk_space(self, root: str) -> tuple[int, int]:
        return (32 * 1024**3, 15 * 1024**3)


def test_only_removable_drives_are_discovered() -> None:
    api = FakeDriveApi(["C:\\", "E:\\"], {"C:\\": 3, "E:\\": DRIVE_REMOVABLE})
    drives = discover_removable_drives(api)
    assert len(drives) == 1
    assert drives[0].drive_letter == "E"
    assert drives[0].display_name == "KINGSTON (E:)"
    assert drives[0].free_bytes == 15 * 1024**3
    assert drives[0].total_bytes == 32 * 1024**3


def test_no_removable_drive_state() -> None:
    api = FakeDriveApi(["C:\\"], {"C:\\": 3})
    assert discover_removable_drives(api) == []


def test_blank_volume_uses_friendly_label() -> None:
    api = FakeDriveApi(["F:\\"], {"F:\\": DRIVE_REMOVABLE})
    drive = discover_removable_drives(api)[0]
    assert drive.display_name == "Removable Drive (F:)"


def test_capacity_formatting() -> None:
    assert format_bytes(0) == "0 bytes"
    assert format_bytes(1536) == "1.5 KB"
    assert format_bytes(2 * 1024**3) == "2.0 GB"


def test_selecting_usb_root_is_valid(tmp_path: Path) -> None:
    usb_root = tmp_path / "usb"
    usb_root.mkdir()
    assert validate_usb_destination(usb_root, usb_root) == usb_root.resolve()


def test_selecting_nested_usb_folder_is_valid(tmp_path: Path) -> None:
    usb_root = tmp_path / "usb"
    music = usb_root / "Car Music" / "Songs"
    music.mkdir(parents=True)
    assert validate_usb_destination(usb_root, music) == music.resolve()


def test_folder_outside_selected_usb_is_rejected(tmp_path: Path) -> None:
    usb_root = tmp_path / "usb"
    outside = tmp_path / "outside"
    usb_root.mkdir()
    outside.mkdir()
    with pytest.raises(UsbDestinationError, match="inside"):
        validate_usb_destination(usb_root, outside)


def test_destination_folder_disappearing_is_rejected(tmp_path: Path) -> None:
    usb_root = tmp_path / "usb"
    music = usb_root / "Music"
    music.mkdir(parents=True)
    music.rmdir()
    with pytest.raises(UsbDestinationError, match="no longer exists"):
        validate_usb_destination(usb_root, music)


def test_drive_change_clears_stale_destination(monkeypatch, tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from app.main_window import MainWindow

    first_root = tmp_path / "usb-one"
    second_root = tmp_path / "usb-two"
    nested = first_root / "Music"
    nested.mkdir(parents=True)
    second_root.mkdir()
    first = UsbDrive(first_root, "E", "FIRST", 1000, 900, 111)
    second = UsbDrive(second_root, "F", "SECOND", 1000, 900, 222)
    monkeypatch.setattr(
        "app.main_window.discover_removable_drives", lambda: [first, second]
    )

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._set_usb_destination(nested.resolve(), first)
    window.usb_combo.setCurrentIndex(1)
    assert window.usb_destination_path == second.root
    assert window.usb_destination_field.text() == str(second.root)
    window.close()
