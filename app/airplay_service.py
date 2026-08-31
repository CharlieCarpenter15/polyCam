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
#: UxPlay exiting this many times inside the window below means it is not
#: starting at all, however healthy the supervisor looks. Almost always either
#: avahi-daemon being down or no display to open a window on — in both cases
#: nothing is advertised, so the room never appears in Screen Mirroring.
CRASH_LOOP_EXITS = 3
CRASH_LOOP_WINDOW_SECONDS = 180.0


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
        self._exits: list[float] = []
        self._detail = ""
        self._last_event = ""
        self._listeners: list = []

    # -- events from the supervisor --------------------------------------
    def on_change(self, callback) -> None:
        """Register ``callback(sharing: bool)``, called when sharing starts/stops."""
        with self._lock:
            self._listeners.append(callback)

    def handle_event(
        self,
        event: str,
        *,
        client: str = "",
        detail: str = "",
        running: bool | None = None,
    ) -> dict[str, object]:
        """Process one supervisor event.

        Recognised events: ``started`` (UxPlay process up), ``exited`` (it died,
        with ``detail`` saying why), ``stopped`` (the supervisor is going away),
        ``connected`` (a device began mirroring), ``disconnected``, ``heartbeat``.

        A heartbeat says the *supervisor* is alive, which is a different claim
        from "UxPlay is up" — a supervisor restarting a receiver that refuses to
        start is exactly the case worth reporting. It carries ``running`` to say
        which, so that a backend that restarted mid-session still learns that
        UxPlay is fine without waiting for it to be restarted.
        """
        event = (event or "").strip().lower()
        changed = False
        now = datetime.now(timezone.utc)

        with self._lock:
            self._last_heartbeat = time.monotonic()
            self._last_event = event

            if event == "started":
                self._uxplay_running = True
                self._detail = ""
            elif event == "heartbeat":
                if running is not None:
                    self._uxplay_running = bool(running)
            elif event in ("exited", "restarted"):
                # "restarted" is what an older supervisor calls the same thing.
                self._uxplay_running = False
                self._restarts += 1
                self._exits.append(time.monotonic())
                self._exits = self._exits[-20:]
                if detail:
                    self._detail = detail[:200]
                # UxPlay dying tears down any mirroring session.
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
                self._detail = ""
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

        if event in ("exited", "restarted"):
            log_event(
                log, logging.WARNING, "airplay.process_exited",
                restarts=self._restarts, reason=detail[:120] or "unknown",
            )
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

    def _crash_looping(self) -> bool:
        """True when UxPlay keeps exiting instead of staying up.

        Called with the lock held; prunes the window as it goes.
        """
        now = time.monotonic()
        self._exits = [at for at in self._exits if now - at < CRASH_LOOP_WINDOW_SECONDS]
        return len(self._exits) >= CRASH_LOOP_EXITS

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
                    "detail": "",
                    "restarts": self._restarts,
                }

        sharing = self.sharing
        with self._lock:
            stale = self._stale_heartbeat()
            running = self._uxplay_running
            restarts = self._restarts
            client = self._client
            started = self._session_started
            crash_looping = self._crash_looping()
            detail = self._detail

        unit_state = self.system.unit_state("room-airplay.service")
        installed = bool(which("uxplay"))
        if not installed:
            status = FAIL
            detail = "UxPlay is not installed."
        elif unit_state != "active":
            status = FAIL
        elif crash_looping:
            # The unit is up and the supervisor is doing its job, but the
            # receiver it keeps starting will not stay up, so nothing is being
            # advertised and the room cannot appear in Screen Mirroring. Green
            # here is the worst possible answer: it sends someone hunting the
            # network for a fault that is on this Pi.
            status = FAIL
            detail = detail or "The AirPlay receiver keeps exiting."
        elif not running:
            status = WARN
            detail = detail or "The AirPlay receiver is not running."
        elif stale:
            status = WARN
            detail = "The AirPlay supervisor has stopped reporting."
        else:
            status = OK
            detail = ""

        return {
            "enabled": True,
            "status": status,
            "sharing": sharing,
            "name": self.config.airplay_name(),
            "client": client,
            "since": started.isoformat() if started and sharing else None,
            "unit": unit_state,
            "uxplay_installed": installed,
            "uxplay_running": running,
            "supervisor_stale": stale,
            "crash_looping": crash_looping,
            "detail": detail,
            "restarts": restarts,
            "pin_required": bool(self.config.str_("AIRPLAY_PIN")),
        }

    # -- control ---------------------------------------------------------
    def restart(self, *, reason: str = "") -> bool:
        with self._lock:
            self._sharing = False
            self._uxplay_running = False
            # A deliberate restart is not evidence of a crash loop; let the
            # fresh supervisor speak for itself.
            self._exits = []
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
