"""On-disk locations under ~/.m73gui: settings.json, logs/, received files.

All paths are resolved per call so tests can point HOME somewhere else."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

__all__ = [
    "app_dir",
    "settings_path",
    "load_settings",
    "save_settings",
    "log_dir",
    "received_dir",
    "SETTINGS_VERSION",
]

SETTINGS_VERSION = 1


def app_dir() -> Path:
    return Path.home() / ".m73gui"


def settings_path() -> Path:
    return app_dir() / "settings.json"


def load_settings() -> dict:
    try:
        with open(settings_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(data: dict) -> None:
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass  # an unwritable home must never crash the GUI


def log_dir() -> Path:
    d = app_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def received_dir() -> str:
    """Daily received-files folder: ~/.m73gui/received/yyyy-mm-dd/."""
    d = app_dir() / "received" / time.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return str(d)
