"""Application entry point for USB Track Order."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from app import __version__
from app.main_window import MainWindow
from app.paths import data_dir
from app.ui.theme import apply_theme, configure_high_dpi, follow_screen_scale

LOG_MAX_BYTES = 1024 * 1024


def resource_path(relative_path: str) -> Path:
    """Resolve bundled data in both source and PyInstaller one-file modes."""

    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_root / relative_path


def configure_logging() -> None:
    """Log to usb_track_order.log next to the loudness cache (best effort)."""

    from logging.handlers import RotatingFileHandler

    log_path = data_dir() / "usb_track_order.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            log_path, maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8"
        )
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger("usb_track_order")
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def self_test() -> int:
    """Exit 0 when bundled dependencies work (used to verify frozen builds)."""

    from pypinyin import lazy_pinyin

    from app.services.track_sorting import name_sort_key

    ok = lazy_pinyin("周杰倫") == ["zhou", "jie", "lun"]
    ok = ok and name_sort_key("周杰倫.mp3")[:3] == name_sort_key("周杰伦.mp3")[:3]
    return 0 if ok else 1


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    configure_logging()
    configure_high_dpi()
    app = QApplication(sys.argv)
    apply_theme(app)
    app.setApplicationName("USB Track Order")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("USB Track Order")
    icon_path = resource_path("icon/Icon2.png")
    if icon_path.exists():
        icon = QIcon(str(icon_path))
        app.setWindowIcon(icon)
    window = MainWindow()
    if icon_path.exists():
        window.setWindowIcon(icon)
    window.show()
    follow_screen_scale(window)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
