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
backoff, and stops the moment the page looks like it is in the call. Exactly one
attempt may be live at a time: each gets its own stop event and a generation
number, so an attempt that has been cancelled can never be revived by the next
one starting. Two loops clicking at one page is what "it joins the meeting
several times" looked like from the room.
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
    build_mute_script,
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

#: How long the room keeps watching a lobby screen. Waiting to be admitted is
#: success in progress rather than a failure, so it is allowed to outlast the
#: join deadline — but not for ever: after this the room stops watching and
#: leaves the page exactly where a person would find it.
LOBBY_WAIT_SECONDS = 300.0


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
    filled_name: bool = False
    waiting: bool = False

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
            "filled_name": self.filled_name,
            "waiting": self.waiting,
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
        # One event per attempt, never reused: see _start_join_automation.
        self._join_stop: threading.Event | None = None
        self._join_generation = 0
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

    @property
    def join_generation(self) -> int:
        """Which join attempt is current. Only this one may click."""
        with self._lock:
            return self._join_generation

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
        """Begin a fresh attempt, cancelling any earlier one for good.

        The old code shared one stop event between attempts and *cleared* it
        here. A thread parked in an eight-second ``evaluate`` outlived the
        three-second join below, woke up, found the flag clear and carried on
        clicking alongside the new attempt — two loops, one page, a meeting
        joined twice. Now every attempt owns its event and a generation number,
        and neither is ever handed back.
        """
        self._stop_join_automation()
        attempt = JoinAttempt(
            meeting_id=meeting.uid,
            provider=meeting.provider_id,
            started_at=datetime.now(timezone.utc),
        )
        stop = threading.Event()
        with self._lock:
            self._join_generation += 1
            generation = self._join_generation
            self._join_stop = stop
            self._last_attempt = attempt
            thread = threading.Thread(
                target=self._join_loop,
                args=(meeting, attempt, stop, generation),
                name=f"join-automation-{generation}",
                daemon=True,
            )
            self._join_thread = thread
        thread.start()

    def _stop_join_automation(self) -> None:
        """Cancel the running attempt. A cancelled attempt never comes back."""
        with self._lock:
            stop = self._join_stop
            thread = self._join_thread
            self._join_stop = None
            self._join_thread = None
            # Moving the generation on matters as much as setting the event: a
            # thread blocked in a DevTools call can outlive the join() below,
            # and this is what stops it at its next checkpoint.
            self._join_generation += 1
        if stop is not None:
            stop.set()
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)

    def _join_cancelled(
        self, stop: threading.Event, generation: int, meeting_id: str
    ) -> bool:
        """True when this attempt must stop; checked at every step of the loop.

        Three ways an attempt ends early: its own stop event, a newer attempt
        taking over, or something else moving the browser somewhere new.
        """
        if stop.is_set():
            return True
        with self._lock:
            return self._join_generation != generation or self._meeting_id != meeting_id

    def _join_wait(
        self, stop: threading.Event, generation: int, meeting_id: str, seconds: float
    ) -> bool:
        """Sleep between passes. True means the attempt has been cancelled."""
        if stop.wait(timeout=max(0.0, seconds)):
            return True
        return self._join_cancelled(stop, generation, meeting_id)

    def _repeat_guard(
        self, recent: dict[str, tuple[str, float]]
    ) -> list[tuple[str, str]]:
        """Buttons the page must not be asked to press again just yet.

        A meeting page can look unchanged for several seconds after Join is
        pressed, so the next pass finds the same button and presses it again —
        which from a chair in the room looks like the meeting being joined over
        and over. Each entry is released as soon as the page moves to another
        URL, or after ``JOIN_REPEAT_GUARD_SECONDS``; 0 switches the guard off.
        """
        seconds = self.config.float_("JOIN_REPEAT_GUARD_SECONDS")
        if seconds <= 0:
            recent.clear()
            return []
        now = time.monotonic()
        for text in [t for t, (_, when) in recent.items() if now - when >= seconds]:
            del recent[text]
        return [(text, url) for text, (url, _) in recent.items()]

    def _mute_on_entry(self, meeting: Meeting) -> bool:
        """Mute the room as it enters the call, when JOIN_MUTE_ON_ENTRY is on.

        Once per join, straight after the in-call signal — never in the loop,
        because a room that keeps pressing mute is a room that ends up unmuted.
        """
        if not self.config.bool_("JOIN_MUTE_ON_ENTRY"):
            return False
        muted = self.mute_meeting_microphone()
        log_event(
            log,
            logging.INFO if muted else logging.DEBUG,
            "meeting.join_muted_on_entry" if muted else "meeting.join_mute_not_found",
            provider=meeting.provider_id,
        )
        return muted

    def _join_loop(
        self,
        meeting: Meeting,
        attempt: JoinAttempt,
        stop: threading.Event,
        generation: int,
    ) -> None:
        """Press the join buttons, giving up quietly after the deadline."""
        flow = flow_for(meeting.provider_id)
        deadline = time.monotonic() + self.config.int_("AUTO_JOIN_TIMEOUT_SECONDS")
        texts = ordered_button_texts(
            meeting.provider_id, self.config.list_("JOIN_BUTTON_TEXTS")
        )
        display_name = self.config.join_display_name()
        in_call_script = build_in_call_script()

        #: What was pressed, where, and when — the repeat guard is built from it.
        recent_clicks: dict[str, tuple[str, float]] = {}
        lobby_deadline: float | None = None

        # Let the page load before poking at it. Clicking into a half-drawn
        # page is worse than waiting: the buttons are not there yet, and on
        # slow hardware the whole attempt can expire before the page is ready.
        settle = self.config.float_("JOIN_SETTLE_SECONDS") or flow.settle_seconds
        log_event(
            log, logging.DEBUG, "meeting.join_waiting_for_page",
            provider=meeting.provider_id, seconds=settle,
        )
        if self._join_wait(stop, generation, meeting.uid, settle):
            return

        interval = 2.0
        while not self._join_cancelled(stop, generation, meeting.uid):
            now = time.monotonic()
            in_lobby = lobby_deadline is not None and now < lobby_deadline
            if now >= deadline and not in_lobby:
                break

            try:
                probe = self._cdp.evaluate(in_call_script, timeout=6.0)
                if str(probe).lower() == "true":
                    attempt.in_call = True
                    attempt.finished_at = datetime.now(timezone.utc)
                    muted = self._mute_on_entry(meeting)
                    log_event(
                        log, logging.INFO, "meeting.join_automation_succeeded",
                        provider=meeting.provider_id, passes=attempt.passes,
                        clicks=",".join(attempt.clicks[-3:]) or "none",
                        muted=muted,
                    )
                    return
            except CDPError as exc:
                attempt.error = str(exc)

            guard = self._repeat_guard(recent_clicks)
            try:
                raw = self._cdp.evaluate(
                    build_click_script(
                        texts,
                        display_name=display_name,
                        fill_name=flow.asks_for_name,
                        guarded_clicks=guard,
                    ),
                    timeout=8.0,
                )
                attempt.passes += 1
                payload = (json.loads(raw) if isinstance(raw, str) else raw) or {}
                waiting = str(payload.get("waiting") or "")
                clicked = str(payload.get("clicked") or "")

                if waiting:
                    # The room is in the lobby: the page pressed nothing and
                    # nothing is worth pressing. From here the pass only keeps
                    # watching for the call to start.
                    if lobby_deadline is None:
                        lobby_deadline = time.monotonic() + LOBBY_WAIT_SECONDS
                        attempt.waiting = True
                        log_event(
                            log, logging.INFO, "meeting.join_waiting_to_be_admitted",
                            provider=meeting.provider_id, page_says=waiting[:60],
                        )
                    interval = 3.0
                elif payload.get("filled_name"):
                    # The name went in and Join is still disabled; the click is
                    # the next pass's job, once the page has caught up.
                    attempt.filled_name = True
                    log_event(
                        log, logging.DEBUG, "meeting.join_name_filled",
                        provider=meeting.provider_id, pass_number=attempt.passes,
                    )
                    interval = 2.0
                elif clicked:
                    attempt.clicks.append(clicked[:60])
                    recent_clicks[clicked.strip().lower()] = (
                        str(payload.get("url") or ""),
                        time.monotonic(),
                    )
                    log_event(
                        log, logging.INFO, "meeting.join_automation_attempted",
                        provider=meeting.provider_id, button=clicked[:40],
                        pass_number=attempt.passes,
                    )
                    # A click usually triggers a page change; give it room.
                    interval = 3.0
                else:
                    if guard:
                        log_event(
                            log, logging.DEBUG, "meeting.join_repeat_guarded",
                            provider=meeting.provider_id,
                            buttons=",".join(text for text, _ in guard),
                        )
                    interval = min(6.0, interval * 1.4)
            except CDPError as exc:
                attempt.error = str(exc)
                log_event(
                    log, logging.DEBUG, "meeting.join_probe_failed", error=str(exc)[:120]
                )
                interval = min(8.0, interval * 1.6)
            except (TypeError, ValueError) as exc:
                attempt.error = f"unreadable automation result: {exc}"

            if self._join_wait(stop, generation, meeting.uid, interval):
                return

        if self._join_cancelled(stop, generation, meeting.uid):
            return

        attempt.finished_at = datetime.now(timezone.utc)
        if attempt.waiting:
            # Not a failure: the room is on the meeting's own waiting screen and
            # joins the moment the host admits it.
            log_event(
                log, logging.INFO, "meeting.join_still_waiting",
                provider=meeting.provider_id, passes=attempt.passes,
                note="waiting to be admitted; there is nothing left to press",
            )
            return

        attempt.gave_up = True
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
        """Run the join automation again for the meeting already on screen.

        This is the "try again" path: it never navigates, so a half-finished
        join is picked up where it is rather than reloaded from the top.
        """
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
        self._start_join_automation(placeholder)
        log_event(log, logging.INFO, "meeting.join_automation_retried", provider=provider)
        return True

    def leave_meeting(self, *, reason: str = "hangup") -> bool:
        """Try to hang up politely, then return to the dashboard regardless."""
        with self._lock:
            in_meeting = self._target == TARGET_MEETING
        if in_meeting:
            hangup_texts = [
                "Leave call", "Leave meeting", "Leave", "Hang up", "End call", "End meeting",
            ]
            self._press(build_click_script(hangup_texts, fill_name=False))
            time.sleep(0.6)
        return self.go_home(reason=reason)

    def bring_to_front(self) -> bool:
        """Raise the kiosk window (after a mirroring session ends)."""
        return self._cdp.bring_to_front()

    # -- media controls the remote uses ---------------------------------
    def _press(self, script: str, *, timeout: float = 6.0) -> bool:
        """Run one click pass. True if something was actually pressed."""
        try:
            raw = self._cdp.evaluate(script, timeout=timeout)
            payload = (json.loads(raw) if isinstance(raw, str) else raw) or {}
            return bool(payload.get("clicked"))
        except (CDPError, TypeError, ValueError):
            return False

    def toggle_meeting_mute(self) -> bool:
        """Press the meeting page's own mute control, where there is one."""
        texts = ["Mute microphone", "Unmute microphone", "Mute", "Unmute"]
        return self._press(build_click_script(texts, fill_name=False))

    def mute_meeting_microphone(self) -> bool:
        """Mute — never toggle — the meeting page's microphone control.

        "Mute" is a substring of "Unmute", so a plain text match cheerfully
        unmutes a room that was already quiet. build_mute_script() carries the
        deny list that makes this one-way, which is what "join muted" needs.
        """
        return self._press(build_mute_script())

    def toggle_meeting_camera(self) -> bool:
        texts = ["Turn camera on", "Turn camera off", "Start video", "Stop video", "Camera"]
        return self._press(build_click_script(texts, fill_name=False))

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
