"""Health monitoring and self-healing.

Runs one loop that answers two questions every few seconds:

* **Is the room healthy?** — the answer is served by ``GET /api/health``.
* **If not, what can be fixed automatically?** — and then does it.

Recovery is deliberately conservative and rate-limited. Restarting things is
cheap but not free (a Chromium restart briefly blanks the TV), so each action
has a cooling-off period and a reason that ends up in the journal. Everything
this service does is also something systemd or ``scripts/watchdog.sh`` would
eventually do on its own; doing it here is simply faster and better explained.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from datetime import datetime, timezone

from . import paths
from .airplay_service import AirPlayService
from .cast_service import CastService
from .miracast_service import MiracastService
from .browser_service import BrowserService
from .calendar_service import CalendarService
from .config import ConfigManager
from .logging_setup import get_logger, log_event
from .meeting_service import MeetingService
from .models import FAIL, MODE_OFFLINE, OFF, OK, UNKNOWN, WARN
from .poly_service import PolyService
from .store import write_json
from .system_service import SystemService

log = get_logger("health")

#: Consecutive failed probes before the browser is restarted.
BROWSER_FAILURES_BEFORE_RESTART = 4
#: Consecutive failed probes before AirPlay is restarted.
AIRPLAY_FAILURES_BEFORE_RESTART = 4
#: Network probes: a short TCP connect is a better test than ping (no root, and
#: ICMP is often filtered).
NETWORK_PROBE_PORTS = (443, 53, 80)
NETWORK_PROBE_TIMEOUT = 2.5


class HealthService:
    """Watches everything and repairs what it can."""

    def __init__(
        self,
        config: ConfigManager,
        calendar: CalendarService,
        browser: BrowserService,
        airplay: AirPlayService,
        cast: CastService,
        miracast: MiracastService,
        poly: PolyService,
        room: MeetingService,
        system: SystemService,
    ) -> None:
        self.config = config
        self.calendar = calendar
        self.browser = browser
        self.airplay = airplay
        self.cast = cast
        self.miracast = miracast
        self.poly = poly
        self.room = room
        self.system = system

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = datetime.now(timezone.utc)

        self._network_ok = True
        self._network_checked = 0.0
        self._browser_failures = 0
        self._airplay_failures = 0
        self._recoveries: list[dict[str, object]] = []
        self._checks = 0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="health-monitor", daemon=True)
        self._thread.start()
        log_event(log, logging.INFO, "health.monitor_started")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    def _run(self) -> None:
        # Give the rest of the system a moment to come up before judging it.
        if self._stop.wait(timeout=8.0):
            return
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:  # pragma: no cover - must never die
                log.exception("health.check_failed")
            self._stop.wait(timeout=max(5, self.config.int_("HEALTH_CHECK_SECONDS")))

    # -- network ---------------------------------------------------------
    def check_network(self, *, force: bool = False) -> bool:
        """TCP-connect to the configured hosts. Cached for a few seconds."""
        now = time.monotonic()
        with self._lock:
            if not force and now - self._network_checked < 5.0:
                return self._network_ok

        hosts = self.config.list_("NETWORK_CHECK_HOSTS") or ["1.1.1.1"]
        reachable = False
        for host in hosts[:4]:
            for port in NETWORK_PROBE_PORTS[:2]:
                try:
                    with socket.create_connection((host, port), NETWORK_PROBE_TIMEOUT):
                        reachable = True
                        break
                except (OSError, ValueError):
                    continue
            if reachable:
                break

        with self._lock:
            was_ok = self._network_ok
            self._network_ok = reachable
            self._network_checked = now

        if was_ok and not reachable:
            log_event(log, logging.WARNING, "network.unavailable", hosts=",".join(hosts[:2]))
        elif not was_ok and reachable:
            log_event(log, logging.INFO, "network.restored")
            # Get fresh meetings as soon as the network comes back.
            self.calendar.refresh_now()

        self.room.set_network_ok(reachable)
        return reachable

    # -- the check -------------------------------------------------------
    def check(self) -> dict[str, object]:
        """One full pass: probe everything, then repair what is broken."""
        with self._lock:
            self._checks += 1

        network_ok = self.check_network()
        report = self.report(network_ok=network_ok)

        self._recover_browser(report)
        self._recover_airplay(report)

        # Publish state for scripts/watchdog.sh, which runs outside this process.
        write_json(
            paths.STATE_FILE,
            {
                "updated": datetime.now(timezone.utc).isoformat(),
                "mode": report.get("mode"),
                "overall": report.get("status"),
                "network_ok": network_ok,
                "browser_ok": bool((report.get("browser") or {}).get("ok")),
                "calendar_ok": bool((report.get("calendar") or {}).get("ok")),
                "pid": __import__("os").getpid(),
            },
            mode=0o644,
        )
        return report

    def _recover_browser(self, report: dict[str, object]) -> None:
        if not self.config.bool_("AUTO_RECOVER_BROWSER"):
            return
        browser = report.get("browser") or {}
        if not browser.get("enabled", True):
            return
        if browser.get("alive"):
            with self._lock:
                self._browser_failures = 0
            # Alive, but possibly showing the wrong thing.
            action = self.browser.enforce_target()
            if action.startswith("recovered"):
                self._note_recovery("browser", action)
            return

        with self._lock:
            self._browser_failures += 1
            failures = self._browser_failures

        if failures == 1:
            log_event(log, logging.WARNING, "browser.not_responding")
        if failures >= BROWSER_FAILURES_BEFORE_RESTART:
            if self.browser.restart_browser(reason="not responding on the debug port"):
                self._note_recovery("browser", "restarted Chromium")
                with self._lock:
                    self._browser_failures = 0

    def _recover_airplay(self, report: dict[str, object]) -> None:
        airplay = report.get("airplay") or {}
        if not airplay.get("enabled") or airplay.get("mock"):
            return
        if airplay.get("status") in (OK, OFF):
            with self._lock:
                self._airplay_failures = 0
            return
        if not airplay.get("uxplay_installed", True):
            return  # nothing to restart; the dashboard already says so

        with self._lock:
            self._airplay_failures += 1
            failures = self._airplay_failures

        if failures >= AIRPLAY_FAILURES_BEFORE_RESTART:
            if self.airplay.restart(reason="AirPlay receiver not healthy"):
                self._note_recovery("airplay", "restarted the AirPlay receiver")
                with self._lock:
                    self._airplay_failures = 0

    def _note_recovery(self, component: str, action: str) -> None:
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "component": component,
            "action": action,
        }
        with self._lock:
            self._recoveries.append(entry)
            self._recoveries = self._recoveries[-20:]

    # -- reporting -------------------------------------------------------
    def report(self, *, network_ok: bool | None = None) -> dict[str, object]:
        """The body of ``GET /api/health``."""
        if network_ok is None:
            network_ok = self.check_network()

        calendar = self.calendar.status()
        browser = self.browser.status()
        airplay = self.airplay.status()
        cast = self.cast.status()
        miracast = self.miracast.status()
        poly = self.poly.status()
        mode = self.room.mode

        calendar_status = self._calendar_status(calendar)
        if not browser.get("enabled", True):
            browser_status = OFF
        else:
            browser_status = OK if browser.get("alive") else FAIL
        network_status = OK if network_ok else FAIL

        components = {
            "backend": OK,
            "calendar": calendar_status,
            "browser": browser_status,
            "airplay": airplay.get("status", UNKNOWN),
            "miracast": miracast.get("status", UNKNOWN),
            "cast": cast.get("status", UNKNOWN),
            "camera": (poly.get("camera") or {}).get("status", UNKNOWN),
            "microphone": (poly.get("microphone") or {}).get("status", UNKNOWN),
            "speaker": (poly.get("speaker") or {}).get("status", UNKNOWN),
            "network": network_status,
        }
        overall = self._overall(components)

        with self._lock:
            checks = self._checks
            recoveries = list(self._recoveries)

        uptime = (datetime.now(timezone.utc) - self._started_at).total_seconds()

        return {
            "status": overall,
            "mode": mode,
            "components": components,
            "backend": {
                "status": OK,
                "uptime_seconds": round(uptime),
                "checks": checks,
                "version": self._version(),
                "dev_mode": self.config.bool_("DEV_MODE"),
                "setup_required": self.config.setup_required(),
                "config_warnings": list(self.config.warnings),
            },
            # What the room decided this machine is, and how hard it is being
            # pushed. First question worth asking when a room feels sluggish.
            "performance": self.config.performance_report(),
            "calendar": calendar,
            "browser": browser,
            "airplay": airplay,
            "miracast": miracast,
            "cast": cast,
            "poly": poly,
            "network": {
                "status": network_status,
                "ok": network_ok,
                "hosts": self.config.list_("NETWORK_CHECK_HOSTS"),
                "addresses": self.system.local_ip_addresses(),
                "hostname": self.system.hostname(),
            },
            "host": {
                "uptime_seconds": round(self.system.uptime_seconds()),
                "load": [round(value, 2) for value in self.system.load_average()],
                "temperature_c": self.system.temperature_celsius(),
                "disk_free_percent": self.system.disk_free_percent(),
                "memory_available_mb": self.system.memory_available_mb(),
            },
            "recoveries": recoveries,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _calendar_status(self, calendar: dict[str, object]) -> str:
        if calendar.get("source") == "none":
            return OFF
        if not calendar.get("configured"):
            return WARN
        if calendar.get("ok"):
            return OK
        # An outage with cached meetings still on screen is a warning, not a
        # failure: the room is usable.
        return WARN if calendar.get("meeting_count") else FAIL

    @staticmethod
    def _overall(components: dict[str, str]) -> str:
        """Worst status wins, but a disabled component is not a problem.

        The camera, microphone and speaker are reported but do not by themselves
        make the room "broken" — a room with no conference bar plugged in can
        still show its calendar and share a screen.
        """
        critical = ("backend", "browser")
        important = ("calendar", "network", "airplay")

        for name in critical:
            status = components.get(name)
            if status == OFF:
                continue  # switched off on purpose, not broken
            if status in (FAIL, UNKNOWN):
                return FAIL
        if any(components.get(name) == FAIL for name in important):
            return WARN
        if any(value == WARN for value in components.values()):
            return WARN
        if any(components.get(name) == FAIL for name in components):
            return WARN
        return OK

    @staticmethod
    def _version() -> str:
        try:
            from . import __version__

            return __version__
        except Exception:  # pragma: no cover
            return "unknown"
