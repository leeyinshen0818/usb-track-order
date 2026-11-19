"""Per-user data directory for the cache, log, and (when overridden) settings."""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR_ENV = "USB_TRACK_ORDER_DATA_DIR"
APP_DIR_NAME = "USB Track Order"


def default_data_dir() -> Path:
    """The real per-user location, ignoring any override."""

    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"])
    elif os.environ.get("XDG_STATE_HOME"):
        root = Path(os.environ["XDG_STATE_HOME"])
    elif os.environ.get("XDG_DATA_HOME"):
        root = Path(os.environ["XDG_DATA_HOME"])
    else:
        root = Path.home() / ".local" / "share"
    return root / APP_DIR_NAME


def data_dir() -> Path:
    """Data directory, overridable with the USB_TRACK_ORDER_DATA_DIR env var."""

    override = os.environ.get(DATA_DIR_ENV)
    return Path(override) if override else default_data_dir()


def settings_ini_override() -> Path | None:
    """With an override set, settings live in an INI file there, not the registry."""

    override = os.environ.get(DATA_DIR_ENV)
    return Path(override) / "settings.ini" if override else None
