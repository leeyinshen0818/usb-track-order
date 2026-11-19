# USB Track Order v2.0.0

USB Track Order is a lightweight Windows desktop utility for arranging local
music files into a custom playback order before exporting them to USB drives.

## Features

- Local folder selection with a native Windows folder picker
- Non-recursive scanning for MP3, FLAC, WAV, M4A, and AAC files
- Natural filename sorting
- Single, Ctrl, Shift, and Ctrl+A multi-selection
- Move Top, Move Up, Move Down, and Move Bottom controls
- Single- and multi-track drag-and-drop ordering
- Live `001`, `002`, `003` numbering preview
- Confirmation preview before local filename changes
- Safe application and removal of canonical numbering
- Collision, invalid-name, missing-file, and external-change validation
- Two-stage temporary renaming with rollback on failure
- One-level undo for the latest successful rename in the current session
- Explorer-style sorting by natural filename, modified date, size, and type
- Chinese filename sorting by Pinyin (for example, `周杰伦` sorts under Z)
- Canonical numbering is ignored when sorting by meaningful filename
- Automatic-sort indicators and manual Custom Order state
- Windows removable-drive detection with volume label and free-space display
- Native selection of any existing destination folder inside the active USB drive
- Pre-export source, free-space, and destination-collision validation
- Strictly sequential copying into the selected USB destination folder
- Responsive background export with progress, cancellation, and size verification
- Automatic background loudness measurement (EBU R128) after a folder loads
- Per-track loudness gain applied only to the USB copies; source files are never modified
- Content-keyed measurement cache that survives copies, renames, and numbering

## Loudness

1. Open a folder. Uncached tracks are measured in the background (four at a time);
   the progress bar shows analyzed, cached, failed, and remaining counts.
2. Choose a target from -20.0 to -10.0 LUFS (default -14.0). Changing the target
   does not re-measure anything.
3. **Copy to USB** applies `target - measured LUFS` to each copy:
   - within ±0.5 LU: copied byte-for-byte;
   - otherwise re-encoded with the gain (MP3 → 256 kbps CBR, M4A/AAC → AAC 256 kbps,
     FLAC → FLAC, WAV → 16-bit PCM), keeping sample rate, metadata, and cover art;
   - positive gain is limited so the true peak stays at or below -1.5 dBTP.

Tracks without a measurement (still analyzing, or FFmpeg could not decode them)
block the export with a list of the affected files. Measurements are stored in
`%LOCALAPPDATA%\USB Track Order\loudness_state.json`, keyed by file size plus a
hash of the first and last 64 KB, so renamed, numbered, or copied files (including
files copied back from a USB drive) are not measured again. A log is written to
`usb_track_order.log` in the same folder.

Files are written to the USB drive as `<name>.part` and renamed once complete, one
at a time in list order. Leftover temporary files in the music folder are removed
when the folder is opened.

Files are never renamed without explicit confirmation. Renaming changes only the
filename: audio contents, extensions, and metadata are preserved.

USB Track Order copies files sequentially in the current list order. Playback
order ultimately depends on the behavior of the target car audio system. Some
systems may instead use filename order, FAT directory order, metadata, or an
internal media database.

## Windows development setup

Python 3.11 or newer is required.
The `imageio-ffmpeg` dependency supplies FFmpeg for development, so a separate
system FFmpeg installation is not required.

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## Run tests

```powershell
python -m pytest
```

## Build the Windows executable

The included PyInstaller specification creates a windowed, single-file build
with the application icon embedded and UPX disabled:

```powershell
python -m PyInstaller --noconfirm --clean "USB Track Order.spec"
```

The application is created at:

```text
dist\USB Track Order.exe
```

The `.exe` bundles FFmpeg, Python, PySide6, and the application icon. It can be
copied directly to another Windows computer without installing Python or FFmpeg.
The one-file build extracts its runtime dependencies to a temporary directory when
launched, so startup is slower than the folder-based build.

The application uses the USB filesystem as-is. It does not format drives or modify
partitions. It does not provide mastering controls, playback, EQ, waveform display,
or metadata editing.
