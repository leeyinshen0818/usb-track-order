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


def wait_for_analysis(window: MainWindow, timeout: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    app = application()
    while window.analysis_running and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert not window.analysis_running


def test_folder_load_cleans_temp_files_and_auto_analyzes(
    tmp_path: Path, monkeypatch
) -> None:
    from app.services.loudness import (
        LoudnessCache,
        LoudnessMeasurement,
        fingerprint_file,
    )

    _app = application()
    monkeypatch.setattr("app.main_window.discover_removable_drives", lambda: [])
    music = tmp_path / "music"
    music.mkdir()
    (music / "A.mp3").write_bytes(b"a" * 100)
    (music / "B.mp3").write_bytes(b"b" * 100)
    (music / ".A.loudness-1234.mp3").write_bytes(b"leftover")
    (music / "B.mp3.part").write_bytes(b"leftover")
    cache = LoudnessCache(tmp_path / "state.json")
    for name in ("A.mp3", "B.mp3"):
        cache.put(fingerprint_file(music / name), LoudnessMeasurement(-14.0, -1.0))

    settings = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    window = MainWindow(settings=settings, loudness_cache=cache, ffmpeg_path="ffmpeg")
    assert not hasattr(window, "synchronize_loudness_button")
    window.load_folder(music)
    assert sorted(p.name for p in music.iterdir()) == ["A.mp3", "B.mp3"]
    wait_for_analysis(window)
    assert "Cached 2" in window.loudness_count_label.text()
    assert window.loudness_current_label.text() == "Loudness: Ready"

    window.target_lufs.setValue(-12.0)
    assert not window.analysis_running
    window.close()
