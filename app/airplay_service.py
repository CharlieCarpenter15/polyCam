"""AirPlay screen sharing state, as reported by the UxPlay supervisor.

UxPlay itself is run by its own systemd unit (``room-airplay.service`` →
``scripts/start-airplay.sh``), which keeps it alive independently of the
backend. The supervisor script watches UxPlay's output and posts session
start/stop events to this service, which is what lets the dashboard get out of
the way while someone is mirroring and come back when they stop.

If the backend is down when an event happens, nothing breaks: the supervisor
retries briefly and then carries on, and this service also expires a stale
session on its own so the dashboard can never be stuck "sharing" forever.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from .config import ConfigManager
from .logging_setup import get_logger, log_event
from .models import FAIL, OFF, OK, WARN
from .system_service import SystemService, which

log = get_logger("airplay")

#: If no heartbeat arrives for this long, assume the supervisor died.
HEARTBEAT_TIMEOUT_SECONDS = 150.0
#: A mirroring session with no further events for this long is treated as over.
SESSION_MAX_SILENCE_SECONDS = 20 * 60.0


class AirPlayService:
    """Tracks whether someone is mirroring, and whether UxPlay is healthy."""

    def __init__(self, config: ConfigManager, system: SystemService) -> None:
        self.config = config
        self.system = system
        self._lock = threading.RLock()
        self._sharing = False
        self._client = ""
        self._session_started: datetime | None = None
        self._session_ended: datetime | None = None
        self._last_heartbeat = 0.0
        self._uxplay_running = False
        self._restarts = 0
        self._last_event = ""
        self._listeners: list = []

    # -- events from the supervisor --------------------------------------
    def on_change(self, callback) -> None:
        """Register ``callback(sharing: bool)``, called when sharing starts/stops."""
        with self._lock:
            self._listeners.append(callback)

    def handle_event(self, event: str, *, client: str = "") -> dict[str, object]:
        """Process one supervisor event.

        Recognised events: ``started`` (UxPlay process up), ``stopped``,
        ``connected`` (a device began mirroring), ``disconnected``,
        ``heartbeat``, ``restarted``.
        """
        event = (event or "").strip().lower()
        changed = False
        now = datetime.now(timezone.utc)

        with self._lock:
            self._last_heartbeat = time.monotonic()
            self._last_event = event

            if event in ("started", "heartbeat"):
                self._uxplay_running = True
            elif event == "restarted":
                self._uxplay_running = True
                self._restarts += 1
                # A restart tears down any mirroring session.
                if self._sharing:
                    self._sharing = False
                    self._session_ended = now
                    changed = True
            elif event == "stopped":
                self._uxplay_running = False
                if self._sharing:
                    self._sharing = False
                    self._session_ended = now
                    changed = True
            elif event == "connected":
                self._uxplay_running = True
                if not self._sharing:
                    self._sharing = True
                    self._session_started = now
                    self._client = client[:60]
                    changed = True
            elif event == "disconnected":
                self._uxplay_running = True
                if self._sharing:
                    self._sharing = False
                    self._session_ended = now
                    self._client = ""
                    changed = True

            sharing = self._sharing

        if event == "restarted":
            log_event(log, logging.WARNING, "airplay.process_restarted", restarts=self._restarts)
        elif changed and sharing:
            log_event(log, logging.INFO, "airplay.sharing_started", client=client[:40] or "unknown")
        elif changed and not sharing:
            log_event(log, logging.INFO, "airplay.sharing_stopped")

        if changed:
            self._notify(sharing)
        return {"ok": True, "sharing": sharing}

    def _notify(self, sharing: bool) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(sharing)
            except Exception:  # pragma: no cover
                log.exception("airplay.listener_failed")

    # -- state -----------------------------------------------------------
    @property
    def sharing(self) -> bool:
        """True while a device is mirroring, with a safety timeout."""
        with self._lock:
            if not self._sharing:
                return False
            if self._session_started is not None:
                elapsed = (datetime.now(timezone.utc) - self._session_started).total_seconds()
                if elapsed > SESSION_MAX_SILENCE_SECONDS and self._stale_heartbeat():
                    # The supervisor stopped reporting mid-session; do not leave
                    # the dashboard hidden indefinitely.
                    self._sharing = False
                    self._session_ended = datetime.now(timezone.utc)
                    log_event(log, logging.WARNING, "airplay.session_expired")
                    return False
            return True

    def _stale_heartbeat(self) -> bool:
        if not self._last_heartbeat:
            return True
        return (time.monotonic() - self._last_heartbeat) > HEARTBEAT_TIMEOUT_SECONDS

    def status(self) -> dict[str, object]:
        enabled = self.config.bool_("AIRPLAY_ENABLED")
        if not enabled:
            return {"enabled": False, "status": OFF, "sharing": False, "name": ""}

        if self.config.bool_("DEV_MODE"):
            with self._lock:
                return {
                    "enabled": True,
                    "mock": True,
                    "status": OK,
                    "sharing": self._sharing,
                    "name": self.config.airplay_name(),
                    "client": self._client,
                    "unit": "simulated",
                    "restarts": self._restarts,
                }

        sharing = self.sharing
        with self._lock:
            stale = self._stale_heartbeat()
            running = self._uxplay_running
            restarts = self._restarts
            client = self._client
            started = self._session_started

        unit_state = self.system.unit_state("room-airplay.service")
        if not which("uxplay"):
            status = FAIL
        elif unit_state == "active" and running and not stale:
            status = OK
        elif unit_state == "active":
            # Unit is up but the supervisor has gone quiet.
            status = WARN
        else:
            status = FAIL

        return {
            "enabled": True,
            "status": status,
            "sharing": sharing,
            "name": self.config.airplay_name(),
            "client": client,
            "since": started.isoformat() if started and sharing else None,
            "unit": unit_state,
            "uxplay_installed": bool(which("uxplay")),
            "supervisor_stale": stale,
            "restarts": restarts,
            "pin_required": bool(self.config.str_("AIRPLAY_PIN")),
        }

    # -- control ---------------------------------------------------------
    def restart(self, *, reason: str = "") -> bool:
        with self._lock:
            self._sharing = False
            self._uxplay_running = False
        return self.system.restart("room-airplay.service", reason=reason or "requested")

    def force_stop_sharing(self) -> bool:
        """Drop a mirroring session (used before opening a meeting).

        Restarting the receiver is the only reliable way to disconnect an
        AirPlay client, and it comes straight back up ready for the next one.
        """
        if not self.sharing:
            return False
        log_event(log, logging.INFO, "airplay.session_interrupted", reason="meeting starting")
        with self._lock:
            self._sharing = False
            self._session_ended = datetime.now(timezone.utc)
        self._notify(False)
        if self.config.bool_("DEV_MODE"):
            return True
        return self.system.restart(
            "room-airplay.service", min_interval=5.0, reason="clear AirPlay for a meeting"
        )

    def simulate_sharing(self, sharing: bool) -> bool:
        """Development-mode helper so the sharing screen can be designed."""
        if not self.config.bool_("DEV_MODE"):
            return False
        self.handle_event("connected" if sharing else "disconnected", client="Mock MacBook")
        return True
