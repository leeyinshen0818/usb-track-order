from pathlib import Path

import pytest

from app.services.file_scanner import FileScanError, is_supported_audio, scan_folder


@pytest.mark.parametrize("extension", [".mp3", ".flac", ".wav", ".m4a", ".aac"])
def test_supported_audio_extensions(extension: str) -> None:
    assert is_supported_audio(f"track{extension}")


@pytest.mark.parametrize("filename", ["Song.MP3", "Song.FlAc", "Song.WaV", "Song.M4A"])
def test_extension_detection_is_case_insensitive(filename: str) -> None:
    assert is_supported_audio(filename)


def test_non_audio_files_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "song.mp3").touch()
    (tmp_path / "cover.jpg").touch()
    (tmp_path / "notes.txt").touch()
    assert [track.original_filename for track in scan_folder(tmp_path)] == ["song.mp3"]


def test_natural_filename_ordering(tmp_path: Path) -> None:
    for name in ["Song 10.mp3", "Song 2.mp3", "Song 1.mp3"]:
        (tmp_path / name).touch()
    assert [track.original_filename for track in scan_folder(tmp_path)] == [
        "Song 1.mp3",
        "Song 2.mp3",
        "Song 10.mp3",
    ]


def test_natural_sort_handles_mixed_numeric_and_text_prefixes(tmp_path: Path) -> None:
    for name in ["Song 2.mp3", "010 Intro.mp3", "002 Intro.mp3"]:
        (tmp_path / name).touch()
    assert [track.original_filename for track in scan_folder(tmp_path)] == [
        "002 Intro.mp3",
        "010 Intro.mp3",
        "Song 2.mp3",
    ]


def test_scan_is_not_recursive(tmp_path: Path) -> None:
    (tmp_path / "top.mp3").touch()
    nested = tmp_path / "album"
    nested.mkdir()
    (nested / "nested.mp3").touch()
    assert [track.original_filename for track in scan_folder(tmp_path)] == ["top.mp3"]


def test_empty_folder_returns_empty_list(tmp_path: Path) -> None:
    assert scan_folder(tmp_path) == []


def test_scanned_track_exposes_file_metadata(tmp_path: Path) -> None:
    path = tmp_path / "Song.MP3"
    path.write_bytes(b"audio-data")
    track = scan_folder(tmp_path)[0]
    assert track.extension == ".MP3"
    assert track.file_type == "MP3"
    assert track.size_bytes == len(b"audio-data")
    assert track.modified_time is not None
    assert track.file_identity is not None


def test_missing_folder_has_friendly_error(tmp_path: Path) -> None:
    with pytest.raises(FileScanError, match="no longer exists"):
        scan_folder(tmp_path / "missing")
