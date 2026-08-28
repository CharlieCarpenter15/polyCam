"""Filesystem layout.

Everything the appliance needs lives under one directory tree so an engineer
can find it, read it and fix it while sitting in front of the Raspberry Pi.
Environment variables allow the layout to be moved (used by the tests and by
development mode).
"""

from __future__ import annotations

import os
from pathlib import Path

#: Repository / installation root (the parent of the ``app`` package).
BASE_DIR = Path(__file__).resolve().parent.parent


def _dir_from_env(var: str, default: Path) -> Path:
    raw = os.environ.get(var, "").strip()
    return Path(raw).expanduser().resolve() if raw else default


#: Writable state: runtime state file, cached calendar, browser profile, logs.
VAR_DIR = _dir_from_env("ROOM_APPLIANCE_VAR", BASE_DIR / "var")

#: Directory holding ``config.yaml``.
CONFIG_DIR = _dir_from_env("ROOM_APPLIANCE_CONFIG_DIR", BASE_DIR / "config")

CONFIG_FILE = CONFIG_DIR / "config.yaml"
CONFIG_BACKUP = CONFIG_DIR / "config.yaml.bak"
CONFIG_BROKEN = CONFIG_DIR / "config.yaml.broken"
CONFIG_EXAMPLE = CONFIG_DIR / "config.example.yaml"
ENV_FILE = BASE_DIR / ".env"

CALENDAR_CACHE = VAR_DIR / "calendar-cache.json"
STATE_FILE = VAR_DIR / "runtime-state.json"
WATCHDOG_STATE = VAR_DIR / "watchdog-state.json"
CHROMIUM_PROFILE = _dir_from_env(
    "ROOM_APPLIANCE_PROFILE", VAR_DIR / "chromium-profile"
)

SCRIPTS_DIR = BASE_DIR / "scripts"
STATIC_DIR = BASE_DIR / "app" / "static"
TEMPLATES_DIR = BASE_DIR / "app" / "templates"


def ensure_dirs() -> None:
    """Create the writable directories if they are missing (safe to repeat)."""
    for path in (VAR_DIR, CONFIG_DIR, CHROMIUM_PROFILE):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            # A read-only filesystem must not stop the dashboard from starting.
            pass
