"""Lightweight, mockable Windows removable-drive discovery."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DRIVE_REMOVABLE = 2


def format_bytes(byte_count: int) -> str:
    value = float(max(0, byte_count))
    units = ("bytes", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "bytes":
                return f"{int(value)} bytes"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


@dataclass(frozen=True, slots=True)
class UsbDrive:
    root: Path
    drive_letter: str
    volume_label: str
    total_bytes: int
    free_bytes: int
    volume_serial: int | None = None

    @property
    def display_name(self) -> str:
        label = self.volume_label.strip() or "Removable Drive"
        return f"{label} ({self.drive_letter.upper()}:)"

    @property
    def capacity_text(self) -> str:
        return f"{format_bytes(self.free_bytes)} free of {format_bytes(self.total_bytes)}"

    @property
    def identity_key(self) -> tuple[str, int | str, int]:
        """Identify a mounted device without treating free-space changes as replacement."""

        device = self.volume_serial if self.volume_serial is not None else self.volume_label.casefold()
        return (str(self.root).casefold(), device, self.total_bytes)


class UsbDestinationError(RuntimeError):
    """A selected export folder is unavailable or outside the active USB drive."""


def validate_usb_destination(
    drive_root: str | Path, destination: str | Path
) -> Path:
    """Resolve an existing directory and prove it is inside the selected drive.

    Resolving both paths also prevents a directory junction or symlink inside the
    drive from escaping to another volume.
    """

    root = Path(drive_root)
    folder = Path(destination)
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise UsbDestinationError(
            "The selected USB drive is unavailable or has been disconnected."
        ) from exc
    if not resolved_root.is_dir():
        raise UsbDestinationError("The selected USB drive root is not a folder.")
    try:
        resolved_folder = folder.resolve(strict=True)
    except OSError as exc:
        raise UsbDestinationError(
            "The selected destination folder no longer exists. Choose another folder."
        ) from exc
    if not resolved_folder.is_dir():
        raise UsbDestinationError("The selected destination is not a folder.")
    try:
        resolved_folder.relative_to(resolved_root)
    except ValueError as exc:
        raise UsbDestinationError(
            "Choose a folder located inside the currently selected USB drive."
        ) from exc
    return resolved_folder


class DriveApi(Protocol):
    def logical_drive_roots(self) -> list[str]: ...

    def drive_type(self, root: str) -> int: ...

    def volume_label(self, root: str) -> str: ...

    def volume_serial(self, root: str) -> int | None: ...

    def disk_space(self, root: str) -> tuple[int, int]: ...


class WindowsDriveApi:
    """Small wrapper around non-administrative Win32 volume APIs."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Removable-drive discovery is available only on Windows.")
        from ctypes import wintypes

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.GetLogicalDrives.restype = wintypes.DWORD
        self._kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        self._kernel32.GetDriveTypeW.restype = wintypes.UINT
        self._kernel32.GetVolumeInformationW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        self._kernel32.GetVolumeInformationW.restype = wintypes.BOOL
        self._kernel32.GetDiskFreeSpaceExW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
        ]
        self._kernel32.GetDiskFreeSpaceExW.restype = wintypes.BOOL

    def logical_drive_roots(self) -> list[str]:
        mask = self._kernel32.GetLogicalDrives()
        if not mask:
            raise ctypes.WinError(ctypes.get_last_error())
        return [
            f"{chr(ord('A') + index)}:\\"
            for index in range(26)
            if mask & (1 << index)
        ]

    def drive_type(self, root: str) -> int:
        return int(self._kernel32.GetDriveTypeW(root))

    def volume_label(self, root: str) -> str:
        label, _serial = self._volume_details(root)
        return label

    def volume_serial(self, root: str) -> int | None:
        _label, serial = self._volume_details(root)
        return serial

    def _volume_details(self, root: str) -> tuple[str, int | None]:
        from ctypes import wintypes

        label = ctypes.create_unicode_buffer(261)
        filesystem = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        success = self._kernel32.GetVolumeInformationW(
            root,
            label,
            len(label),
            ctypes.byref(serial),
            None,
            None,
            filesystem,
            len(filesystem),
        )
        return (label.value, int(serial.value)) if success else ("", None)

    def disk_space(self, root: str) -> tuple[int, int]:
        available = ctypes.c_ulonglong()
        total = ctypes.c_ulonglong()
        total_free = ctypes.c_ulonglong()
        success = self._kernel32.GetDiskFreeSpaceExW(
            root,
            ctypes.byref(available),
            ctypes.byref(total),
            ctypes.byref(total_free),
        )
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(total.value), int(available.value)


def discover_removable_drives(api: DriveApi | None = None) -> list[UsbDrive]:
    """Return currently accessible drives reported by Windows as removable."""

    if api is None:
        if os.name != "nt":
            return []
        api = WindowsDriveApi()

    drives: list[UsbDrive] = []
    try:
        roots = api.logical_drive_roots()
    except OSError:
        return []
    for root in roots:
        try:
            if api.drive_type(root) != DRIVE_REMOVABLE:
                continue
            total, free = api.disk_space(root)
            letter = root[0] if root else "?"
            drives.append(
                UsbDrive(
                    root=Path(root),
                    drive_letter=letter,
                    volume_label=api.volume_label(root),
                    total_bytes=total,
                    free_bytes=free,
                    volume_serial=getattr(api, "volume_serial", lambda _root: None)(root),
                )
            )
        except OSError:
            continue
    return sorted(drives, key=lambda drive: drive.drive_letter.casefold())
