"""Tiny atomic JSON persistence used for caches and runtime state.

Writes go to a temporary file in the same directory and are then renamed, so a
power cut cannot leave a half-written file behind. Reads never raise: a missing
or corrupt file simply yields the caller's default. That is the behaviour an
appliance needs — a damaged cache must never stop the room screen from coming up.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

log = get_logger("store")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        log.warning(
            "store.read_failed", extra={"fields": {"path": str(path), "error": str(exc)}}
        )
        return default


def write_json(path: Path, payload: Any, *, mode: int = 0o600) -> bool:
    """Atomically write ``payload``. Returns True on success, never raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, mode)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return True
    except (OSError, TypeError, ValueError) as exc:
        log.warning(
            "store.write_failed",
            extra={"fields": {"path": str(path), "error": str(exc)}},
        )
        return False


def write_text(path: Path, text: str, *, mode: int = 0o600) -> bool:
    """Atomically write a text file (used for ``config.yaml``)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, mode)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return True
    except OSError as exc:
        logging.getLogger("room.store").warning(
            "store.write_failed",
            extra={"fields": {"path": str(path), "error": str(exc)}},
        )
        return False
