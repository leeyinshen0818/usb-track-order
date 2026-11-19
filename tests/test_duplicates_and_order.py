"""Duplicate tiers, Recycle Bin removal, sort header glyphs, unapplied order."""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QSettings, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QHeaderView, QMessageBox

import app.main_window as main_window
from app.models.track import Track
from app.services import recycle_bin
from app.services.filename_numbering import filenames_follow_order
from app.services.loudness import LoudnessCache
from app.services.track_flags import DuplicateTier, find_duplicate_groups
from app.services.usb_drives import UsbDrive
from app.widgets.duplicate_review import DuplicateReviewDialog, preselected_paths
from app.widgets.track_list import (
    FILENAME_COLUMN,
    MODIFIED_COLUMN,
    ORDER_COLUMN,
    SORT_GLYPH_ASCENDING,
    SORT_GLYPH_DESCENDING,
    SORT_GLYPH_INACTIVE,
    SORT_TOOLTIP,
    TrackListWidget,
)


def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def write(folder: Path, name: str, content: bytes, mtime: float | None = None) -> Track:
    path = folder / name
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return Track.from_path(path)


def names(tracks) -> list[str]:
    return [t.original_filename for t in tracks]


# --- tier classification ------------------------------------------------------


def test_identical_content_is_likely_and_later_copy_preselected(tmp_path: Path) -> None:
    older = write(tmp_path, "Song A.mp3", b"same audio", mtime=1_000_000)
    newer = write(tmp_path, "Totally Different.mp3", b"same audio", mtime=2_000_000)
    other = write(tmp_path, "Other.mp3", b"other audi")  # same size, other content
    groups = find_duplicate_groups([newer, older, other])
    assert len(groups) == 1
    group = groups[0]
    assert group.tier == DuplicateTier.LIKELY
    assert set(names(group.tracks)) == {"Song A.mp3", "Totally Different.mp3"}
    assert group.preselected == {newer.original_path}


def test_copy_suffix_with_existing_base_is_likely(tmp_path: Path) -> None:
    base = write(tmp_path, "003 Song.mp3", b"one")
    copy = write(tmp_path, "Song - Copy.mp3", b"second file")
    copy_two = write(tmp_path, "Song (1).mp3", b"third file!!")
    groups = find_duplicate_groups([copy, base, copy_two])
    assert [g.tier for g in groups] == [DuplicateTier.LIKELY]
    assert groups[0].preselected == {copy.original_path, copy_two.original_path}


def test_copy_suffix_without_base_or_other_extension_is_not_flagged(tmp_path: Path) -> None:
    lonely = write(tmp_path, "Lonely (1).mp3", b"a")
    flac = write(tmp_path, "Song.flac", b"bb")
    copy = write(tmp_path, "Song - Copy.mp3", b"ccc")
    groups = find_duplicate_groups([lonely, flac, copy])
    assert all(g.tier == DuplicateTier.SIMILAR for g in groups)
    assert all(not g.preselected for g in groups)
    assert lonely.original_path not in {t.original_path for g in groups for t in g.tracks}


def test_same_artist_title_different_content_is_similar_name(tmp_path: Path) -> None:
    traditional = write(tmp_path, "周杰倫 - 晴天.mp3", b"version one")
    simplified = write(tmp_path, "002 周杰伦-晴天.m4a", b"version two, longer")
    groups = find_duplicate_groups([traditional, simplified])
    assert [g.tier for g in groups] == [DuplicateTier.SIMILAR]
    assert groups[0].preselected == frozenset()


def test_homophones_are_not_duplicates(tmp_path: Path) -> None:
    first = write(tmp_path, "想你.mp3", b"song one")
    second = write(tmp_path, "像你.mp3", b"song two!")
    assert find_duplicate_groups([first, second]) == []


def test_review_preselects_only_likely_extra_copies(tmp_path: Path) -> None:
    app()
    base = write(tmp_path, "Song.mp3", b"one")
    copy = write(tmp_path, "Song - Copy.mp3", b"two!")
    similar_a = write(tmp_path, "周杰倫 - 晴天.mp3", b"three")
    similar_b = write(tmp_path, "周杰伦 - 晴天.flac", b"four!!")
    groups = find_duplicate_groups([base, copy, similar_a, similar_b])
    assert {g.tier for g in groups} == {DuplicateTier.LIKELY, DuplicateTier.SIMILAR}
    assert preselected_paths(groups) == {copy.original_path}
    dialog = DuplicateReviewDialog(groups)
    assert dialog.checked_paths() == [copy.original_path]
    assert dialog.remove_button.isEnabled()
    dialog.close()


# --- Recycle Bin ----------------------------------------------------------------


@pytest.mark.parametrize("returned, expected", [(True, True), (False, False),
                                                ((True, "x"), True), ((False, ""), False)])
def test_move_to_trash_wrapper(monkeypatch, returned, expected) -> None:
    monkeypatch.setattr(recycle_bin.QFile, "moveToTrash", lambda _path: returned)
    assert recycle_bin.move_to_trash(Path("C:/x.mp3")) is expected


def make_window(tmp_path: Path, monkeypatch, drives=()):
    app()
    monkeypatch.setattr(main_window, "discover_removable_drives", lambda: list(drives))
    settings = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    return main_window.MainWindow(
        settings=settings, loudness_cache=LoudnessCache(tmp_path / "c.json")
    )


def music_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "music"
    folder.mkdir()
    for name, content in (("A.mp3", b"a"), ("A - Copy.mp3", b"aa"), ("B.mp3", b"bbb"),
                          ("C.mp3", b"cccc")):
        (folder / name).write_bytes(content)
    return folder


def test_recycle_failure_deletes_nothing_and_explains(tmp_path: Path, monkeypatch) -> None:
    folder = music_folder(tmp_path)
    window = make_window(tmp_path, monkeypatch)
    window.load_folder(folder)
    before = names(window.track_list.tracks)
    calls: list[Path] = []
    monkeypatch.setattr(main_window, "move_to_trash", lambda p: calls.append(p) or False)
    monkeypatch.setattr(window, "_confirm_recycle", lambda count: True)
    warnings: list[str] = []
    monkeypatch.setattr(
        main_window.QMessageBox, "warning", lambda _p, _t, text: warnings.append(text)
    )
    window.move_tracks_to_recycle_bin([folder / "A - Copy.mp3", folder / "B.mp3"])
    assert len(calls) == 1  # stops at the first failure
    assert warnings == [main_window.NO_RECYCLE_BIN_MESSAGE]
    assert names(window.track_list.tracks) == before
    assert sorted(p.name for p in folder.iterdir()) == sorted(before)
    window.close()


def test_recycle_success_removes_rows_and_renumbers(tmp_path: Path, monkeypatch) -> None:
    folder = music_folder(tmp_path)
    window = make_window(tmp_path, monkeypatch)
    window.load_folder(folder)
    assert "2 possible duplicates" in window.notice_label.text()
    # Fake Recycle Bin: never touch the real one from tests.
    monkeypatch.setattr(main_window, "move_to_trash", lambda p: True)
    monkeypatch.setattr(window, "_confirm_recycle", lambda count: count == 1)
    window.move_tracks_to_recycle_bin([folder / "A.mp3"])
    tracks = names(window.track_list.tracks)
    assert tracks == ["A - Copy.mp3", "B.mp3", "C.mp3"]
    model = window.track_list.model
    assert [model.data(model.index(r, ORDER_COLUMN)) for r in range(3)] == ["001", "002", "003"]
    assert window.count_label.text() == "3 songs"
    assert window.notice_label.text() == ""
    window.close()


def test_recycle_confirm_defaults_to_cancel(tmp_path: Path, monkeypatch) -> None:
    window = make_window(tmp_path, monkeypatch)
    seen = {}

    def no_click(box: QMessageBox) -> int:
        seen["text"] = box.text()
        seen["default"] = box.defaultButton() is box.button(QMessageBox.Cancel)
        return 0

    monkeypatch.setattr(QMessageBox, "exec", no_click)
    assert window._confirm_recycle(2) is False
    assert seen == {"text": "Move 2 song(s) to the Recycle Bin?", "default": True}
    window.close()


def test_delete_key_requests_removal_of_selection(tmp_path: Path, monkeypatch) -> None:
    folder = music_folder(tmp_path)
    window = make_window(tmp_path, monkeypatch)
    window.load_folder(folder)
    requested: list[list[Path]] = []
    monkeypatch.setattr(window, "move_tracks_to_recycle_bin", requested.append)
    window.track_list._select_rows([2])
    table = window.track_list.table
    QApplication.sendEvent(table, QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
    assert requested == [[window.track_list.tracks[2].original_path]]
    window.track_list.set_order_changes_enabled(False)
    QApplication.sendEvent(table, QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
    assert len(requested) == 1
    window.close()


# --- sort header ----------------------------------------------------------------


def test_header_glyphs_follow_sort_state(tmp_path: Path, monkeypatch) -> None:
    folder = music_folder(tmp_path)
    window = make_window(tmp_path, monkeypatch)
    window.load_folder(folder)
    header = window.track_list.table.horizontalHeader()
    model = window.track_list.model
    assert header.sort_glyph(ORDER_COLUMN) is None
    assert header.sort_glyph(FILENAME_COLUMN) == SORT_GLYPH_INACTIVE
    assert model.headerData(FILENAME_COLUMN, Qt.Horizontal, Qt.ToolTipRole) == SORT_TOOLTIP
    assert model.headerData(ORDER_COLUMN, Qt.Horizontal, Qt.ToolTipRole) is None
    assert not header.isSortIndicatorShown()

    window.sort_by_column(FILENAME_COLUMN)
    assert header.sort_glyph(FILENAME_COLUMN) == SORT_GLYPH_ASCENDING
    assert header.sort_glyph(MODIFIED_COLUMN) == SORT_GLYPH_INACTIVE
    window.sort_by_column(FILENAME_COLUMN)
    assert header.sort_glyph(FILENAME_COLUMN) == SORT_GLYPH_DESCENDING
    window.mark_custom_order()
    assert header.sort_glyph(FILENAME_COLUMN) == SORT_GLYPH_INACTIVE
    assert window.sort_label.text() == "Sort: Custom Order"
    window.close()


def test_header_reserves_room_for_the_glyph() -> None:
    app()
    widget = TrackListWidget()
    header = widget.table.horizontalHeader()
    for column in (ORDER_COLUMN, MODIFIED_COLUMN):
        plain = QHeaderView.sectionSizeFromContents(header, column).width()
        reserved = header.sectionSizeFromContents(column).width()
        assert (reserved > plain) == (column == MODIFIED_COLUMN)


# --- unapplied order ----------------------------------------------------------------


def test_filenames_follow_order() -> None:
    def fake(name):
        return Track(Path("C:/M") / name, name)

    assert filenames_follow_order([fake("A.mp3"), fake("B.mp3"), fake("Track 10.mp3")])
    assert filenames_follow_order([fake("001 Z.mp3"), fake("002 A.mp3"), fake("New.mp3")])
    assert not filenames_follow_order([fake("B.mp3"), fake("A.mp3")])
    assert not filenames_follow_order([fake("002 A.mp3"), fake("001 Z.mp3")])


def test_unapplied_order_state_and_copy_confirm(tmp_path: Path, monkeypatch) -> None:
    folder = music_folder(tmp_path)
    usb = tmp_path / "usb"
    usb.mkdir()
    drive = UsbDrive(usb, "E", "TEST", 10**9, 10**9, 1)
    window = make_window(tmp_path, monkeypatch, drives=[drive])
    window.load_folder(folder)
    button = window.apply_numbering_button
    assert window.status_note.text() == main_window.DEFAULT_STATUS_NOTE
    assert not button.property("softAccent")

    asked: list[bool] = []
    monkeypatch.setattr(window, "_confirm_unapplied_order", lambda: asked.append(True) or False)
    monkeypatch.setattr(QMessageBox, "exec", lambda box: 0)  # Start Copy: not clicked
    window.copy_to_usb()
    assert asked == []  # filenames already follow the list order

    window.track_list._select_rows([0])
    window.track_list.move_selected_bottom()
    assert window.status_note.text() == main_window.ORDER_CHANGED_NOTE
    assert button.property("softAccent") is True
    window.copy_to_usb()
    assert asked == [True]
    assert list(usb.iterdir()) == []

    monkeypatch.setattr(main_window.RenamePreviewDialog, "confirm", lambda **_kw: True)
    window.apply_numbering()
    assert window.status_note.text() == main_window.DEFAULT_STATUS_NOTE
    assert not button.property("softAccent")
    window.close()
