"""Configuration loading, validation and saving.

Layers, lowest priority first:

1. :func:`app.config_schema.defaults` — sensible values for a fresh appliance
2. ``config/config.yaml`` — written by the installer and by the Settings page
3. ``.env`` / process environment — handy for development and for secrets

The Settings page writes layer 2 through :meth:`ConfigManager.update`, which
validates every value first, keeps a ``.bak`` copy of the previous good file and
writes atomically. If ``config.yaml`` is ever unreadable or corrupt the manager
falls back to the backup and then to the defaults, so the appliance always
starts — a broken config file shows a warning on the dashboard instead of a
black screen.
"""

from __future__ import annotations

import copy
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from . import paths
from .config_schema import (
    FIELDS,
    FIELDS_BY_KEY,
    SECRET_KEYS,
    Field,
    defaults,
    restart_units_for,
)
from .logging_setup import get_logger
from .store import write_text

log = get_logger("config")

_TRUE = {"1", "true", "yes", "on", "y", "enabled", "enable"}
_FALSE = {"0", "false", "no", "off", "n", "disabled", "disable", ""}


class ConfigError(ValueError):
    """Raised when a submitted value cannot be used."""


# ---------------------------------------------------------------------------
# Coercion / validation
# ---------------------------------------------------------------------------


def coerce(field: Field, value: Any) -> Any:
    """Convert ``value`` to the field's type, raising :class:`ConfigError`."""
    if field.type in ("str", "text", "password"):
        if value is None:
            return ""
        return str(value).strip() if field.type != "text" else str(value)

    if field.type == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ConfigError(f"{field.label}: expected yes or no, got {value!r}")

    if field.type == "int":
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError):
            raise ConfigError(f"{field.label}: expected a whole number, got {value!r}")
        return _clamp(field, number)

    if field.type == "float":
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            raise ConfigError(f"{field.label}: expected a number, got {value!r}")
        return _clamp(field, number)

    if field.type == "choice":
        text = str(value).strip()
        lowered = {c.lower(): c for c in field.choices}
        if text.lower() not in lowered:
            raise ConfigError(
                f"{field.label}: must be one of {', '.join(field.choices)}"
            )
        return lowered[text.lower()]

    if field.type == "list":
        if isinstance(value, (list, tuple)):
            items = [str(v).strip() for v in value]
        else:
            items = [line.strip() for line in re.split(r"[\r\n]+", str(value))]
        return [item for item in items if item]

    raise ConfigError(f"{field.label}: unsupported type {field.type}")  # pragma: no cover


def _clamp(field: Field, number: float | int) -> float | int:
    if field.minimum is not None and number < field.minimum:
        raise ConfigError(f"{field.label}: must be at least {_pretty(field.minimum)}")
    if field.maximum is not None and number > field.maximum:
        raise ConfigError(f"{field.label}: must be at most {_pretty(field.maximum)}")
    return number


def _pretty(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)


def validate_pairs(pairs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Coerce and validate ``pairs``. Returns ``(clean_values, errors_by_key)``."""
    clean: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, raw in pairs.items():
        field = FIELDS_BY_KEY.get(key)
        if field is None:
            errors[key] = "Unknown setting."
            continue
        try:
            value = coerce(field, raw)
        except ConfigError as exc:
            errors[key] = str(exc).split(": ", 1)[-1]
            continue
        if field.validator is not None:
            problem = field.validator(value)
            if problem:
                errors[key] = problem
                continue
        clean[key] = value
    return clean, errors


def cross_check(values: dict[str, Any]) -> dict[str, str]:
    """Rules that involve more than one setting and must *block* a save.

    Kept deliberately short. Anything that is merely "not finished yet" belongs
    in :func:`advisories` instead — a half-configured room must still be able to
    save its settings, or the setup page cannot be used at all.
    """
    errors: dict[str, str] = {}

    # Opening the Settings page to the network without a PIN is the one change
    # that is unsafe rather than just incomplete, so it is refused.
    if values.get("ADMIN_LAN_ACCESS") and not str(values.get("ADMIN_PIN") or "").strip():
        errors["ADMIN_PIN"] = (
            "Set an admin PIN before allowing access from other computers."
        )

    restart_time = str(values.get("DAILY_RESTART_TIME") or "").strip()
    if restart_time and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", restart_time):
        errors["DAILY_RESTART_TIME"] = "Use 24-hour HH:MM, for example 04:30."

    for key in ("ACCENT_COLOR", "BACKGROUND_SOLID_COLOR"):
        colour = str(values.get(key) or "").strip()
        if colour and not re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", colour):
            errors[key] = "Use a hex colour such as #3d8bfd."

    return errors


def advisories(values: dict[str, Any]) -> dict[str, str]:
    """Non-blocking "still to do" notes, shown next to the relevant field.

    These never prevent a save: an administrator may well want to set the room
    name before they have the calendar link to hand.
    """
    notes: dict[str, str] = {}

    if values.get("CALENDAR_SOURCE") == "ics" and not str(
        values.get("CALENDAR_ICS_URL") or ""
    ).strip():
        notes["CALENDAR_ICS_URL"] = (
            "Still needed before meetings will appear. Set the calendar source "
            "to “mock” if you just want to try the screen out."
        )

    if values.get("ADMIN_LAN_ACCESS") is False and not str(
        values.get("ADMIN_PIN") or ""
    ).strip():
        notes["ADMIN_PIN"] = (
            "Set a PIN if you want to reach this page from a phone or laptop."
        )

    if values.get("BACKGROUND_MODE") == "slideshow":
        notes["BACKGROUND_MODE"] = (
            "Upload images on the control panel for the slideshow to have "
            "anything to show."
        )

    if values.get("DEV_MODE"):
        notes["DEV_MODE"] = (
            "Development mode is on: hardware is simulated. Turn this off for a "
            "real room."
        )

    return notes


# ---------------------------------------------------------------------------
# Environment overlay
# ---------------------------------------------------------------------------


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal ``.env`` reader (no dependency on the shell)."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def env_overlay() -> dict[str, Any]:
    """Values from ``.env`` and the real environment, keyed by schema key.

    Both bare names (``ROOM_NAME``) and a ``ROOM_APPLIANCE_`` prefix are accepted.
    """
    sources: dict[str, str] = {}
    sources.update(load_dotenv(paths.ENV_FILE))
    sources.update(os.environ)

    out: dict[str, Any] = {}
    for field in FIELDS:
        for candidate in (field.key, f"ROOM_APPLIANCE_{field.key}"):
            if candidate in sources and str(sources[candidate]).strip() != "":
                try:
                    out[field.key] = coerce(field, sources[candidate])
                except ConfigError as exc:
                    log.warning(
                        "config.env_value_ignored",
                        extra={"fields": {"key": field.key, "error": str(exc)}},
                    )
                break
    return out


# ---------------------------------------------------------------------------
# YAML rendering
# ---------------------------------------------------------------------------


def render_yaml(values: dict[str, Any], *, comment_header: bool = True) -> str:
    """Render a documented, grouped YAML file for the given values."""
    from .config_schema import GROUPS, GROUP_HELP

    lines: list[str] = []
    if comment_header:
        lines += [
            "# Meeting-room appliance configuration",
            "#",
            "# Written by scripts/install.sh and by the Settings page. You can edit",
            "# it by hand, then run:  sudo systemctl restart room-dashboard",
            "#",
            "# Every option has a sensible default: deleting a line is safe.",
            f"# Last written: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}",
            "",
        ]

    for gid, title in GROUPS:
        members = [f for f in FIELDS if f.group == gid]
        if not members:
            continue
        lines.append(f"# {'-' * 70}")
        lines.append(f"# {title}")
        help_text = GROUP_HELP.get(gid, "")
        if help_text:
            for chunk in _wrap(help_text, 68):
                lines.append(f"#   {chunk}")
        lines.append(f"# {'-' * 70}")
        for f in members:
            if f.help:
                for chunk in _wrap(f.help, 72):
                    lines.append(f"# {chunk}")
            if f.choices:
                lines.append(f"# options: {', '.join(f.choices)}")
            value = values.get(f.key, f.default)
            lines.append(_yaml_line(f.key, value))
            lines.append("")
        # Collapse the trailing blank line of each group into a single spacer.
        while lines and lines[-1] == "":
            lines.pop()
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _yaml_line(key: str, value: Any) -> str:
    """Render one ``key: value`` entry.

    Always goes through PyYAML on a single-key mapping so quoting, escaping and
    block lists are handled by the library. Rendering a bare scalar instead
    would append YAML's ``...`` document-end marker and produce a file that
    cannot be read back.
    """
    dumped = yaml.safe_dump(
        {key: value},
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=100,
    )
    return dumped.rstrip("\n")


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class ConfigManager:
    """Thread-safe holder of the current configuration."""

    def __init__(self, config_file: Path | None = None) -> None:
        self._file = Path(config_file) if config_file else paths.CONFIG_FILE
        self._lock = threading.RLock()
        self._values: dict[str, Any] = defaults()
        self._env: dict[str, Any] = {}
        self._file_values: dict[str, Any] = {}
        self._listeners: list[Callable[[dict[str, Any], set[str]], None]] = []
        #: Human-readable problems to show on the dashboard / settings page.
        self.warnings: list[str] = []
        self.loaded_from: str = "defaults"
        self.load()

    # -- properties ------------------------------------------------------
    @property
    def file(self) -> Path:
        return self._file

    def __contains__(self, key: str) -> bool:
        return key in self._values

    def get(self, key: str, fallback: Any = None) -> Any:
        with self._lock:
            if key in self._values:
                value = self._values[key]
                return list(value) if isinstance(value, list) else value
        field = FIELDS_BY_KEY.get(key)
        if field is not None:
            return field.default
        return fallback

    # Convenience typed accessors keep call sites readable.
    def str_(self, key: str) -> str:
        return str(self.get(key) or "")

    def int_(self, key: str) -> int:
        try:
            return int(self.get(key))
        except (TypeError, ValueError):
            return int(FIELDS_BY_KEY[key].default)

    def float_(self, key: str) -> float:
        try:
            return float(self.get(key))
        except (TypeError, ValueError):
            return float(FIELDS_BY_KEY[key].default)

    def bool_(self, key: str) -> bool:
        return bool(self.get(key))

    def list_(self, key: str) -> list[str]:
        value = self.get(key)
        return list(value) if isinstance(value, list) else []

    def as_dict(self, *, redact: bool = False) -> dict[str, Any]:
        with self._lock:
            out = copy.deepcopy(self._values)
        if redact:
            for key in SECRET_KEYS:
                if out.get(key):
                    out[key] = "********"
        return out

    def env_locked_keys(self) -> list[str]:
        """Keys pinned by the environment; the Settings page shows them read-only."""
        with self._lock:
            return sorted(self._env)

    # -- loading ---------------------------------------------------------
    def load(self) -> dict[str, Any]:
        """(Re)load defaults + file + environment. Never raises."""
        with self._lock:
            self.warnings = []
            file_values, source = self._read_file()
            self._file_values = file_values
            self._env = env_overlay()
            self.loaded_from = source

            merged = defaults()
            merged.update(file_values)
            merged.update(self._env)

            # A value that fails validation is dropped back to its default
            # rather than taking the appliance down.
            clean, errors = validate_pairs(merged)
            for key, problem in errors.items():
                if key in FIELDS_BY_KEY:
                    clean[key] = FIELDS_BY_KEY[key].default
                    self.warnings.append(f"{FIELDS_BY_KEY[key].label}: {problem}")
                    log.warning(
                        "config.value_reset_to_default",
                        extra={"fields": {"key": key, "error": problem}},
                    )
            for field in FIELDS:
                clean.setdefault(field.key, field.default)

            # LAN admin without a PIN would be an open door: refuse it.
            if clean.get("ADMIN_LAN_ACCESS") and not str(
                clean.get("ADMIN_PIN") or ""
            ).strip():
                clean["ADMIN_LAN_ACCESS"] = False
                self.warnings.append(
                    "Network access to the Settings page was switched off because "
                    "no admin PIN is set."
                )
                log.warning("config.lan_admin_disabled_no_pin")

            self._values = clean
        return self.as_dict()

    def _read_file(self) -> tuple[dict[str, Any], str]:
        """Read the config file, healing a corrupt one from the backup."""
        for candidate, label in (
            (self._file, "config.yaml"),
            (paths.CONFIG_BACKUP, "config.yaml.bak"),
        ):
            if not candidate.exists():
                continue
            try:
                raw = yaml.safe_load(candidate.read_text(encoding="utf-8"))
            except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
                self.warnings.append(
                    f"{candidate.name} could not be read ({exc.__class__.__name__}); "
                    "falling back to the previous version."
                )
                log.error(
                    "config.file_unreadable",
                    extra={"fields": {"path": str(candidate), "error": str(exc)}},
                )
                self._quarantine(candidate)
                continue
            if raw is None:
                return {}, label
            if not isinstance(raw, dict):
                self.warnings.append(f"{candidate.name} is not a set of settings.")
                log.error("config.file_not_mapping", extra={"fields": {"path": str(candidate)}})
                self._quarantine(candidate)
                continue
            known = {k: v for k, v in raw.items() if k in FIELDS_BY_KEY}
            unknown = sorted(set(raw) - set(known))
            if unknown:
                log.warning(
                    "config.unknown_keys_ignored",
                    extra={"fields": {"keys": ",".join(unknown[:10])}},
                )
            if label != "config.yaml":
                self.warnings.append(
                    "Running from the backup configuration; open Settings and press "
                    "Save to write a fresh file."
                )
            return known, label
        return {}, "defaults"

    @staticmethod
    def _quarantine(path: Path) -> None:
        """Move an unusable config aside so a good one can be written."""
        try:
            path.replace(paths.CONFIG_BROKEN)
            log.warning("config.quarantined", extra={"fields": {"path": str(paths.CONFIG_BROKEN)}})
        except OSError:
            pass

    # -- saving ----------------------------------------------------------
    def update(
        self, pairs: dict[str, Any], *, persist: bool = True
    ) -> tuple[set[str], dict[str, str]]:
        """Validate and apply ``pairs``.

        Returns ``(changed_keys, errors)``. Nothing is applied if there are
        errors, so a bad Settings submission leaves the appliance untouched.
        """
        clean, errors = validate_pairs(pairs)

        with self._lock:
            # Cross-checks run against everything that *did* validate, so a
            # single save reports every problem at once instead of revealing
            # them one at a time.
            candidate = copy.deepcopy(self._values)
            candidate.update(clean)
            for key, problem in cross_check(candidate).items():
                errors.setdefault(key, problem)
            if errors:
                return set(), errors

            changed = {
                key for key, value in clean.items() if self._values.get(key) != value
            }
            if not changed:
                return set(), {}

            self._file_values.update(clean)
            self._values = candidate

            if persist:
                if not self._write_file():
                    return changed, {
                        "_file": "Settings applied, but the configuration file "
                        "could not be written — they will be lost on restart. "
                        "Check permissions on config/config.yaml."
                    }

        self._notify(changed)
        log.info(
            "config.updated",
            extra={"fields": {"keys": ",".join(sorted(changed)), "count": len(changed)}},
        )
        return changed, {}

    def reset_to_defaults(self, *, keep: Iterable[str] = ()) -> set[str]:
        """Factory reset, optionally preserving some keys (e.g. the calendar URL)."""
        keep = set(keep)
        with self._lock:
            preserved = {k: self._values.get(k) for k in keep if k in self._values}
            fresh = defaults()
            fresh.update({k: v for k, v in preserved.items() if v is not None})
            changed = {k for k, v in fresh.items() if self._values.get(k) != v}
            self._values = fresh
            self._file_values = {
                k: v for k, v in fresh.items() if k in preserved
            }
            self._write_file()
        self._notify(changed)
        log.warning(
            "config.reset_to_defaults",
            extra={"fields": {"kept": ",".join(sorted(keep))}},
        )
        return changed

    def _write_file(self) -> bool:
        """Write ``config.yaml`` atomically, keeping the previous copy as ``.bak``."""
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            if self._file.exists():
                try:
                    paths.CONFIG_BACKUP.write_bytes(self._file.read_bytes())
                    os.chmod(paths.CONFIG_BACKUP, 0o600)
                except OSError as exc:
                    log.warning(
                        "config.backup_failed", extra={"fields": {"error": str(exc)}}
                    )
        except OSError as exc:
            log.error("config.write_failed", extra={"fields": {"error": str(exc)}})
            return False

        # Persist the full effective set minus environment-pinned keys, so the
        # file always documents the whole configuration.
        with self._lock:
            payload = {
                k: v for k, v in self._values.items() if k not in self._env
            }
        ok = write_text(self._file, render_yaml(payload), mode=0o600)
        if ok:
            log.info("config.saved", extra={"fields": {"path": str(self._file)}})
        return ok

    # -- change notification --------------------------------------------
    def on_change(self, callback: Callable[[dict[str, Any], set[str]], None]) -> None:
        """Register a callback fired after settings change."""
        with self._lock:
            self._listeners.append(callback)

    def _notify(self, changed: set[str]) -> None:
        snapshot = self.as_dict()
        with self._lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(snapshot, changed)
            except Exception:  # pragma: no cover - a listener must not break saving
                log.exception(
                    "config.listener_failed",
                    extra={"fields": {"callback": getattr(callback, "__name__", "?")}},
                )

    # -- helpers ---------------------------------------------------------
    def restart_units_for_changes(self, changed: Iterable[str]) -> list[str]:
        return restart_units_for(changed)

    def setup_required(self) -> bool:
        """True until the room has a usable calendar configuration.

        A calendar link is the one thing an administrator must supply; every
        other option has a working default. Choosing the mock or disabled
        calendar source is a deliberate decision, so neither counts as
        unfinished setup.
        """
        return self.str_("CALENDAR_SOURCE") == "ics" and not self.str_(
            "CALENDAR_ICS_URL"
        )

    def airplay_name(self) -> str:
        return self.str_("AIRPLAY_NAME") or self.str_("ROOM_NAME") or "Meeting Room"

    def join_display_name(self) -> str:
        return self.str_("JOIN_DISPLAY_NAME") or self.str_("ROOM_NAME") or "Meeting Room"

    def performance(self):
        """The :class:`~app.hardware_profile.Tuning` this machine should run at.

        Imported lazily: the hardware is read from ``/proc`` and ``/sys``, and
        nothing that merely loads the configuration should have to touch them.
        """
        from .hardware_profile import tuning_for

        return tuning_for(self.str_("PERFORMANCE_PROFILE"))

    def performance_report(self) -> dict:
        from .hardware_profile import report

        return report(self.str_("PERFORMANCE_PROFILE"))

    def tz(self):
        """The room's :class:`~zoneinfo.ZoneInfo`, or the system zone."""
        name = self.str_("TIMEZONE")
        if name:
            try:
                from zoneinfo import ZoneInfo

                return ZoneInfo(name)
            except Exception:
                log.warning("config.bad_timezone", extra={"fields": {"timezone": name}})
        return datetime.now().astimezone().tzinfo


#: Process-wide configuration, created on first use.
_manager: ConfigManager | None = None
_manager_lock = threading.Lock()


def get_config(config_file: Path | None = None) -> ConfigManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            paths.ensure_dirs()
            _manager = ConfigManager(config_file)
        return _manager


def reset_config_for_tests() -> None:  # pragma: no cover - test helper
    global _manager
    with _manager_lock:
        _manager = None
