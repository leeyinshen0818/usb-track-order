"""Application entry point for USB Track Order."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from app import __version__
from app.main_window import MainWindow


def resource_path(relative_path: str) -> Path:
    """Resolve bundled data in both source and PyInstaller one-file modes."""

    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return bundle_root / relative_path


def main() -> int:
    app = QApplication(sys.argv)
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
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
