"""Miracast receiver state, as reported by the sink supervisor.

This is the Windows equivalent of AirPlay, and it is what somebody means when
they say "just stream it to the TV": Win+K, pick the room, done. Nothing is
installed on the laptop and no address is typed, because the list is drawn by
Windows itself.

The receiver process is run by its own systemd unit (``room-miracast.service``
→ ``scripts/start-miracast.sh``), which keeps it alive independently of the
backend and posts session events here. Deliberately the same arrangement as
AirPlay, for the same reason: people must still be able to put a screen on the
TV while the room software is restarting.

**What this cannot do anything about.** A Miracast receiver announces itself as
a Wi-Fi Direct group owner — that is true even in "over Infrastructure" mode,
where the video then travels over the ordinary network. A card acting as a group
owner generally cannot also be associated with a normal network, so the room
needs either a free radio (the Pi on Ethernet) or a second adapter.
``scripts/detect-miracast.sh`` reports which of those a given room has, and
:meth:`MiracastService.status` surfaces the answer on the dashboard rather than
letting it present as "the room just never appears in the list".

Structured like :class:`~app.airplay_service.AirPlayService` on purpose: same
event names, same heartbeat, same hard session expiry. The two are not merged
into a shared base class deliberately — AirPlay works and is in daily use, and
the duplication is confined to bookkeeping that is easy to read twice. If a
third supervised receiver ever appears, unify them then.
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

log = get_logger("miracast")

#: If no heartbeat arrives for this long, assume the supervisor died.
HEARTBEAT_TIMEOUT_SECONDS = 150.0

#: A mirroring session with no further events for this long is treated as over.
SESSION_MAX_SILENCE_SECONDS = 20 * 60.0

#: Backends the supervisor knows how to drive. "auto" picks whichever is
#: installed, which is what a room should do when nobody has an opinion.
BACKENDS = ("auto", "miraclecast", "lazycast")

#: How the two backends are recognised on disk.
BACKEND_COMMANDS = {
    "miraclecast": "miracle-sinkctl",
    "lazycast": "",  # a checkout, not a command; resolved from MIRACAST_LAZYCAST_DIR
}


class MiracastService:
    """Tracks whether a Windows laptop is mirroring, and whether the sink is well."""

    def __init__(self, config: ConfigManager, system: SystemService) -> None:
        self.config = config
        self.system = system
        self._lock = threading.RLock()
        self._sharing = False
        self._client = ""
        self._session_started: datetime | None = None
        self._session_ended: datetime | None = None
        self._last_heartbeat = 0.0
        self._sink_running = False
        self._restarts = 0
        self._last_event = ""
        #: What the supervisor said was wrong, in words, when it could not
        #: start. The radio being busy is the common one and it is invisible
        #: from here, so the supervisor has to tell us.
        self._blocked = ""
        self._backend = ""
        self._listeners: list = []

    # -- events from the supervisor --------------------------------------
    def on_change(self, callback) -> None:
        """Register ``callback(sharing: bool)``, called when sharing starts/stops."""
        with self._lock:
            self._listeners.append(callback)

    def handle_event(
        self, event: str, *, client: str = "", detail: str = "", backend: str = ""
    ) -> dict[str, object]:
        """Process one supervisor event.

        Recognised events: ``started`` (the sink is up), ``stopped``,
        ``connected`` (a laptop began mirroring), ``disconnected``,
        ``heartbeat``, ``restarted``, and ``blocked`` — which the supervisor
        sends when it cannot run at all, with ``detail`` saying why.
        """
        event = (event or "").strip().lower()
        changed = False
        now = datetime.now(timezone.utc)

        with self._lock:
            self._last_heartbeat = time.monotonic()
            self._last_event = event
            if backend:
                self._backend = backend[:20]

            if event in ("started", "heartbeat"):
                self._sink_running = True
                self._blocked = ""
            elif event == "restarted":
                self._sink_running = True
                self._restarts += 1
                if self._sharing:
                    self._sharing = False
                    self._session_ended = now
                    changed = True
            elif event == "stopped":
                self._sink_running = False
                if self._sharing:
                    self._sharing = False
                    self._session_ended = now
                    changed = True
            elif event == "blocked":
                # Running but unable to receive: the radio is in use, the
                # driver will not do Wi-Fi Direct, the backend is missing.
                self._sink_running = False
                self._blocked = detail[:200]
                if self._sharing:
                    self._sharing = False
                    self._session_ended = now
                    changed = True
            elif event == "connected":
                self._sink_running = True
                self._blocked = ""
                if not self._sharing:
                    self._sharing = True
                    self._session_started = now
                    self._client = client[:60]
                    changed = True
            elif event == "disconnected":
                self._sink_running = True
                if self._sharing:
                    self._sharing = False
                    self._session_ended = now
                    self._client = ""
                    changed = True

            sharing = self._sharing

        if event == "restarted":
            log_event(log, logging.WARNING, "miracast.process_restarted",
                      restarts=self._restarts)
        elif event == "blocked":
            log_event(log, logging.ERROR, "miracast.blocked", reason=detail[:120])
        elif changed and sharing:
            log_event(log, logging.INFO, "miracast.sharing_started",
                      client=client[:40] or "unknown")
        elif changed and not sharing:
            log_event(log, logging.INFO, "miracast.sharing_stopped")

        if changed:
            self._notify(sharing)
        return {"ok": True, "sharing": sharing}

    def _notify(self, sharing: bool) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(sharing)
            except Exception:  # pragma: no cover - a listener must not break sharing
                log.exception("miracast.listener_failed")

    # -- state -----------------------------------------------------------
    @property
    def sharing(self) -> bool:
        """True while a laptop is mirroring, with a safety timeout."""
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
                    log_event(log, logging.WARNING, "miracast.session_expired")
                    return False
            return True

    def _stale_heartbeat(self) -> bool:
        if not self._last_heartbeat:
            return True
        return (time.monotonic() - self._last_heartbeat) > HEARTBEAT_TIMEOUT_SECONDS

    def backend(self) -> str:
        """Which sink implementation is in use, or "" if not yet known."""
        with self._lock:
            if self._backend:
                return self._backend
        configured = self.config.str_("MIRACAST_BACKEND")
        return configured if configured != "auto" else ""

    def installed_backend(self) -> str:
        """Which backend this machine could run, from what is on disk."""
        configured = self.config.str_("MIRACAST_BACKEND")
        if configured == "miraclecast":
            return "miraclecast" if which("miracle-sinkctl") else ""
        if configured == "lazycast":
            return "lazycast" if self._lazycast_present() else ""
        if which("miracle-sinkctl"):
            return "miraclecast"
        if self._lazycast_present():
            return "lazycast"
        return ""

    def _lazycast_present(self) -> bool:
        from pathlib import Path

        directory = self.config.str_("MIRACAST_LAZYCAST_DIR")
        if not directory:
            return False
        try:
            return (Path(directory) / "all.sh").is_file()
        except OSError:
            return False

    def status(self) -> dict[str, object]:
        """What the dashboard, the health report and diagnostics all read."""
        enabled = self.config.bool_("MIRACAST_ENABLED")
        if not enabled:
            return {"enabled": False, "status": OFF, "sharing": False, "name": ""}

        if self.config.bool_("DEV_MODE"):
            with self._lock:
                return {
                    "enabled": True,
                    "mock": True,
                    "status": OK,
                    "sharing": self._sharing,
                    "name": self.config.miracast_name(),
                    "client": self._client,
                    "unit": "simulated",
                    "backend": self._backend or "simulated",
                    "restarts": self._restarts,
                    "blocked": "",
                }

        sharing = self.sharing
        with self._lock:
            stale = self._stale_heartbeat()
            running = self._sink_running
            restarts = self._restarts
            client = self._client
            started = self._session_started
            blocked = self._blocked

        unit_state = self.system.unit_state("room-miracast.service")
        installed = self.installed_backend()

        if not installed:
            # Nothing to run. Neither backend is packaged for Raspberry Pi OS,
            # so this is the expected state until somebody installs one.
            status = FAIL
            blocked = blocked or (
                "No Miracast receiver software is installed. Run "
                "scripts/detect-miracast.sh, which says what to do."
            )
        elif blocked:
            status = FAIL
        elif unit_state == "active" and running and not stale:
            status = OK
        elif unit_state == "active":
            status = WARN
        else:
            status = FAIL

        return {
            "enabled": True,
            "status": status,
            "sharing": sharing,
            "name": self.config.miracast_name(),
            "client": client,
            "since": started.isoformat() if started and sharing else None,
            "unit": unit_state,
            "backend": self.backend() or installed,
            "installed_backend": installed,
            "supervisor_stale": stale,
            "restarts": restarts,
            "blocked": blocked,
            "pin_required": bool(self.config.str_("MIRACAST_PIN")),
        }

    # -- control ---------------------------------------------------------
    def restart(self, *, reason: str = "") -> bool:
        with self._lock:
            self._sharing = False
            self._sink_running = False
            self._blocked = ""
        return self.system.restart("room-miracast.service", reason=reason or "requested")

    def force_stop_sharing(self) -> bool:
        """Drop a mirroring session (used before opening a meeting).

        Restarting the receiver is the only reliable way to disconnect a
        Miracast client — the protocol has no "go away" a sink can send that
        every Windows build honours — and it comes back up ready for the next
        person. The same trade the AirPlay receiver makes.
        """
        if not self.sharing:
            return False
        log_event(log, logging.INFO, "miracast.session_interrupted",
                  reason="meeting starting")
        with self._lock:
            self._sharing = False
            self._session_ended = datetime.now(timezone.utc)
        self._notify(False)
        if self.config.bool_("DEV_MODE"):
            return True
        return self.system.restart(
            "room-miracast.service", min_interval=5.0,
            reason="clear Miracast for a meeting",
        )

    def simulate_sharing(self, sharing: bool) -> bool:
        """Development-mode helper so the sharing screen can be designed."""
        if not self.config.bool_("DEV_MODE"):
            return False
        self.handle_event(
            "connected" if sharing else "disconnected", client="Mock Windows PC"
        )
        return True
