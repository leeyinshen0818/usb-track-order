import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from app.models.track import Track
from app.paths import data_dir
from app.services.filename_numbering import (
    build_apply_numbering_plan,
    has_canonical_numbering,
    strip_canonical_numbering,
)
from app.services.rename_journal import (
    RenameJournal,
    load_receipt,
    receipt_belongs_to,
    recover_interrupted_rename,
    save_receipt,
)
from app.services.safe_renamer import (
    RenameExecutionError,
    RenameValidationError,
    SafeRenamer,
)
from app.services.track_flags import (
    FolderHistory,
    find_new_tracks,
    find_possible_duplicates,
    has_copy_suffix,
)
from app.services.track_sorting import SortDirection, SortField, sort_tracks
from app.widgets.track_list import DUPLICATE_MARKER, TrackListWidget


def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def fake(name: str) -> Track:
    return Track(Path("C:/Music") / name, name, extension=Path(name).suffix)


def names(tracks) -> list[str]:
    return [t.original_filename for t in tracks]


def make(folder: Path, file_names: list[str]) -> list[Track]:
    tracks = []
    for index, name in enumerate(file_names):
        path = folder / name
        path.write_bytes(f"song-{index}-{name}".encode())
        tracks.append(Track.from_path(path))
    return tracks


# --- 1. numbering detection protects real names ------------------------------


@pytest.mark.parametrize(
    "name", ["5566 - x.mp3", "2NE1 - x.mp3", "7 rings.mp3", "1999 - Prince.mp3", "0001 x.mp3"]
)
def test_real_numeric_names_are_not_numbering(name: str) -> None:
    assert strip_canonical_numbering(name) == name
    assert not has_canonical_numbering(name)


@pytest.mark.parametrize(
    ("name", "base"),
    [
        ("001 5566 - x.mp3", "5566 - x.mp3"),
        ("012 2NE1 - x.mp3", "2NE1 - x.mp3"),
        ("003 7 rings.mp3", "7 rings.mp3"),
        ("1000 Song.mp3", "Song.mp3"),
    ],
)
def test_app_numbering_is_stripped_down_to_real_name(name: str, base: str) -> None:
    assert strip_canonical_numbering(name) == base


def test_filename_sort_ignores_app_numbering_but_not_real_names() -> None:
    tracks = [fake("001 Zedd - x.mp3"), fake("002 5566 - x.mp3"), fake("003 7 rings.mp3"),
              fake("2NE1 - x.mp3")]
    assert names(sort_tracks(tracks, SortField.FILENAME, SortDirection.ASCENDING)) == [
        "2NE1 - x.mp3",  # number-aware: 2 < 7 < 5566
        "003 7 rings.mp3",
        "002 5566 - x.mp3",
        "001 Zedd - x.mp3",
    ]


# --- 2. apply numbering replaces existing numbering --------------------------


def test_apply_numbering_replaces_existing_numbers_after_reorder(tmp_path: Path) -> None:
    tracks = make(tmp_path, ["001 A.mp3", "002 B.mp3", "003 5566 - C.mp3", "D.mp3"])
    reordered = [tracks[3], tracks[2], tracks[0], tracks[1]]
    SafeRenamer().execute(build_apply_numbering_plan(reordered))
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "001 D.mp3",
        "002 5566 - C.mp3",
        "003 A.mp3",
        "004 B.mp3",
    ]


# --- 3. pinyin + latin shared key ---------------------------------------------


def test_chinese_and_latin_share_one_key_space() -> None:
    tracks = [fake("周杰倫 - 晴天.mp3"), fake("Zebra.mp3"), fake("Adele.mp3"),
              fake("林俊杰.mp3"), fake("zz top.mp3")]
    assert names(sort_tracks(tracks, SortField.FILENAME, SortDirection.ASCENDING)) == [
        "Adele.mp3",
        "林俊杰.mp3",
        "Zebra.mp3",
        "周杰倫 - 晴天.mp3",
        "zz top.mp3",
    ]


def test_traditional_and_simplified_sort_together() -> None:
    tracks = [fake("周杰倫 - 晴天.mp3"), fake("Zhang.mp3"), fake("周杰伦 - 稻香.mp3"),
              fake("Zhu.mp3")]
    ordered = names(sort_tracks(tracks, SortField.FILENAME, SortDirection.ASCENDING))
    assert ordered.index("周杰倫 - 晴天.mp3") + 1 == ordered.index("周杰伦 - 稻香.mp3") or (
        ordered.index("周杰伦 - 稻香.mp3") + 1 == ordered.index("周杰倫 - 晴天.mp3")
    )
    assert ordered[0] == "Zhang.mp3" and ordered[-1] == "Zhu.mp3"


def test_sort_is_number_aware_in_pinyin_and_latin() -> None:
    tracks = [fake("情歌 10.mp3"), fake("情歌 2.mp3"), fake("Track 10.mp3"), fake("Track 9.mp3")]
    assert names(sort_tracks(tracks, SortField.FILENAME, SortDirection.ASCENDING)) == [
        "情歌 2.mp3",
        "情歌 10.mp3",
        "Track 9.mp3",
        "Track 10.mp3",
    ]


# --- 4. new-song detection ------------------------------------------------------


def test_new_tracks_are_unnumbered_or_added_since_last_load(tmp_path: Path) -> None:
    history = FolderHistory(tmp_path / "history.json")
    folder = tmp_path / "music"
    assert history.previous_names(folder) is None
    history.record(folder, [fake("001 Old.mp3"), fake("Other.mp3")])
    previous = history.previous_names(folder)

    tracks = [fake("002 Old.mp3"), fake("003 Added.mp3"), fake("Plain.mp3")]
    new = find_new_tracks(tracks, previous)
    assert {p.name for p in new} == {"003 Added.mp3", "Plain.mp3"}
    # First load of a folder: only unnumbered tracks count as new.
    assert {p.name for p in find_new_tracks(tracks, None)} == {"Plain.mp3"}


# --- 5. duplicates ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["Song - Copy.mp3", "Song - Copy (2).mp3", "Song (1).mp3", "001 Song - 副本.mp3"]
)
def test_copy_suffixes_are_detected(name: str) -> None:
    assert has_copy_suffix(name)


@pytest.mark.parametrize("name", ["Song.mp3", "Live (Remix).mp3", "Copycat.mp3"])
def test_ordinary_names_are_not_copies(name: str) -> None:
    assert not has_copy_suffix(name)


def test_duplicates_by_normalized_artist_title() -> None:
    tracks = [
        fake("001 Jay Chou - Qing Tian.mp3"),
        fake("jay chou – qing tian.flac"),
        fake("周杰倫 - 晴天.mp3"),
        fake("002 周杰伦-晴天.m4a"),
        fake("Unique.mp3"),
        fake("Other (1).mp3"),
    ]
    flagged = find_possible_duplicates(tracks)
    assert {p.name for p in flagged} == {
        "001 Jay Chou - Qing Tian.mp3",
        "jay chou – qing tian.flac",
        "周杰倫 - 晴天.mp3",
        "002 周杰伦-晴天.m4a",
        "Other (1).mp3",
    }


# --- 6. multi-select moves and drag/drop on the widget -----------------------------


def test_widget_multi_select_moves_and_drop() -> None:
    app()
    widget = TrackListWidget()
    tracks = [fake(f"{c}.mp3") for c in "ABCDE"]
    widget.set_tracks(tracks, "")
    widget._select_rows([1, 3])
    widget.move_selected_top()
    assert names(widget.tracks) == ["B.mp3", "D.mp3", "A.mp3", "C.mp3", "E.mp3"]
    assert widget.selected_rows() == [0, 1]
    widget.move_selected_bottom()
    assert names(widget.tracks)[-2:] == ["B.mp3", "D.mp3"]
    widget.move_selected_up()
    assert names(widget.tracks) == ["A.mp3", "C.mp3", "B.mp3", "D.mp3", "E.mp3"]
    widget.move_selected_down()
    assert names(widget.tracks) == ["A.mp3", "C.mp3", "E.mp3", "B.mp3", "D.mp3"]
    widget.table.rows_dropped.emit([3, 4], 0)
    assert names(widget.tracks) == ["B.mp3", "D.mp3", "A.mp3", "C.mp3", "E.mp3"]
    assert widget.selected_rows() == [0, 1]


def test_widget_shows_highlight_and_duplicate_marker() -> None:
    app()
    widget = TrackListWidget()
    tracks = [fake("001 A.mp3"), fake("B.mp3")]
    widget.set_tracks(tracks, "")
    widget.set_flags({tracks[1].original_path}, {tracks[0].original_path: "copy"})
    model = widget.model
    assert model.data(model.index(0, 0)) == f"001 {DUPLICATE_MARKER}"
    assert model.data(model.index(0, 1), Qt.BackgroundRole) is None
    assert model.data(model.index(1, 1), Qt.BackgroundRole) is not None
    assert "Possible duplicate" in model.data(model.index(0, 1), Qt.ToolTipRole)


# --- 7. journal + persisted undo -----------------------------------------------------


class Crash(BaseException):
    """Simulates the process dying mid-rename (not caught as OSError)."""


def crashing_rename(after: int):
    count = 0

    def rename(source: Path, target: Path) -> None:
        nonlocal count
        if count == after:
            raise Crash
        count += 1
        source.rename(target)

    return rename


def test_journal_is_written_before_renames_and_removed_after(tmp_path: Path) -> None:
    folder = tmp_path / "music"
    folder.mkdir()
    journal = tmp_path / "journal.json"
    tracks = make(folder, ["A.mp3", "B.mp3"])
    seen = []

    def rename(source: Path, target: Path) -> None:
        seen.append(journal.exists())
        source.rename(target)

    SafeRenamer(rename=rename, journal_path=journal).execute(
        build_apply_numbering_plan(tracks)
    )
    assert seen and all(seen)
    assert not journal.exists()


def test_completed_rollback_removes_journal(tmp_path: Path) -> None:
    folder = tmp_path / "music"
    folder.mkdir()
    journal = tmp_path / "journal.json"
    tracks = make(folder, ["A.mp3", "B.mp3"])
    calls = 0

    def flaky(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("locked")
        source.rename(target)

    with pytest.raises(RenameExecutionError):
        SafeRenamer(rename=flaky, journal_path=journal).execute(
            build_apply_numbering_plan(tracks)
        )
    assert not journal.exists()
    assert sorted(p.name for p in folder.iterdir()) == ["A.mp3", "B.mp3"]


@pytest.mark.parametrize("crash_after", [1, 2, 3])
def test_crash_mid_rename_is_rolled_back_on_next_start(
    tmp_path: Path, crash_after: int
) -> None:
    folder = tmp_path / "music"
    folder.mkdir()
    journal = tmp_path / "journal.json"
    tracks = make(folder, ["A.mp3", "B.mp3"])
    contents = {t.original_filename: t.original_path.read_bytes() for t in tracks}
    # Swap names A <-> B: the hardest case for recovery.
    from app.services.safe_renamer import RenameAction

    plan = [
        RenameAction(tracks[0].original_path, tracks[1].original_path),
        RenameAction(tracks[1].original_path, tracks[0].original_path),
    ]
    with pytest.raises(Crash):
        SafeRenamer(rename=crashing_rename(crash_after), journal_path=journal).execute(plan)
    assert journal.exists()

    report = recover_interrupted_rename(journal)
    assert report is not None and report.complete
    assert not journal.exists()
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == contents


def test_recovery_without_journal_is_noop(tmp_path: Path) -> None:
    assert recover_interrupted_rename(tmp_path / "missing.json") is None


def test_recovery_reports_missing_file_and_keeps_failed_journal(tmp_path: Path) -> None:
    folder = tmp_path / "music"
    folder.mkdir()
    journal_path = tmp_path / "journal.json"
    from app.services.rename_journal import JournalEntry

    RenameJournal(journal_path).write(
        [JournalEntry(folder / "A.mp3", folder / ".tmp.mp3", folder / "001 A.mp3", 1, 5)]
    )
    report = recover_interrupted_rename(journal_path)
    assert report is not None and not report.complete
    assert "could not be found" in report.problems[0]
    assert not journal_path.exists()
    assert list(tmp_path.glob("journal.failed-*.json"))


def test_undo_receipt_survives_restart(tmp_path: Path) -> None:
    folder = tmp_path / "music"
    folder.mkdir()
    receipt_path = tmp_path / "last_rename.json"
    tracks = make(folder, ["A.mp3", "B.mp3"])
    receipt = SafeRenamer().execute(build_apply_numbering_plan(tracks))
    assert save_receipt(receipt_path, receipt)

    restored = load_receipt(receipt_path)
    assert restored is not None and receipt_belongs_to(restored, folder)
    assert not receipt_belongs_to(restored, tmp_path)
    SafeRenamer().execute(restored.undo_actions())
    assert sorted(p.name for p in folder.iterdir()) == ["A.mp3", "B.mp3"]


def test_persisted_undo_is_refused_if_file_changed(tmp_path: Path) -> None:
    folder = tmp_path / "music"
    folder.mkdir()
    receipt_path = tmp_path / "last_rename.json"
    tracks = make(folder, ["A.mp3"])
    receipt = SafeRenamer().execute(build_apply_numbering_plan(tracks))
    save_receipt(receipt_path, receipt)
    (folder / "001 A.mp3").write_bytes(b"edited elsewhere")

    with pytest.raises(RenameValidationError, match="changed outside"):
        SafeRenamer().validate(load_receipt(receipt_path).undo_actions())
    assert sorted(p.name for p in folder.iterdir()) == ["001 A.mp3"]


# --- main window integration ------------------------------------------------------------


def make_window(tmp_path: Path, monkeypatch):
    from app.main_window import MainWindow
    from app.services.loudness import LoudnessCache

    app()
    monkeypatch.setattr("app.main_window.discover_removable_drives", lambda: [])
    settings = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    return MainWindow(settings=settings, loudness_cache=LoudnessCache(tmp_path / "c.json"))


def test_window_notices_and_highlight(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "music"
    folder.mkdir()
    make(folder, ["001 A.mp3", "B.mp3", "B - Copy.mp3", ".__usb_track_order_tmp_ab.mp3"])
    window = make_window(tmp_path, monkeypatch)
    window.load_folder(folder)
    text = window.notice_label.text()
    assert "Interrupted rename detected: 1 files" in text
    assert "2 possible duplicates" in text
    assert (folder / ".__usb_track_order_tmp_ab.mp3").exists()
    model = window.track_list.model
    rows = {t.original_filename: i for i, t in enumerate(model.tracks)}
    assert model.data(model.index(rows["001 A.mp3"], 1), Qt.BackgroundRole) is None
    assert model.data(model.index(rows["B.mp3"], 1), Qt.BackgroundRole) is not None
    window.close()


def test_window_undo_survives_restart(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "music"
    folder.mkdir()
    make(folder, ["A.mp3", "B.mp3"])
    monkeypatch.setattr(
        "app.main_window.RenamePreviewDialog.confirm", lambda **_kwargs: True
    )
    first = make_window(tmp_path, monkeypatch)
    first.load_folder(folder)
    first.apply_numbering()
    assert (data_dir() / "last_rename.json").exists()
    assert not (data_dir() / "rename_journal.json").exists()
    first.close()

    second = make_window(tmp_path, monkeypatch)
    second.load_folder(folder)
    assert second.undo_rename_button.isEnabled()
    second.undo_last_rename()
    assert sorted(p.name for p in folder.iterdir()) == ["A.mp3", "B.mp3"]
    assert not (data_dir() / "last_rename.json").exists()
    second.close()


def test_window_recovers_journal_on_startup(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "music"
    folder.mkdir()
    tracks = make(folder, ["A.mp3"])
    with pytest.raises(Crash):
        SafeRenamer(
            rename=crashing_rename(1), journal_path=data_dir() / "rename_journal.json"
        ).execute(build_apply_numbering_plan(tracks))
    shown = []
    monkeypatch.setattr(
        "app.main_window.QMessageBox.information",
        lambda _parent, title, text: shown.append((title, text)),
    )
    window = make_window(tmp_path, monkeypatch)
    app().processEvents()
    assert shown and shown[0][0] == "Interrupted Rename Restored"
    assert "Restored to original names: 1" in shown[0][1]
    assert sorted(p.name for p in folder.iterdir()) == ["A.mp3"]
    window.close()
