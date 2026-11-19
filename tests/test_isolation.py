import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.main_window import MainWindow, default_settings
from app.paths import data_dir
from app.services.loudness import LoudnessCache, LoudnessMeasurement
from conftest import REAL_DATA_DIR, is_inside


def test_default_paths_never_point_at_real_data_dir(
    isolated_app_data: Path, monkeypatch
) -> None:
    QApplication.instance() or QApplication([])
    monkeypatch.setattr("app.main_window.discover_removable_drives", lambda: [])

    cache = LoudnessCache()
    settings = default_settings()
    window = MainWindow()
    paths = [
        data_dir(),
        cache.path,
        window.loudness_cache.path,
        Path(settings.fileName()),
        Path(window.settings.fileName()),
    ]
    window.close()
    for path in paths:
        assert not is_inside(path, REAL_DATA_DIR), path
        assert is_inside(path, isolated_app_data.resolve()), path


def test_default_cache_writes_only_into_tmp(isolated_app_data: Path) -> None:
    cache = LoudnessCache()
    cache.put("1:a", LoudnessMeasurement(-14.0, -1.0))
    assert cache.save()
    assert cache.path.is_file()
    assert is_inside(cache.path, isolated_app_data.resolve())
    assert not is_inside(cache.path, REAL_DATA_DIR)
