"""Validated, collision-safe, transactional local file renaming."""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_INVALID_WINDOWS_CHARS = frozenset('<>:"/\\|?*')
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class RenameAction:
    source: Path
    target: Path
    expected_identity: tuple[int, int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class RenameReceipt:
    """The completed mapping needed for a one-level session undo."""

    actions: tuple[RenameAction, ...]
    result_identities: tuple[tuple[int, int, int, int] | None, ...] = ()

    def undo_actions(self) -> tuple[RenameAction, ...]:
        identities = self.result_identities or (None,) * len(self.actions)
        return tuple(
            RenameAction(action.target, action.source, identity)
            for action, identity in zip(self.actions, identities)
        )


class RenameValidationError(RuntimeError):
    """A complete plan was rejected before any file was changed."""


class RenameExecutionError(RuntimeError):
    """A filesystem error occurred while executing a validated plan."""

    def __init__(
        self,
        message: str,
        *,
        rollback_complete: bool,
        manual_attention: Iterable[Path] = (),
    ) -> None:
        super().__init__(message)
        self.rollback_complete = rollback_complete
        self.manual_attention = tuple(manual_attention)


def _path_key(path: Path) -> str:
    """Use Windows-style case-insensitive identity even in unit-test environments."""

    return os.path.abspath(os.fspath(path)).replace("/", "\\").casefold()


def _same_spelling(left: Path, right: Path) -> bool:
    return os.path.abspath(os.fspath(left)) == os.path.abspath(os.fspath(right))


def _windows_component_length(name: str) -> int:
    return len(name.encode("utf-16-le")) // 2


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def validate_windows_filename(filename: str) -> None:
    """Reject names that Windows cannot safely create as regular files."""

    if not filename or filename in {".", ".."}:
        raise RenameValidationError("A target filename is empty or invalid.")
    if filename.endswith((" ", ".")):
        raise RenameValidationError(
            f'Target filename ends with a space or period: "{filename}"'
        )
    if any(character in _INVALID_WINDOWS_CHARS or ord(character) < 32 for character in filename):
        raise RenameValidationError(f'Target filename contains an invalid character: "{filename}"')
    if _windows_component_length(filename) > 255:
        raise RenameValidationError(f'Target filename is too long for Windows: "{filename}"')

    stem = filename.split(".", 1)[0].upper()
    if stem in _RESERVED_WINDOWS_NAMES:
        raise RenameValidationError(f'Target filename is reserved by Windows: "{filename}"')


class SafeRenamer:
    """Execute rename plans through unique temporary paths with rollback."""

    def __init__(
        self,
        rename: Callable[[Path, Path], None] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._rename = rename or self._default_rename
        self._token_factory = token_factory or (lambda: uuid.uuid4().hex)

    @staticmethod
    def _default_rename(source: Path, target: Path) -> None:
        source.rename(target)

    def validate(self, actions: Iterable[RenameAction]) -> tuple[RenameAction, ...]:
        """Validate the entire operation without changing the filesystem."""

        plan = tuple(
            action for action in actions if not _same_spelling(action.source, action.target)
        )
        if not plan:
            return ()

        source_keys = [_path_key(action.source) for action in plan]
        target_keys = [_path_key(action.target) for action in plan]
        if len(source_keys) != len(set(source_keys)):
            raise RenameValidationError("The rename plan contains the same source file twice.")
        if len(target_keys) != len(set(target_keys)):
            duplicate = next(
                action.target.name
                for index, action in enumerate(plan)
                if target_keys.index(target_keys[index]) != index
            )
            raise RenameValidationError(
                f'Two files would produce the same target filename: "{duplicate}"'
            )

        source_key_set = set(source_keys)
        checked_folders: set[str] = set()
        for action in plan:
            if _path_key(action.source.parent) != _path_key(action.target.parent):
                raise RenameValidationError("Renames must remain inside the loaded folder.")
            if not action.source.exists() or not action.source.is_file():
                raise RenameValidationError(
                    f'Source file is missing or no longer valid: "{action.source.name}"\n'
                    "Reload the folder before trying again."
                )
            if action.expected_identity is not None:
                try:
                    identity_changed = (
                        _file_identity(action.source) != action.expected_identity
                    )
                except OSError as exc:
                    raise RenameValidationError(
                        f'Source file can no longer be inspected: "{action.source.name}"\n'
                        "Reload the folder before trying again."
                    ) from exc
                if identity_changed:
                    raise RenameValidationError(
                        f'Source file changed outside the application: "{action.source.name}"\n'
                        "Reload the folder before trying again."
                    )
            validate_windows_filename(action.target.name)
            if len(os.path.abspath(os.fspath(action.target))) >= 260:
                raise RenameValidationError(
                    f'Target path is too long to rename safely: "{action.target.name}"'
                )

            folder_key = _path_key(action.source.parent)
            if folder_key not in checked_folders:
                if not action.source.parent.is_dir():
                    raise RenameValidationError("The loaded folder no longer exists.")
                if not os.access(action.source.parent, os.W_OK):
                    raise RenameValidationError(
                        "The loaded folder is not writable with the current permissions."
                    )
                checked_folders.add(folder_key)

        # Inspect actual directory entries so differently-cased Windows names are
        # treated as the same target even when tests run on another platform.
        existing_keys: set[str] = set()
        try:
            for folder in {action.source.parent for action in plan}:
                existing_keys.update(_path_key(entry) for entry in folder.iterdir())
        except OSError as exc:
            raise RenameValidationError(f"Unable to inspect the loaded folder: {exc}") from exc

        for action, target_key in zip(plan, target_keys):
            if target_key in existing_keys and target_key not in source_key_set:
                raise RenameValidationError(
                    f'Target already exists: "{action.target.name}"\nNo files were changed.'
                )
        return plan

    def execute(self, actions: Iterable[RenameAction]) -> RenameReceipt:
        """Validate and execute a complete plan, rolling back on any failure."""

        plan = self.validate(actions)
        if not plan:
            return RenameReceipt(())

        reserved = {
            *(_path_key(action.source) for action in plan),
            *(_path_key(action.target) for action in plan),
        }
        temporary_paths = [self._new_temp_path(action.source, reserved) for action in plan]
        current_paths = [action.source for action in plan]

        try:
            for index, (action, temporary) in enumerate(zip(plan, temporary_paths)):
                self._rename_without_overwrite(action.source, temporary)
                current_paths[index] = temporary
            for index, (action, temporary) in enumerate(zip(plan, temporary_paths)):
                self._rename_without_overwrite(temporary, action.target)
                current_paths[index] = action.target
        except OSError as exc:
            logger.exception("Rename operation failed; attempting rollback")
            rollback_errors = self._rollback(plan, current_paths, reserved)
            if rollback_errors:
                affected = [
                    current
                    for action, current in zip(plan, current_paths)
                    if _path_key(current) != _path_key(action.source) or not action.source.exists()
                ]
                details = "; ".join(rollback_errors)
                raise RenameExecutionError(
                    "The rename failed and automatic rollback was incomplete. "
                    f"Manual attention may be required. Details: {details}",
                    rollback_complete=False,
                    manual_attention=affected,
                ) from exc
            raise RenameExecutionError(
                "The rename could not be completed. All changed files were restored "
                "to their original names.",
                rollback_complete=True,
            ) from exc

        identities: list[tuple[int, int, int, int] | None] = []
        for action in plan:
            try:
                identities.append(_file_identity(action.target))
            except OSError:
                identities.append(None)
        return RenameReceipt(plan, tuple(identities))

    def _new_temp_path(self, source: Path, reserved: set[str]) -> Path:
        for _attempt in range(100):
            token = re.sub(r"[^A-Za-z0-9_-]", "", self._token_factory())
            if not token:
                continue
            candidate = source.with_name(
                f".__usb_track_order_tmp_{token}{source.suffix}"
            )
            key = _path_key(candidate)
            if key not in reserved and not candidate.exists():
                reserved.add(key)
                return candidate
        raise RenameValidationError("Unable to allocate a unique temporary filename.")

    def _rename_without_overwrite(self, source: Path, target: Path) -> None:
        if not source.exists():
            raise OSError(f'Source file disappeared: "{source.name}"')
        if target.exists():
            raise OSError(f'Refusing to overwrite existing file: "{target.name}"')
        self._rename(source, target)

    def _rollback(
        self,
        plan: tuple[RenameAction, ...],
        current_paths: list[Path],
        reserved: set[str],
    ) -> list[str]:
        """Best-effort two-stage restoration to the original source names."""

        errors: list[str] = []
        recovery_paths: list[Path | None] = [None] * len(plan)

        # Move every changed current location out of the way first. This makes
        # rollback safe even when the original and target names form a cycle.
        for index, (action, current) in enumerate(zip(plan, current_paths)):
            if _path_key(current) == _path_key(action.source) and action.source.exists():
                continue
            if not current.exists():
                errors.append(f'could not locate "{current.name}" during rollback')
                continue
            try:
                recovery = self._new_temp_path(action.source, reserved)
                self._rename_without_overwrite(current, recovery)
                recovery_paths[index] = recovery
                current_paths[index] = recovery
            except (OSError, RenameValidationError) as rollback_exc:
                errors.append(f'could not secure "{current.name}": {rollback_exc}')

        for index, (action, recovery) in enumerate(zip(plan, recovery_paths)):
            if recovery is None:
                continue
            try:
                self._rename_without_overwrite(recovery, action.source)
                current_paths[index] = action.source
            except OSError as rollback_exc:
                errors.append(f'could not restore "{action.source.name}": {rollback_exc}')

        return errors
