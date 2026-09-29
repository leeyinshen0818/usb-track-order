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
- Fast FFmpeg loudness synchronization from -20.0 to -10.0 LUFS
- Six-track bounded processing with safe temporary replacement
- Rename-safe local sync history that skips unchanged tracks at the same target

Fast loudness mode uses single-pass normalization and writes normalized audio at
48 kHz for broad car-audio compatibility and lower processing overhead.

## Loudness synchronization

1. Open a folder containing supported music files.
2. Optionally select specific tracks; with no selection, all loaded tracks are checked.
3. Choose a target from -20.0 to -10.0 LUFS. The default is -14.0 LUFS.
4. Select **Synchronize Loudness** and confirm the summary.

Up to six tracks are processed concurrently. Each result is written to a temporary
file and replaces its original only after FFmpeg exits successfully. Tracks already
synchronized to the same target are skipped using rename-safe local state stored in
`%LOCALAPPDATA%\USB Track Order\loudness_state.json`. The target setting is also
remembered between launches.

Limitations:

- Fast mode uses single-pass normalization, so results are approximate rather than
  mastering-grade measurements.
- Normalized audio is encoded at 48 kHz. Lossy formats are re-encoded once.
- Metadata and embedded artwork are preserved where the source container supports it.
- Processing several tracks can temporarily use significant CPU and disk bandwidth.
- Sync recognition uses file identity, size, timestamp, and sampled content; moving a
  file to another filesystem may cause it to be processed again.

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
