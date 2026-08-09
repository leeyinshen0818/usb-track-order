import os
from pathlib import Path

from app.models.track import Track
from app.services.track_sorting import (
    SortDirection,
    SortField,
    SortState,
    next_sort_state,
    sort_tracks,
)


def track(name: str, *, size: int = 0, modified: float = 0) -> Track:
    return Track(
        original_path=Path("C:/Music") / name,
        original_filename=name,
        extension=Path(name).suffix,
        size_bytes=size,
        modified_time=modified,
    )


def filenames(tracks: list[Track]) -> list[str]:
    return [item.original_filename for item in tracks]


def test_filename_ascending_and_natural_sort() -> None:
    tracks = [track("Song 10.mp3"), track("Song 2.mp3"), track("Song 1.mp3")]
    assert filenames(sort_tracks(tracks, SortField.FILENAME, SortDirection.ASCENDING)) == [
        "Song 1.mp3",
        "Song 2.mp3",
        "Song 10.mp3",
    ]


def test_filename_descending() -> None:
    tracks = [track("Apple.mp3"), track("Moon.mp3"), track("Zebra.mp3")]
    assert filenames(sort_tracks(tracks, SortField.FILENAME, SortDirection.DESCENDING)) == [
        "Zebra.mp3",
        "Moon.mp3",
        "Apple.mp3",
    ]


def test_filename_sort_ignores_canonical_numbering() -> None:
    tracks = [track("001 Zebra.mp3"), track("002 Apple.mp3"), track("003 Moon.mp3")]
    assert filenames(sort_tracks(tracks, SortField.FILENAME, SortDirection.ASCENDING)) == [
        "002 Apple.mp3",
        "003 Moon.mp3",
        "001 Zebra.mp3",
    ]


def test_filename_sort_uses_pinyin_for_chinese_first_character() -> None:
    tracks = [
        track("周杰伦 - 晴天.mp3"),
        track("A-Lin - 失恋无罪.mp3"),
        track("林俊杰 - 江南.mp3"),
        track("Eric周兴哲 - 离开你以后.mp3"),
    ]
    assert filenames(
        sort_tracks(tracks, SortField.FILENAME, SortDirection.ASCENDING)
    ) == [
        "A-Lin - 失恋无罪.mp3",
        "Eric周兴哲 - 离开你以后.mp3",
        "林俊杰 - 江南.mp3",
        "周杰伦 - 晴天.mp3",
    ]


def test_pinyin_sort_still_ignores_canonical_number_prefix() -> None:
    tracks = [track("001 周杰伦.mp3"), track("002 林俊杰.mp3")]
    assert filenames(
        sort_tracks(tracks, SortField.FILENAME, SortDirection.ASCENDING)
    ) == ["002 林俊杰.mp3", "001 周杰伦.mp3"]


def test_pinyin_filename_sort_reverses_correctly() -> None:
    tracks = [track("林俊杰.mp3"), track("周杰伦.mp3")]
    assert filenames(
        sort_tracks(tracks, SortField.FILENAME, SortDirection.DESCENDING)
    ) == ["周杰伦.mp3", "林俊杰.mp3"]


def test_modified_newest_and_oldest() -> None:
    tracks = [track("A.mp3", modified=10), track("B.mp3", modified=30), track("C.mp3", modified=20)]
    assert filenames(sort_tracks(tracks, SortField.MODIFIED, SortDirection.DESCENDING)) == [
        "B.mp3", "C.mp3", "A.mp3"
    ]
    assert filenames(sort_tracks(tracks, SortField.MODIFIED, SortDirection.ASCENDING)) == [
        "A.mp3", "C.mp3", "B.mp3"
    ]


def test_size_largest_and_smallest() -> None:
    tracks = [track("A.mp3", size=10), track("B.mp3", size=30), track("C.mp3", size=20)]
    assert filenames(sort_tracks(tracks, SortField.SIZE, SortDirection.DESCENDING)) == [
        "B.mp3", "C.mp3", "A.mp3"
    ]
    assert filenames(sort_tracks(tracks, SortField.SIZE, SortDirection.ASCENDING)) == [
        "A.mp3", "C.mp3", "B.mp3"
    ]


def test_type_ascending_and_descending() -> None:
    tracks = [track("A.wav"), track("B.flac"), track("C.MP3")]
    assert filenames(sort_tracks(tracks, SortField.TYPE, SortDirection.ASCENDING)) == [
        "B.flac", "C.MP3", "A.wav"
    ]
    assert filenames(sort_tracks(tracks, SortField.TYPE, SortDirection.DESCENDING)) == [
        "A.wav", "C.MP3", "B.flac"
    ]


def test_metadata_ties_use_deterministic_natural_filename_order() -> None:
    tracks = [track("Song 10.mp3", size=5), track("Song 2.mp3", size=5)]
    assert filenames(sort_tracks(tracks, SortField.SIZE, SortDirection.DESCENDING)) == [
        "Song 2.mp3", "Song 10.mp3"
    ]


def test_first_click_defaults_and_direction_toggle() -> None:
    filename = next_sort_state(SortState.custom(), SortField.FILENAME)
    assert filename.direction == SortDirection.ASCENDING
    assert next_sort_state(filename, SortField.FILENAME).direction == SortDirection.DESCENDING
    assert next_sort_state(filename, SortField.SIZE).direction == SortDirection.DESCENDING


def test_sorting_does_not_modify_files(tmp_path: Path) -> None:
    paths = [tmp_path / "Song 10.mp3", tmp_path / "Song 2.mp3"]
    for path in paths:
        path.write_bytes(b"audio")
    before = [(path.name, path.stat().st_size, path.stat().st_mtime_ns) for path in paths]
    sort_tracks([Track.from_path(path) for path in paths], SortField.FILENAME, SortDirection.ASCENDING)
    after = [(path.name, path.stat().st_size, path.stat().st_mtime_ns) for path in paths]
    assert after == before


def test_manual_movement_switches_main_window_to_custom(monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from app.main_window import MainWindow

    monkeypatch.setattr("app.main_window.discover_removable_drives", lambda: [])
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.track_list.set_tracks([track("A.mp3"), track("B.mp3")], "empty")
    window.sort_state = SortState(SortField.FILENAME, SortDirection.ASCENDING)
    window.track_list.table.selectRow(1)
    window.track_list.move_selected_up()
    assert window.sort_state.is_custom
    assert window.sort_label.text() == "Sort: Custom Order"
    window.close()


def test_header_sort_selection_follows_track_not_old_row(monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from app.main_window import FILENAME_COLUMN, MainWindow

    monkeypatch.setattr("app.main_window.discover_removable_drives", lambda: [])
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.track_list.set_tracks(
        [track("Song 10.mp3"), track("Song 2.mp3")], "empty"
    )
    window.track_list.table.selectRow(0)
    window.sort_by_column(FILENAME_COLUMN)
    assert filenames(window.track_list.tracks) == ["Song 2.mp3", "Song 10.mp3"]
    assert window.track_list.selected_rows() == [1]
    window.close()
