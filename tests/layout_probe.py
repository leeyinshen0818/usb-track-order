"""Measure the main window at one device pixel ratio (run as a subprocess).

Usage: python layout_probe.py <dpr> <grab_dir|-> <W>x<H> [+<W>x<H> ...]

A leading "+" saves widget.grab() of that size as a PNG into grab_dir.

QT_SCALE_FACTOR is process-wide, so every DPR needs its own process. Sizes are
logical; the window gets the screen height minus taskbar and title bar, as a
maximized window would. Prints one JSON object per size.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DPR = sys.argv[1]
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_SCALE_FACTOR"] = DPR
os.environ.pop("QT_ENABLE_HIGHDPI_SCALING", None)
# The offscreen platform has no font database of its own: use the system's.
for fonts in (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts", Path("/usr/share/fonts")):
    if fonts.is_dir():
        os.environ.setdefault("QT_QPA_FONTDIR", str(fonts))
        break
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QLayout, QWidget  # noqa: E402

import app.main_window as main_window  # noqa: E402
from app.models.track import Track  # noqa: E402
from app.services.loudness import LoudnessCache  # noqa: E402
from app.services.usb_drives import UsbDrive  # noqa: E402
from app.ui.theme import apply_theme, configure_high_dpi  # noqa: E402

# Windows taskbar (48) + title bar (~31), in logical pixels at any scale.
WINDOW_CHROME = 80
TRACK_COUNT = 60


def managed_widgets(layout: QLayout) -> list[QWidget]:
    found: list[QWidget] = []
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item.widget() is not None:
            found.append(item.widget())
        elif item.layout() is not None:
            found += managed_widgets(item.layout())
    return found


def check(window: main_window.MainWindow) -> dict:
    too_small: list[str] = []
    overlaps: list[str] = []
    containers = [window.centralWidget().widget(), *window.findChildren(QWidget)]
    for container in containers:
        layout = container.layout()
        if layout is None or not container.isVisible():
            continue
        widgets = [w for w in managed_widgets(layout) if w.isVisible()]
        for widget in widgets:
            hint = widget.minimumSizeHint()
            if hint.isValid() and (
                widget.width() < hint.width() or widget.height() < hint.height()
            ):
                too_small.append(
                    f"{type(widget).__name__}({widget.objectName() or getattr(widget, 'text', lambda: '')()!r})"
                    f" {widget.width()}x{widget.height()} < {hint.width()}x{hint.height()}"
                )
        for i, first in enumerate(widgets):
            for second in widgets[i + 1 :]:
                if first.geometry().intersects(second.geometry()):
                    overlaps.append(f"{type(first).__name__} / {type(second).__name__}")
    table = window.track_list.table
    scroll = window.centralWidget()
    return {
        "too_small": too_small,
        "overlaps": overlaps,
        "visible_rows": table.viewport().height() // table.row_height(),
        "page_hscroll": scroll.horizontalScrollBar().isVisible(),
        "page_vscroll": scroll.verticalScrollBar().isVisible(),
        "dpr": window.devicePixelRatioF(),
    }


def main() -> None:
    grab_dir = None if sys.argv[2] == "-" else Path(sys.argv[2])
    sizes = [
        (arg.startswith("+"), *(int(v) for v in arg.lstrip("+").split("x")))
        for arg in sys.argv[3:]
    ]
    configure_high_dpi()
    app = QApplication([])
    apply_theme(app)

    probe_dir = Path(os.environ["USB_TRACK_ORDER_PROBE_DIR"])
    usb_root = probe_dir / "usb"
    usb_root.mkdir(parents=True, exist_ok=True)
    drive = UsbDrive(usb_root, "E", "KINGSTON DATATRAVELER", 64 * 1024**3, 31 * 1024**3, 7)
    main_window.discover_removable_drives = lambda: [drive]
    settings_path = probe_dir / f"probe-{DPR}.ini"
    settings = QSettings(str(settings_path), QSettings.IniFormat)
    cache = LoudnessCache(settings_path.with_suffix(".json"))
    window = main_window.MainWindow(settings=settings, loudness_cache=cache)
    window.unify_volume_checkbox.setChecked(True)  # widest copy row
    tracks = [
        Track(
            Path(f"C:/Music/{index:03d} Artist {index} - A Fairly Long Song Title.mp3"),
            f"{index:03d} Artist {index} - A Fairly Long Song Title.mp3",
            extension=".mp3",
            size_bytes=4_200_000 + index,
            modified_time=1_700_000_000 + index,
        )
        for index in range(1, TRACK_COUNT + 1)
    ]
    window.track_list.set_tracks(tracks, "")
    window.track_list.set_flags({tracks[1].original_path}, {tracks[2].original_path: "copy"})
    window.count_label.setText(f"{TRACK_COUNT} songs")
    window.notice_label.setText("2 possible duplicates")
    window.folder_path.setText(r"C:\Users\Shop\Music\Car Playlist")
    window.export_current_label.setText(
        "Current: 042 Some Artist - A Very Long Song Title (Extended Live Version).mp3"
    )
    window.show()

    for grab, width, height in sizes:
        window.resize(width, height - WINDOW_CHROME)
        for _ in range(5):
            app.processEvents()
        result = check(window)
        result["size"] = f"{width}x{height}"
        if grab and grab_dir is not None:
            grab_dir.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(grab_dir / f"window-{width}x{height}@{DPR}.png"))
        print(json.dumps(result), flush=True)
    window.close()


if __name__ == "__main__":
    main()
