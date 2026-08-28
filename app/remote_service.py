"""Optional Poly remote / controller button handling.

Poly controllers, like most conference remotes, appear to Linux as ordinary HID
input devices, so their buttons arrive as standard ``KEY_*`` codes on
``/dev/input/event*``. Because the codes differ between models, every mapping is
configurable (``POLY_ANSWER_KEY`` and friends) and the administrator can discover
the real codes with:

    ./scripts/diagnose-remote.sh          # human-friendly wrapper around evtest

or from Settings → Diagnostics → Discover remote buttons, which uses
:meth:`RemoteService.capture_keys` to do the same thing without a terminal.

The whole service is optional. Without ``python3-evdev``, without a remote, or
with ``POLY_REMOTE_ENABLED`` off, it logs once and does nothing.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import ConfigManager
from .logging_setup import get_logger, log_event

log = get_logger("remote")

#: Actions a button can be bound to. Names appear in the Settings UI help.
ACTIONS = (
    "join",       # join the current or next meeting
    "hangup",     # leave the meeting, return to the dashboard
    "mute",       # toggle the microphone
    "volume_up",
    "volume_down",
    "camera",     # toggle the camera in the meeting
    "home",       # force the dashboard back on screen
)

#: Config key -> action.
KEY_BINDINGS: dict[str, str] = {
    "POLY_ANSWER_KEY": "join",
    "POLY_HANGUP_KEY": "hangup",
    "POLY_MUTE_KEY": "mute",
    "POLY_VOLUME_UP_KEY": "volume_up",
    "POLY_VOLUME_DOWN_KEY": "volume_down",
    "POLY_CAMERA_KEY": "camera",
    "POLY_HOME_KEY": "home",
}

#: Words that suggest an input device is a conference remote rather than a
#: keyboard. Used only when ``POLY_REMOTE_DEVICE`` is "auto".
_DEVICE_HINTS = (
    "poly", "plantronics", "polycom", "studio", "remote", "consumer control",
    "hid", "cec", "ir-receiver", "gpio-key",
)

#: Ignore a repeated press of the same button within this window (debounce).
DEBOUNCE_SECONDS = 0.45


@dataclass
class RemoteStatus:
    available: bool = False
    enabled: bool = False
    devices: list[str] = None            # type: ignore[assignment]
    last_key: str = ""
    last_action: str = ""
    last_at: float = 0.0
    error: str = ""
    presses: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "enabled": self.enabled,
            "devices": list(self.devices or []),
            "last_key": self.last_key,
            "last_action": self.last_action,
            "seconds_since_last": (
                round(time.monotonic() - self.last_at, 1) if self.last_at else None
            ),
            "error": self.error,
            "presses": self.presses,
        }


def evdev_available() -> bool:
    try:
        import evdev  # noqa: F401
        return True
    except Exception:
        return False


class RemoteService:
    """Watches input devices and turns button presses into room actions."""

    def __init__(
        self,
        config: ConfigManager,
        dispatch: Callable[[str], None],
    ) -> None:
        self.config = config
        #: Called with an action name from :data:`ACTIONS`.
        self.dispatch = dispatch
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._status = RemoteStatus(available=evdev_available(), devices=[])
        self._last_press: dict[str, float] = {}

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if not self.config.bool_("POLY_REMOTE_ENABLED"):
            log_event(log, logging.INFO, "remote.disabled")
            return
        if not self._status.available:
            with self._lock:
                self._status.error = (
                    "python3-evdev is not installed; remote support is unavailable."
                )
            log_event(log, logging.WARNING, "remote.evdev_missing")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="remote-input", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3)

    # -- state -----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            self._status.enabled = self.config.bool_("POLY_REMOTE_ENABLED")
            return self._status.to_dict()

    def mappings(self) -> dict[str, str]:
        """Currently configured ``KEY_* -> action`` map."""
        out: dict[str, str] = {}
        for key_name, action in KEY_BINDINGS.items():
            code = self.config.str_(key_name).strip().upper()
            if code:
                out[code] = action
        return out

    # -- device discovery ------------------------------------------------
    def list_devices(self) -> list[dict[str, Any]]:
        """Every input device, for the diagnostics page."""
        if not self._status.available:
            return []
        import evdev

        devices: list[dict[str, Any]] = []
        for path in evdev.list_devices():
            try:
                device = evdev.InputDevice(path)
            except OSError:
                continue
            try:
                has_keys = evdev.ecodes.EV_KEY in device.capabilities()
                devices.append(
                    {
                        "path": path,
                        "name": device.name,
                        "phys": device.phys or "",
                        "has_keys": has_keys,
                        "likely_remote": self._looks_like_remote(device.name),
                    }
                )
            finally:
                try:
                    device.close()
                except Exception:
                    pass
        return devices

    @staticmethod
    def _looks_like_remote(name: str) -> bool:
        lowered = (name or "").lower()
        return any(hint in lowered for hint in _DEVICE_HINTS)

    def _open_devices(self) -> list[Any]:
        import evdev

        preference = self.config.str_("POLY_REMOTE_DEVICE").strip()
        opened: list[Any] = []
        for path in evdev.list_devices():
            try:
                device = evdev.InputDevice(path)
            except OSError:
                continue
            keep = False
            try:
                if preference and preference.lower() != "auto":
                    keep = path == preference or device.name == preference
                else:
                    keep = evdev.ecodes.EV_KEY in device.capabilities()
            except Exception:
                keep = False
            if keep:
                opened.append(device)
            else:
                try:
                    device.close()
                except Exception:
                    pass
        return opened

    # -- the watch loop --------------------------------------------------
    def _run(self) -> None:
        import select

        while not self._stop.is_set():
            devices = self._open_devices()
            with self._lock:
                self._status.devices = [f"{d.name} ({d.path})" for d in devices]
                self._status.error = "" if devices else "No input devices found."

            if not devices:
                log_event(log, logging.WARNING, "remote.no_devices")
                if self._stop.wait(timeout=20):
                    return
                continue

            log_event(
                log, logging.INFO, "remote.watching",
                devices=len(devices),
                names=";".join(d.name for d in devices)[:120],
            )
            try:
                self._watch(devices, select)
            except Exception:
                log.exception("remote.watch_failed")
            finally:
                for device in devices:
                    try:
                        device.close()
                    except Exception:
                        pass
            # A remote can be unplugged; rescan after a pause.
            if self._stop.wait(timeout=5):
                return

    def _watch(self, devices: list[Any], select_module: Any) -> None:
        import evdev

        by_fd = {device.fd: device for device in devices}
        mappings = self.mappings()

        while not self._stop.is_set():
            try:
                readable, _, _ = select_module.select(list(by_fd), [], [], 1.0)
            except (OSError, ValueError):
                return
            if not readable:
                # Cheap way to pick up remapped buttons without a restart.
                mappings = self.mappings()
                continue

            for fd in readable:
                device = by_fd.get(fd)
                if device is None:
                    continue
                try:
                    for event in device.read():
                        if event.type != evdev.ecodes.EV_KEY:
                            continue
                        key_event = evdev.categorize(event)
                        # value 1 = press (ignore release and auto-repeat)
                        if key_event.keystate != 1:
                            continue
                        self._handle_key(key_event.keycode, mappings)
                except OSError:
                    # Device went away; let the outer loop rescan.
                    return

    def _handle_key(self, keycode: Any, mappings: dict[str, str]) -> None:
        # evdev gives a list when one scancode maps to several names.
        names = keycode if isinstance(keycode, (list, tuple)) else [keycode]
        names = [str(n).upper() for n in names]

        with self._lock:
            self._status.last_key = names[0]
            self._status.presses += 1

        for name in names:
            action = mappings.get(name)
            if not action:
                continue
            now = time.monotonic()
            if now - self._last_press.get(action, 0.0) < DEBOUNCE_SECONDS:
                return
            self._last_press[action] = now
            with self._lock:
                self._status.last_action = action
                self._status.last_at = now
            log_event(log, logging.INFO, "remote.button_pressed", key=name, action=action)
            try:
                self.dispatch(action)
            except Exception:
                log.exception("remote.action_failed", extra={"fields": {"action": action}})
            return

        log_event(log, logging.DEBUG, "remote.button_unmapped", key=names[0])

    # -- diagnostics -----------------------------------------------------
    def capture_keys(self, seconds: float = 10.0) -> dict[str, Any]:
        """Record button presses for a few seconds so they can be mapped.

        Powers "Discover remote buttons" in the web UI: the administrator presses
        the button they want, and the exact ``KEY_*`` name comes back ready to
        paste into Settings.
        """
        if not self._status.available:
            return {"ok": False, "error": "python3-evdev is not installed.", "keys": []}

        import evdev
        import select as select_module

        seconds = max(2.0, min(30.0, float(seconds)))
        devices = self._open_devices()
        if not devices:
            return {"ok": False, "error": "No input devices found.", "keys": []}

        seen: list[dict[str, Any]] = []
        deadline = time.monotonic() + seconds
        by_fd = {device.fd: device for device in devices}
        try:
            while time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                try:
                    readable, _, _ = select_module.select(list(by_fd), [], [], min(0.5, remaining))
                except (OSError, ValueError):
                    break
                for fd in readable:
                    device = by_fd.get(fd)
                    if device is None:
                        continue
                    try:
                        for event in device.read():
                            if event.type != evdev.ecodes.EV_KEY:
                                continue
                            key_event = evdev.categorize(event)
                            if key_event.keystate != 1:
                                continue
                            code = key_event.keycode
                            name = str(code[0] if isinstance(code, (list, tuple)) else code).upper()
                            if not any(entry["key"] == name for entry in seen):
                                seen.append({"key": name, "device": device.name})
                    except OSError:
                        continue
        finally:
            for device in devices:
                try:
                    device.close()
                except Exception:
                    pass

        return {
            "ok": True,
            "keys": seen,
            "seconds": seconds,
            "devices": [d for d in (dev.get("name") for dev in self.list_devices()) if d],
        }
