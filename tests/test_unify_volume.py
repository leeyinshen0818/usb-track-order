"""Opt-in "Unify volume": plain copy by default, analysis only inside Copy to USB."""

import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from app.main_window import MainWindow, unmeasured_summary
from app.models.track import Track
from app.services.loudness import CommandResult, LoudnessCache
from app.services.usb_drives import UsbDrive
from app.services.usb_exporter import build_export_plan

QUIET = "    I:         -20.0 LUFS\n    Peak:      -10.0 dBFS\n"


def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def forbid(name: str):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"{name} must not be called")

    return fail


@pytest.fixture
def setup(tmp_path: Path, monkeypatch):
    """A music folder, a fake removable drive, and auto-accepted confirmation."""

    application()
    music = tmp_path / "music"
    usb = tmp_path / "usb"
    music.mkdir()
    usb.mkdir()
    for name, content in (("A.mp3", b"aaaa"), ("Bad.mp3", b"bb"), ("C.mp3", b"cccccc")):
        (music / name).write_bytes(content)
    drive = UsbDrive(usb, "E", "TEST", 10**9, 10**9, 1)
    monkeypatch.setattr("app.main_window.discover_removable_drives", lambda: [drive])

    def accept_start_copy(box: QMessageBox) -> int:
        for button in box.buttons():
            if box.buttonRole(button) == QMessageBox.AcceptRole:
                box._clicked = button
        return 0

    monkeypatch.setattr(QMessageBox, "exec", accept_start_copy)
    monkeypatch.setattr(
        QMessageBox, "clickedButton", lambda box: getattr(box, "_clicked", None)
    )
    return tmp_path, music, usb


def make_window(tmp_path: Path, **kwargs) -> MainWindow:
    settings = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    return MainWindow(
        settings=settings, loudness_cache=LoudnessCache(tmp_path / "c.json"), **kwargs
    )


def wait_until_idle(window: MainWindow, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    app = application()
    while window.export_running and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert not window.export_running and not window.analysis_running


def fake_analysis(command, cancellation) -> CommandResult:
    source = Path(command[command.index("-i") + 1])
    if source.name == "Bad.mp3":
        return CommandResult(1, stderr="Invalid data found when processing input")
    return CommandResult(0, stderr=QUIET)


def fake_gain(command, cancellation) -> CommandResult:
    source = Path(command[command.index("-i") + 1])
    volume = next(part for part in command if part.startswith("volume="))
    Path(command[-1]).write_bytes(source.read_bytes() + volume.encode())
    return CommandResult(0)


# --- checkbox ------------------------------------------------------------------


def test_unify_volume_is_off_by_default_and_hides_target(setup) -> None:
    tmp_path, _music, _usb = setup
    window = make_window(tmp_path)
    assert not window.unify_volume_checkbox.isChecked()
    assert window.target_lufs_container.isHidden()
    for removed in ("loudness_progress", "loudness_count_label", "cancel_loudness_button"):
        assert not hasattr(window, removed)
    window.unify_volume_checkbox.setChecked(True)
    assert not window.target_lufs_container.isHidden()
    window.close()


def test_unify_volume_setting_is_remembered(setup) -> None:
    tmp_path, _music, _usb = setup
    first = make_window(tmp_path)
    first.unify_volume_checkbox.setChecked(True)
    first.settings.sync()
    first.close()
    second = make_window(tmp_path)
    assert second.unify_volume_checkbox.isChecked()
    assert not second.target_lufs_container.isHidden()
    second.unify_volume_checkbox.setChecked(False)
    second.settings.sync()
    second.close()
    third = make_window(tmp_path)
    assert not third.unify_volume_checkbox.isChecked()
    third.close()


# --- unticked: never touch FFmpeg ---------------------------------------------------


def test_folder_load_never_analyzes_or_locates_ffmpeg(setup, monkeypatch) -> None:
    tmp_path, music, _usb = setup
    monkeypatch.setattr("app.main_window.discover_ffmpeg", forbid("discover_ffmpeg"))
    monkeypatch.setattr("app.services.loudness.run_command", forbid("run_command"))
    window = make_window(tmp_path)
    window.unify_volume_checkbox.setChecked(True)  # even when ticked
    window.load_folder(music)
    assert not window.analysis_running and window.analysis_thread is None
    assert window.apply_numbering_button.isEnabled()
    assert window.copy_to_usb_button.isEnabled()
    window.close()


def test_plain_copy_without_ffmpeg(setup, monkeypatch) -> None:
    tmp_path, music, usb = setup
    for target in (
        "app.main_window.discover_ffmpeg",
        "app.services.usb_exporter.discover_ffmpeg",
        "app.services.loudness.run_command",
        "app.services.usb_exporter.run_command",
    ):
        monkeypatch.setattr(target, forbid(target))
    window = make_window(tmp_path)
    window.load_folder(music)
    window.copy_to_usb()
    wait_until_idle(window)
    assert sorted(p.name for p in usb.iterdir()) == ["A.mp3", "Bad.mp3", "C.mp3"]
    for path in usb.iterdir():
        assert path.read_bytes() == (music / path.name).read_bytes()
    window.close()


# --- ticked: analysis inside Copy ---------------------------------------------------


def unified_window(tmp_path: Path, music: Path, monkeypatch, answer: bool):
    monkeypatch.setattr("app.services.loudness.run_command", fake_analysis)
    monkeypatch.setattr("app.services.usb_exporter.run_command", fake_gain)
    window = make_window(tmp_path, ffmpeg_path="ffmpeg")
    window.unify_volume_checkbox.setChecked(True)
    window.load_folder(music)
    asked: list[list[str]] = []

    def confirm(entries: list[str]) -> bool:
        asked.append(entries)
        return answer

    monkeypatch.setattr(window, "_confirm_unmeasured", confirm)
    return window, asked


def test_failed_track_continue_copies_it_at_original_volume_in_order(
    setup, monkeypatch
) -> None:
    tmp_path, music, usb = setup
    window, asked = unified_window(tmp_path, music, monkeypatch, answer=True)
    labels: list[str] = []
    window.export_count_label.setText = lambda text: labels.append(text)
    window.copy_to_usb()
    wait_until_idle(window)
    assert any(text.startswith("Analyzing volume") for text in labels)
    assert "Analyzing volume 3/3" in labels
    assert len(asked) == 1 and len(asked[0]) == 1
    assert asked[0][0].startswith("Bad.mp3 — Invalid data found")
    assert (usb / "Bad.mp3").read_bytes() == b"bb"
    assert (usb / "A.mp3").read_bytes() == b"aaaa" + b"volume=6.00dB"
    assert (usb / "C.mp3").read_bytes() == b"cccccc" + b"volume=6.00dB"
    window.close()


def test_failed_track_cancel_copies_nothing(setup, monkeypatch) -> None:
    tmp_path, music, usb = setup
    window, asked = unified_window(tmp_path, music, monkeypatch, answer=False)
    window.copy_to_usb()
    wait_until_idle(window)
    assert len(asked) == 1
    assert list(usb.iterdir()) == []
    assert window.copy_to_usb_button.isEnabled()
    window.close()


def test_no_dialog_when_every_track_is_measured(setup, monkeypatch) -> None:
    tmp_path, music, usb = setup
    (music / "Bad.mp3").unlink()
    window, asked = unified_window(tmp_path, music, monkeypatch, answer=False)
    window.copy_to_usb()
    wait_until_idle(window)
    assert asked == []
    assert sorted(p.name for p in usb.iterdir()) == ["A.mp3", "C.mp3"]
    window.close()


@pytest.mark.parametrize("answer", [True, False])
def test_missing_ffmpeg_lists_all_tracks_once(setup, monkeypatch, answer) -> None:
    tmp_path, music, usb = setup
    monkeypatch.setattr("app.main_window.discover_ffmpeg", lambda: None)
    monkeypatch.setattr("app.services.loudness.run_command", forbid("run_command"))
    window = make_window(tmp_path)
    window.unify_volume_checkbox.setChecked(True)
    window.load_folder(music)
    asked: list[list[str]] = []
    monkeypatch.setattr(
        window, "_confirm_unmeasured", lambda entries: asked.append(entries) or answer
    )
    window.copy_to_usb()
    wait_until_idle(window)
    assert len(asked) == 1
    assert [entry.split(" — ")[0] for entry in asked[0]] == ["A.mp3", "Bad.mp3", "C.mp3"]
    copied = sorted(p.name for p in usb.iterdir())
    assert copied == (["A.mp3", "Bad.mp3", "C.mp3"] if answer else [])
    for path in usb.iterdir():
        assert path.read_bytes() == (music / path.name).read_bytes()
    window.close()


def test_cancel_during_analysis_copies_nothing(setup, monkeypatch) -> None:
    tmp_path, music, usb = setup
    started = threading.Event()

    def blocking(command, cancellation) -> CommandResult:
        started.set()
        while not cancellation.cancelled:
            time.sleep(0.01)
        return CommandResult(-1, cancelled=True)

    monkeypatch.setattr("app.services.loudness.run_command", blocking)
    window = make_window(tmp_path, ffmpeg_path="ffmpeg")
    window.unify_volume_checkbox.setChecked(True)
    window.load_folder(music)
    monkeypatch.setattr(window, "_confirm_unmeasured", forbid("_confirm_unmeasured"))
    window.copy_to_usb()
    assert window.export_running and window.analysis_running
    assert not window.apply_numbering_button.isEnabled()
    assert not window.undo_rename_button.isEnabled()
    assert window.cancel_copy_button.isEnabled()
    assert started.wait(5)
    window.cancel_export()
    wait_until_idle(window)
    assert list(usb.iterdir()) == []
    assert window.export_current_label.text() == "Current: Copy cancelled"
    window.close()


# --- pure helpers ---------------------------------------------------------------------


def test_unmeasured_summary_lists_and_truncates() -> None:
    entries = [f"T{i}.mp3 — bad" for i in range(17)]
    text = unmeasured_summary(entries)
    assert text.startswith("17 track(s) could not be analyzed")
    assert "T14.mp3 — bad" in text and "T15.mp3" not in text
    assert text.endswith("… and 2 more")


def test_allow_unmeasured_plans_plain_copy_in_place(tmp_path: Path) -> None:
    from app.services.loudness import LoudnessMeasurement, fingerprint_file

    source = tmp_path / "source"
    usb = tmp_path / "usb"
    source.mkdir()
    usb.mkdir()
    tracks = []
    for index, name in enumerate(("A.mp3", "B.mp3", "C.mp3"), start=1):
        (source / name).write_bytes(bytes([index]) * index)
        tracks.append(Track.from_path(source / name))
    cache = LoudnessCache(tmp_path / "state.json")
    for track in (tracks[0], tracks[2]):
        cache.put(fingerprint_file(track.original_path), LoudnessMeasurement(-20, -10))
    plan = build_export_plan(
        tracks, usb, available_bytes=100, target_lufs=-14.0, state_cache=cache,
        analysis_failures={fingerprint_file(tracks[1].original_path): "broken"},
        ffmpeg_path="ffmpeg", allow_unmeasured=True,
    )
    assert [t.filename for t in plan.tracks] == ["A.mp3", "B.mp3", "C.mp3"]
    assert [t.gain_db for t in plan.tracks] == [6.0, None, 6.0]
    assert plan.unmeasured == ("B.mp3 — broken",)
