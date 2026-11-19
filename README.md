# USB Track Order

A simple Windows app for arranging music tracks in order and copying them to a USB drive, so car and portable players play them in the right sequence.

## Download

Get `USB Track Order.exe` from the [latest release](../../releases/latest). No installation needed (FFmpeg is bundled).

## How to use

1. Click **Open Folder** and choose the folder with your songs.
2. Click the **Filename** header to sort A → Z (Chinese names are sorted by pinyin). Use **Move Top / Up / Down / Bottom** for manual changes.
3. Click **Apply Numbering** to rename the files with order numbers (001, 002, ...). **Undo Last Rename** reverses it.
4. Choose your USB drive and click **Copy to USB**. Files are copied one by one, in order.

## Features

- Numbering with preview, undo, and automatic recovery if a rename is interrupted
- Ordered, safe copy to USB (written as `.part` first, then renamed)
- Optional **Unify volume**: makes songs play at a similar loudness while copying. Off by default; your original files are never modified
- Possible duplicates are highlighted; right-click or press Delete to move songs to the Recycle Bin
- Layout adapts to different Windows screen scaling settings

## Notes

- Windows only.
- Original song files are never changed, except when you choose Apply Numbering (renames only).
- Settings and the volume cache are stored in `%LOCALAPPDATA%\USB Track Order`.
