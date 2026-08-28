"""Chromium kiosk control.

The browser is *launched* by systemd (``room-kiosk.service`` running
``scripts/start-kiosk.sh``) and *driven* from here over the DevTools protocol.
Keeping those separate is deliberate: if the backend dies, systemd keeps the TV
showing the dashboard, and if Chromium dies, systemd restarts it — neither
depends on the other being healthy.

This service tracks what the TV *should* be showing and corrects drift. Two
failure modes matter in a real room:

* Chromium gets stuck on a finished meeting → we navigate back to the dashboard.
* Chromium wanders somewhere unexpected (a crash-recovery page, an error page)
  → we put it back on the dashboard.

Join automation runs on its own thread with a deadline, retries with a gentle
backoff, and stops the moment the page looks like it is in the call.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .cdp import CDPError, ChromeDevTools
from .config import ConfigManager
from .join_flows import (
    build_click_script,
    build_in_call_script,
    flow_for,
    ordered_button_texts,
    prepare_url,
)
from .logging_setup import get_logger, log_event, redact_url
from .models import Meeting
from .system_service import SystemService

log = get_logger("browser")

#: What the TV is meant to be showing.
TARGET_DASHBOARD = "dashboard"
TARGET_MEETING = "meeting"
TARGET_UNKNOWN = "unknown"


@dataclass
class JoinAttempt:
    """Record of the most recent auto-join, shown on the diagnostics page."""

    meeting_id: str = ""
    provider: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    clicks: list[str] = field(default_factory=list)
    in_call: bool = False
    passes: int = 0
    error: str = ""
    gave_up: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "meeting_id": self.meeting_id,
            "provider": self.provider,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "clicks": list(self.clicks),
            "in_call": self.in_call,
            "passes": self.passes,
            "error": self.error,
            "gave_up": self.gave_up,
        }


class BrowserService:
    """Owns the relationship between the appliance and the kiosk browser."""

    def __init__(self, config: ConfigManager, system: SystemService) -> None:
        self.config = config
        self.system = system
        self._cdp = ChromeDevTools(port=config.int_("CHROMIUM_DEBUG_PORT"))
        self._lock = threading.RLock()
        self._target = TARGET_DASHBOARD
        self._target_url = ""
        self._meeting_id = ""
        self._join_thread: threading.Thread | None = None
        self._join_stop = threading.Event()
        self._last_attempt = JoinAttempt()
        self._last_alive: float = 0.0
        self._last_seen_url = ""
        self._consecutive_probe_failures = 0
        config.on_change(self._on_config_change)

    # -- URLs ------------------------------------------------------------
    def dashboard_url(self) -> str:
        port = self.config.int_("DASHBOARD_PORT")
        return f"http://127.0.0.1:{port}/"

    def _on_config_change(self, values: dict[str, object], changed: set[str]) -> None:
        if "CHROMIUM_DEBUG_PORT" in changed:
            with self._lock:
                self._cdp.close()
                self._cdp = ChromeDevTools(port=self.config.int_("CHROMIUM_DEBUG_PORT"))

    # -- state -----------------------------------------------------------
    @property
    def target(self) -> str:
        with self._lock:
            return self._target

    @property
    def meeting_id(self) -> str:
        with self._lock:
            return self._meeting_id

    @property
    def last_attempt(self) -> JoinAttempt:
        with self._lock:
            return self._last_attempt

    def is_alive(self) -> bool:
        """True if Chromium is answering on its debug port."""
        alive = self._cdp.is_alive()
        with self._lock:
            if alive:
                self._last_alive = time.monotonic()
                self._consecutive_probe_failures = 0
            else:
                self._consecutive_probe_failures += 1
        return alive

    def current_url(self) -> str:
        url = self._cdp.current_url()
        if url:
            with self._lock:
                self._last_seen_url = url
        return url

    @property
    def enabled(self) -> bool:
        """False while the kiosk is deliberately switched off (development)."""
        return self.config.bool_("KIOSK_ENABLED")

    def status(self) -> dict[str, object]:
        if not self.enabled:
            return {
                "ok": True,
                "enabled": False,
                "alive": False,
                "debug_port": self._cdp.port,
                "target": self.target,
                "meeting_id": self.meeting_id,
                "current_url": "",
                "on_dashboard": False,
                "kiosk_unit": "disabled",
                "last_join": self.last_attempt.to_dict(),
            }
        alive = self.is_alive()
        url = self.current_url() if alive else ""
        with self._lock:
            return {
                "ok": alive,
                "enabled": True,
                "alive": alive,
                "debug_port": self._cdp.port,
                "target": self._target,
                "meeting_id": self._meeting_id,
                "current_url": redact_url(url) if url else "",
                "on_dashboard": self._looks_like_dashboard(url),
                "failed_probes": self._consecutive_probe_failures,
                "kiosk_unit": self.system.unit_state("room-kiosk.service"),
                "last_join": self._last_attempt.to_dict(),
            }

    def _looks_like_dashboard(self, url: str) -> bool:
        if not url:
            return False
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        if parts.hostname not in ("127.0.0.1", "localhost"):
            return False
        return parts.port in (None, self.config.int_("DASHBOARD_PORT"))

    # -- navigation ------------------------------------------------------
    def go_home(self, *, reason: str = "") -> bool:
        """Put the TV back on the room dashboard."""
        self._stop_join_automation()
        url = self.dashboard_url()
        with self._lock:
            self._target = TARGET_DASHBOARD
            self._target_url = url
            self._meeting_id = ""
        ok = self._cdp.navigate(url)
        log_event(
            log,
            logging.INFO,
            "browser.returning_to_dashboard" if ok else "browser.navigate_home_failed",
            reason=reason or "requested",
        )
        return ok

    def open_meeting(self, meeting: Meeting, *, reason: str = "") -> bool:
        """Navigate to a meeting and start best-effort join automation."""
        if not meeting.join_url:
            log_event(log, logging.WARNING, "meeting.no_link", meeting=meeting.uid)
            return False

        self._stop_join_automation()
        url = prepare_url(meeting.provider_id, meeting.join_url)

        # Pre-grant camera and microphone for the meeting's origin so no one has
        # to click a permission prompt on a TV with no keyboard.
        try:
            parts = urlsplit(url)
            if parts.scheme and parts.hostname:
                self._cdp.grant_media_permissions(f"{parts.scheme}://{parts.netloc}")
        except ValueError:
            pass

        with self._lock:
            self._target = TARGET_MEETING
            self._target_url = url
            self._meeting_id = meeting.uid

        ok = self._cdp.navigate(url, timeout=15.0)
        log_event(
            log,
            logging.INFO if ok else logging.ERROR,
            f"meeting.opening_{meeting.provider_id or 'link'}" if ok else "meeting.open_failed",
            provider=meeting.provider_id or "unknown",
            title=meeting.title[:60],
            reason=reason or "requested",
            url=redact_url(url),
        )
        if not ok:
            return False

        self._cdp.bring_to_front()
        if self.config.bool_("AUTO_CLICK_JOIN"):
            self._start_join_automation(meeting)
        return True

    # -- join automation -------------------------------------------------
    def _start_join_automation(self, meeting: Meeting) -> None:
        self._join_stop.clear()
        attempt = JoinAttempt(
            meeting_id=meeting.uid,
            provider=meeting.provider_id,
            started_at=datetime.now(timezone.utc),
        )
        with self._lock:
            self._last_attempt = attempt
            self._join_thread = threading.Thread(
                target=self._join_loop,
                args=(meeting, attempt),
                name="join-automation",
                daemon=True,
            )
            thread = self._join_thread
        thread.start()

    def _stop_join_automation(self) -> None:
        self._join_stop.set()
        with self._lock:
            thread = self._join_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)

    def _join_loop(self, meeting: Meeting, attempt: JoinAttempt) -> None:
        """Press the join buttons, giving up quietly after the deadline."""
        flow = flow_for(meeting.provider_id)
        deadline = time.monotonic() + self.config.int_("AUTO_JOIN_TIMEOUT_SECONDS")
        script = build_click_script(
            ordered_button_texts(meeting.provider_id, self.config.list_("JOIN_BUTTON_TEXTS")),
            display_name=self.config.join_display_name(),
            fill_name=flow.asks_for_name,
        )
        in_call_script = build_in_call_script()

        # Let the page load before poking at it. Clicking into a half-drawn
        # page is worse than waiting: the buttons are not there yet, and on
        # slow hardware the whole attempt can expire before the page is ready.
        settle = self.config.float_("JOIN_SETTLE_SECONDS") or flow.settle_seconds
        log_event(
            log, logging.DEBUG, "meeting.join_waiting_for_page",
            provider=meeting.provider_id, seconds=settle,
        )
        if self._join_stop.wait(timeout=settle):
            return

        interval = 2.0
        while not self._join_stop.is_set() and time.monotonic() < deadline:
            # Abandon automation if something else moved the browser on.
            with self._lock:
                if self._meeting_id != meeting.uid:
                    return

            try:
                in_call = self._cdp.evaluate(in_call_script, timeout=6.0)
                if str(in_call).lower() == "true":
                    attempt.in_call = True
                    attempt.finished_at = datetime.now(timezone.utc)
                    log_event(
                        log, logging.INFO, "meeting.join_automation_succeeded",
                        provider=meeting.provider_id, passes=attempt.passes,
                        clicks=",".join(attempt.clicks[-3:]) or "none",
                    )
                    return
            except CDPError as exc:
                attempt.error = str(exc)

            try:
                raw = self._cdp.evaluate(script, timeout=8.0)
                attempt.passes += 1
                payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
                clicked = (payload or {}).get("clicked")
                if clicked:
                    attempt.clicks.append(str(clicked)[:60])
                    log_event(
                        log, logging.INFO, "meeting.join_automation_attempted",
                        provider=meeting.provider_id, button=str(clicked)[:40],
                        pass_number=attempt.passes,
                    )
                    # A click usually triggers a page change; give it room.
                    interval = 3.0
                else:
                    interval = min(6.0, interval * 1.4)
            except CDPError as exc:
                attempt.error = str(exc)
                log_event(
                    log, logging.DEBUG, "meeting.join_probe_failed", error=str(exc)[:120]
                )
                interval = min(8.0, interval * 1.6)
            except (TypeError, ValueError) as exc:
                attempt.error = f"unreadable automation result: {exc}"

            if self._join_stop.wait(timeout=interval):
                return

        if not self._join_stop.is_set():
            attempt.gave_up = True
            attempt.finished_at = datetime.now(timezone.utc)
            log_event(
                log,
                logging.WARNING,
                "meeting.join_automation_failed",
                provider=meeting.provider_id,
                passes=attempt.passes,
                clicks=",".join(attempt.clicks) or "none",
                error=attempt.error or "no join button matched",
                note="the room can still join with the JOIN button",
            )

    def retry_join(self) -> bool:
        """Run the join automation again for the meeting already on screen."""
        with self._lock:
            meeting_id = self._meeting_id
            provider = self._last_attempt.provider
        if not meeting_id:
            return False
        placeholder = Meeting(
            uid=meeting_id,
            title="",
            start=datetime.now(timezone.utc),
            end=datetime.now(timezone.utc),
            provider_id=provider,
            join_url="already-open",
        )
        self._stop_join_automation()
        self._start_join_automation(placeholder)
        return True

    def leave_meeting(self, *, reason: str = "hangup") -> bool:
        """Try to hang up politely, then return to the dashboard regardless."""
        with self._lock:
            in_meeting = self._target == TARGET_MEETING
        if in_meeting:
            hangup_texts = [
                "Leave call", "Leave meeting", "Leave", "Hang up", "End call", "End meeting",
            ]
            try:
                self._cdp.evaluate(
                    build_click_script(hangup_texts, fill_name=False), timeout=6.0
                )
            except CDPError:
                pass  # Navigating away ends the call anyway.
            time.sleep(0.6)
        return self.go_home(reason=reason)

    def bring_to_front(self) -> bool:
        """Raise the kiosk window (after a mirroring session ends)."""
        return self._cdp.bring_to_front()

    # -- media controls the remote uses ---------------------------------
    def toggle_meeting_mute(self) -> bool:
        """Press the meeting page's own mute control, where there is one."""
        texts = ["Mute microphone", "Unmute microphone", "Mute", "Unmute"]
        try:
            raw = self._cdp.evaluate(build_click_script(texts, fill_name=False), timeout=6.0)
            payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
            return bool((payload or {}).get("clicked"))
        except (CDPError, TypeError, ValueError):
            return False

    def toggle_meeting_camera(self) -> bool:
        texts = ["Turn camera on", "Turn camera off", "Start video", "Stop video", "Camera"]
        try:
            raw = self._cdp.evaluate(build_click_script(texts, fill_name=False), timeout=6.0)
            payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
            return bool((payload or {}).get("clicked"))
        except (CDPError, TypeError, ValueError):
            return False

    # -- recovery --------------------------------------------------------
    def restart_browser(self, *, reason: str = "") -> bool:
        """Ask systemd to restart the kiosk."""
        self._stop_join_automation()
        self._cdp.close()
        with self._lock:
            self._target = TARGET_DASHBOARD
            self._meeting_id = ""
        ok = self.system.restart("room-kiosk.service", reason=reason or "browser recovery")
        log_event(
            log,
            logging.WARNING if ok else logging.ERROR,
            "browser.restarted" if ok else "browser.restart_failed",
            reason=reason or "browser recovery",
        )
        return ok

    def enforce_target(self) -> str:
        """Correct the browser if it has drifted. Returns the action taken.

        Called from the health loop. Only acts when it is confident: an
        unreachable browser is left to systemd, and a meeting page is never
        second-guessed (a meeting can legitimately navigate through several
        URLs).
        """
        if not self.enabled:
            return "kiosk-disabled"
        if not self.config.bool_("AUTO_RECOVER_BROWSER"):
            return "disabled"
        if not self.is_alive():
            return "browser-unreachable"

        url = self.current_url()
        if not url:
            return "no-page"

        with self._lock:
            target = self._target

        if target == TARGET_DASHBOARD:
            if self._looks_like_dashboard(url):
                return "ok"
            # Chromium is showing something else while we believe it is home.
            log_event(
                log, logging.WARNING, "browser.drifted_from_dashboard",
                url=redact_url(url),
            )
            self.go_home(reason="drifted from dashboard")
            return "recovered-dashboard"

        if target == TARGET_MEETING:
            # A blank or error page means the meeting never loaded.
            if url.startswith(("chrome-error://", "about:blank", "chrome://network-error")):
                log_event(log, logging.WARNING, "browser.meeting_page_error", url=url[:60])
                self.go_home(reason="meeting page failed to load")
                return "recovered-error-page"
            return "ok"

        return "ok"
