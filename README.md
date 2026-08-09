# USB Track Order

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

Files are never renamed without explicit confirmation. Renaming changes only the
filename: audio contents, extensions, and metadata are preserved.

USB Track Order copies files sequentially in the current list order. Playback
order ultimately depends on the behavior of the target car audio system. Some
systems may instead use filename order, FAT directory order, metadata, or an
internal media database.

## Windows development setup

Python 3.11 or newer is required.

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

The `.exe` is self-contained and can be copied directly to another Windows
computer. Python and PySide6 do not need to be installed there. The one-file
build extracts its runtime dependencies to a temporary directory when launched,
so startup is slower than the folder-based build.

The application uses the USB filesystem as-is. It does not format drives or
modify partitions. Phase 3 intentionally does not include packaging, themes,
settings persistence, metadata editing, playlists, cloud features, or other
Phase 4 work.
