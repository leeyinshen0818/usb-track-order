"""Crash safety for renames: an on-disk journal and a persisted undo receipt.

The journal is written before any file is renamed and removed after success
(or after a complete in-process rollback). If the app dies mid-rename, the
journal survives and ``recover_interrupted_rename`` puts every file back under
its original name on the next start.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.services.safe_renamer import (
    RENAME_TEMP_PREFIX,
    RenameAction,
    RenameReceipt,
)

logger = logging.getLogger("usb_track_order.rename_journal")

JOURNAL_VERSION = 1
RECEIPT_VERSION = 1


def _write_json_atomic(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("Unreadable file %s: %s", path, exc)
        return None


# --------------------------------------------------------------------------- #
# Journal
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class JournalEntry:
    source: Path
    temporary: Path
    target: Path
    inode: int
    size_bytes: int


class RenameJournal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, entries: Sequence[JournalEntry]) -> None:
        _write_json_atomic(
            self.path,
            {
                "version": JOURNAL_VERSION,
                "created": time.time(),
                "entries": [
                    {
                        "source": str(entry.source),
                        "temporary": str(entry.temporary),
                        "target": str(entry.target),
                        "inode": entry.inode,
                        "size_bytes": entry.size_bytes,
                    }
                    for entry in entries
                ],
            },
        )

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def read(self) -> list[JournalEntry] | None:
        document = _read_json(self.path)
        if not isinstance(document, dict) or document.get("version") != JOURNAL_VERSION:
            return None if document is None else []
        entries: list[JournalEntry] = []
        for raw in document.get("entries", []):
            try:
                entries.append(
                    JournalEntry(
                        Path(raw["source"]),
                        Path(raw["temporary"]),
                        Path(raw["target"]),
                        int(raw["inode"]),
                        int(raw["size_bytes"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return entries


@dataclass(slots=True)
class RecoveryReport:
    restored: list[tuple[Path, Path]] = field(default_factory=list)  # (from, to)
    already_in_place: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        lines = [
            "A rename was interrupted last time (for example by a crash or power loss).",
            f"Restored to original names: {len(self.restored)}",
            f"Already in place: {self.already_in_place}",
        ]
        if self.restored:
            lines.append("")
            lines.extend(f"{dst.name}  ←  {src.name}" for src, dst in self.restored[:15])
            if len(self.restored) > 15:
                lines.append(f"… and {len(self.restored) - 15} more")
        if self.problems:
            lines.append("")
            lines.append("Could not restore automatically — please check:")
            lines.extend(self.problems[:15])
        return "\n".join(lines)


def _matches(path: Path, entry: JournalEntry) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    if not path.is_file() or stat.st_size != entry.size_bytes:
        return False
    return not entry.inode or not stat.st_ino or stat.st_ino == entry.inode


def recover_interrupted_rename(
    journal_path: Path,
    *,
    rename: Callable[[Path, Path], None] | None = None,
) -> RecoveryReport | None:
    """Roll an interrupted rename back to the original names.

    Returns None when there is no journal. On success the journal is removed;
    otherwise it is kept beside the original as ``*.failed-<time>.json``.
    """

    journal = RenameJournal(journal_path)
    entries = journal.read()
    if entries is None:
        return None
    do_rename = rename or (lambda src, dst: src.rename(dst))
    report = RecoveryReport()

    # Locate each file by identity among the three names it could have.
    located: list[tuple[JournalEntry, Path]] = []
    for entry in entries:
        current = next(
            (
                candidate
                for candidate in (entry.source, entry.temporary, entry.target)
                if _matches(candidate, entry)
            ),
            None,
        )
        if current is None:
            report.problems.append(f'"{entry.source.name}" could not be found')
        elif current == entry.source:
            report.already_in_place += 1
        else:
            located.append((entry, current))

    # Two stages (out of the way, then home) so swapped names cannot collide.
    staged: list[tuple[JournalEntry, Path, Path]] = []
    for entry, current in located:
        parking = entry.source.with_name(
            f"{RENAME_TEMP_PREFIX}rec{uuid.uuid4().hex}{entry.source.suffix}"
        )
        try:
            do_rename(current, parking)
        except OSError as exc:
            report.problems.append(f'"{current}" could not be moved: {exc}')
            continue
        staged.append((entry, current, parking))
    for entry, original_current, parking in staged:
        if entry.source.exists():
            report.problems.append(
                f'"{entry.source.name}" is occupied; the song is kept as "{parking.name}"'
            )
            continue
        try:
            do_rename(parking, entry.source)
        except OSError as exc:
            report.problems.append(
                f'"{entry.source.name}" could not be restored ({exc}); '
                f'the song is kept as "{parking.name}"'
            )
            continue
        report.restored.append((original_current, entry.source))

    if report.complete:
        journal.clear()
    else:
        try:
            os.replace(
                journal_path,
                journal_path.with_name(
                    f"{journal_path.stem}.failed-{int(time.time())}{journal_path.suffix}"
                ),
            )
        except OSError:
            pass
    logger.warning(
        "Recovered interrupted rename: %d restored, %d in place, %d problems",
        len(report.restored),
        report.already_in_place,
        len(report.problems),
    )
    return report


# --------------------------------------------------------------------------- #
# Persisted undo receipt
# --------------------------------------------------------------------------- #


def save_receipt(path: Path, receipt: RenameReceipt) -> bool:
    identities = receipt.result_identities or (None,) * len(receipt.actions)
    try:
        _write_json_atomic(
            path,
            {
                "version": RECEIPT_VERSION,
                "actions": [
                    {
                        "source": str(action.source),
                        "target": str(action.target),
                        "result_identity": list(identity) if identity else None,
                    }
                    for action, identity in zip(receipt.actions, identities)
                ],
            },
        )
    except OSError as exc:
        logger.warning("Could not save undo receipt %s: %s", path, exc)
        return False
    return True


def load_receipt(path: Path) -> RenameReceipt | None:
    document = _read_json(path)
    if not isinstance(document, dict) or document.get("version") != RECEIPT_VERSION:
        return None
    actions: list[RenameAction] = []
    identities: list[tuple[int, int, int, int] | None] = []
    try:
        for raw in document["actions"]:
            actions.append(RenameAction(Path(raw["source"]), Path(raw["target"])))
            identity = raw.get("result_identity")
            identities.append(
                tuple(int(part) for part in identity)  # type: ignore[misc]
                if isinstance(identity, list) and len(identity) == 4
                else None
            )
    except (KeyError, TypeError, ValueError):
        return None
    if not actions or any(identity is None for identity in identities):
        # Without identities an undo could not detect external changes.
        return None
    return RenameReceipt(tuple(actions), tuple(identities))


def clear_receipt(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove undo receipt %s: %s", path, exc)


def receipt_belongs_to(receipt: RenameReceipt, folder: str | Path) -> bool:
    key = os.path.normcase(os.path.abspath(os.fspath(folder)))
    return all(
        os.path.normcase(os.path.abspath(os.fspath(action.target.parent))) == key
        for action in receipt.actions
    )
