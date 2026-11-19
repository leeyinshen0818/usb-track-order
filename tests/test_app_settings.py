import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from app import __version__
from app.main_window import MainWindow


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_target_loudness_persists_between_windows(
    tmp_path: Path, monkeypatch
) -> None:
    _app = application()
    monkeypatch.setattr("app.main_window.discover_removable_drives", lambda: [])
    path = tmp_path / "settings.ini"

    first_settings = QSettings(str(path), QSettings.IniFormat)
    first = MainWindow(settings=first_settings)
    assert first.target_lufs.value() == -14.0
    first.target_lufs.setValue(-12.5)
    first_settings.sync()
    first.close()

    second_settings = QSettings(str(path), QSettings.IniFormat)
    second = MainWindow(settings=second_settings)
    assert second.target_lufs.value() == -12.5
    assert f"v{__version__}" in second.windowTitle()
    second.close()


def test_invalid_saved_target_uses_default(tmp_path: Path, monkeypatch) -> None:
    _app = application()
    monkeypatch.setattr("app.main_window.discover_removable_drives", lambda: [])
    settings = QSettings(str(tmp_path / "invalid.ini"), QSettings.IniFormat)
    settings.setValue("audio/target_lufs", "invalid")
    window = MainWindow(settings=settings)
    assert window.target_lufs.value() == -14.0
    window.close()
