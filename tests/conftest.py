"""Keep every test away from the real per-user data directory."""

from __future__ import annotations

import gc
import os
from pathlib import Path

import pytest

from app.paths import DATA_DIR_ENV, default_data_dir

# Captured before any fixture redirects the environment.
REAL_DATA_DIR = default_data_dir().resolve()


def _snapshot(directory: Path) -> dict[str, tuple[int, int]]:
    if not directory.is_dir():
        return {}
    return {
        entry.name: (entry.stat().st_size, entry.stat().st_mtime_ns)
        for entry in directory.iterdir()
    }


_REAL_SNAPSHOT = _snapshot(REAL_DATA_DIR)


@pytest.fixture(autouse=True)
def isolated_app_data(tmp_path_factory, monkeypatch) -> Path:
    """Point the data dir override and LOCALAPPDATA/XDG roots at a tmp dir."""

    root = tmp_path_factory.mktemp("appdata")
    monkeypatch.setenv(DATA_DIR_ENV, str(root / "USB Track Order"))
    monkeypatch.setenv("LOCALAPPDATA", str(root / "LocalAppData"))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "xdg-state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "xdg-data"))
    return root


@pytest.fixture(autouse=True)
def collect_garbage_on_main_thread():
    """Collect each test's leftover Qt wrappers on the GUI thread.

    Otherwise a later test's worker thread may trigger the cyclic garbage
    collector and destroy those Qt objects off the GUI thread (access violation).
    """

    yield
    gc.collect()


def pytest_sessionfinish(session, exitstatus) -> None:
    """Fail the run if anything changed in the real data directory."""

    after = _snapshot(REAL_DATA_DIR)
    if after != _REAL_SNAPSHOT:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        print(
            f"\nERROR: tests modified the real data directory {REAL_DATA_DIR}:\n"
            f"before={_REAL_SNAPSHOT}\nafter={after}"
        )


def is_inside(path: Path, directory: Path) -> bool:
    try:
        Path(os.path.abspath(path)).resolve().relative_to(directory)
    except ValueError:
        return False
    return True
