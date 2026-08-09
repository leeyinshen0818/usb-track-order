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

Files are never renamed without explicit confirmation. Renaming changes only the
filename: audio contents, extensions, and metadata are preserved.

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

Phase 2 intentionally does not include USB detection/export, metadata editing,
persistence, packaging, or other later-phase features.
