"""The room state machine: what should the TV be showing, right now?

This is the only place that decides between the four modes. Everything else
reports facts (a calendar snapshot, a mirroring session, a browser URL) and this
service turns them into a decision and the actions that follow from it.

Precedence, highest first:

1. ``screen-sharing`` — someone is mirroring, so their screen is on the TV
2. ``meeting``        — the TV is on a meeting page
3. ``offline``        — no network; the dashboard stays up and says so
4. ``home``           — the room dashboard

Two safety rules matter more than anything clever here:

* The appliance **always** leaves a meeting screen eventually. A meeting ends at
  its scheduled end plus a grace period; failing that, at a hard maximum;
  failing that, if the meeting vanishes from the calendar entirely.
* A failure in this loop is caught and logged, and the loop keeps running.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .airplay_service import AirPlayService
from .browser_service import BrowserService
from .calendar_service import CalendarService
from .config import ConfigManager
from .logging_setup import get_logger, log_event
from .models import (
    MODE_HOME,
    MODE_MEETING,
    MODE_OFFLINE,
    MODE_SHARING,
    Meeting,
)
from .poly_service import PolyService
from .system_service import SystemService

log = get_logger("room")

#: How often the state machine re-evaluates.
TICK_SECONDS = 5.0


@dataclass
class ActiveMeeting:
    """The meeting the TV is currently on."""

    meeting_id: str
    title: str
    provider_id: str
    scheduled_end: datetime
    opened_at: datetime
    opened_manually: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.meeting_id,
            "title": self.title,
            "provider": self.provider_id,
            "scheduled_end": self.scheduled_end.isoformat(),
            "opened_at": self.opened_at.isoformat(),
            "opened_manually": self.opened_manually,
        }


@dataclass
class RoomState:
    """A snapshot of the room, served to the dashboard as ``/api/state``."""

    mode: str = MODE_HOME
    active: ActiveMeeting | None = None
    network_ok: bool = True
    last_action: str = ""
    last_action_at: datetime | None = None
    notices: list[str] = field(default_factory=list)


class MeetingService:
    """Drives the room between the dashboard, meetings and screen sharing."""

    def __init__(
        self,
        config: ConfigManager,
        calendar: CalendarService,
        browser: BrowserService,
        airplay: AirPlayService,
        poly: PolyService,
        system: SystemService,
    ) -> None:
        self.config = config
        self.calendar = calendar
        self.browser = browser
        self.airplay = airplay
        self.poly = poly
        self.system = system

        self._lock = threading.RLock()
        self._state = RoomState()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._network_ok = True
        self._opened_meeting_ids: set[str] = set()
        self._last_daily_restart: date | None = None
        self._starting_up = True

        airplay.on_change(self._on_sharing_change)

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="room-state", daemon=True)
        self._thread.start()
        log_event(log, logging.INFO, "room.state_machine_started")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # pragma: no cover - must never die
                log.exception("room.tick_failed")
            self._stop.wait(timeout=TICK_SECONDS)

    # -- external facts --------------------------------------------------
    def set_network_ok(self, ok: bool) -> None:
        """Called by the health service."""
        with self._lock:
            self._network_ok = ok

    def _on_sharing_change(self, sharing: bool) -> None:
        """React immediately when mirroring starts or stops."""
        if sharing:
            with self._lock:
                in_meeting = self._state.active is not None
            if in_meeting and not self.config.bool_("AIRPLAY_INTERRUPTS_MEETING"):
                log_event(log, logging.WARNING, "airplay.refused_during_meeting")
                self.airplay.force_stop_sharing()
                return
        else:
            # Mirroring stopped: make sure the dashboard (or meeting) is visible.
            self.browser.bring_to_front()
        self.tick()

    # -- state -----------------------------------------------------------
    @property
    def mode(self) -> str:
        with self._lock:
            return self._state.mode

    def state(self) -> RoomState:
        with self._lock:
            return self._state

    def _record_action(self, action: str) -> None:
        with self._lock:
            self._state.last_action = action
            self._state.last_action_at = datetime.now(timezone.utc)

    # -- the tick --------------------------------------------------------
    def tick(self) -> str:
        """Re-evaluate the room and take any action required. Returns the mode."""
        tz = self.config.tz()
        now = datetime.now(tz)

        with self._lock:
            active = self._state.active
            network_ok = self._network_ok

        # 1. Leave a finished meeting.
        if active is not None:
            reason = self._should_return_home(active, now)
            if reason:
                self._return_home(reason)
                active = None

        # 2. Open an upcoming meeting.
        if active is None and self.config.bool_("AUTO_OPEN_MEETING"):
            candidate = self._meeting_due(now)
            if candidate is not None:
                self.open_meeting(candidate, manual=False)
                with self._lock:
                    active = self._state.active

        # 3. Optional nightly restart while the room is empty.
        self._maybe_daily_restart(now)

        # 4. Work out the mode.
        sharing = self.airplay.sharing
        if sharing:
            mode = MODE_SHARING
        elif active is not None:
            mode = MODE_MEETING
        elif not network_ok:
            mode = MODE_OFFLINE
        else:
            mode = MODE_HOME

        with self._lock:
            previous = self._state.mode
            self._state.mode = mode
            self._state.network_ok = network_ok

        if previous != mode:
            log_event(log, logging.INFO, "room.mode_changed", was=previous, now=mode)

        self._starting_up = False
        return mode

    def _should_return_home(self, active: ActiveMeeting, now: datetime) -> str:
        """Why the TV should leave this meeting, or an empty string to stay."""
        grace = timedelta(minutes=self.config.float_("RETURN_HOME_MINUTES"))
        hard_limit = timedelta(minutes=self.config.int_("MAX_MEETING_MINUTES"))

        meeting = self.calendar.find(active.meeting_id)
        if meeting is not None:
            # Trust the live calendar: the meeting may have been extended.
            if now >= meeting.end + grace:
                return "meeting ended"
            if meeting.cancelled:
                return "meeting cancelled"
        else:
            if now >= active.scheduled_end + grace:
                return "meeting ended"
            # The calendar may simply be unreachable; only treat a *successful*
            # refresh that no longer lists the meeting as it having gone away.
            snapshot = self.calendar.snapshot
            if snapshot.ok and not snapshot.stale and now >= active.scheduled_end:
                return "meeting no longer on the calendar"

        if now >= active.opened_at + hard_limit:
            return f"safety limit of {self.config.int_('MAX_MEETING_MINUTES')} minutes reached"
        return ""

    def _meeting_due(self, now: datetime) -> Meeting | None:
        """The meeting that should be opened now, if any."""
        lead = timedelta(minutes=self.config.float_("AUTO_OPEN_MINUTES"))
        grace = timedelta(minutes=self.config.float_("RETURN_HOME_MINUTES"))
        current, upcoming = self.calendar.current_and_upcoming(now)

        # A meeting already under way takes priority (e.g. after a reboot
        # mid-meeting, the room rejoins by itself).
        for candidate in ([current] if current else []) + upcoming:
            if candidate is None or not candidate.has_link or candidate.cancelled:
                continue
            if candidate.uid in self._opened_meeting_ids:
                continue
            if now >= candidate.end + grace:
                continue
            if candidate.start - lead <= now:
                return candidate
        return None

    def _maybe_daily_restart(self, now: datetime) -> None:
        """Restart the room software at a configured quiet hour."""
        target = self.config.str_("DAILY_RESTART_TIME").strip()
        if not target or ":" not in target:
            return
        try:
            hour, minute = (int(part) for part in target.split(":", 1))
        except ValueError:
            return
        if now.hour != hour or now.minute != minute:
            return
        if self._last_daily_restart == now.date():
            return
        with self._lock:
            if self._state.active is not None:
                return  # never interrupt a meeting
        if self.airplay.sharing:
            return
        self._last_daily_restart = now.date()
        log_event(log, logging.WARNING, "room.daily_restart", at=target)
        self.browser.restart_browser(reason="scheduled nightly restart")

    # -- actions ---------------------------------------------------------
    def browser_problem(self) -> str:
        """A human explanation of why the TV cannot be driven, or ""."""
        status = self.browser.status()
        if not status.get("enabled", True):
            return "The TV display is switched off (KIOSK_ENABLED is off)."
        if not status.get("alive"):
            return (
                "The TV display is not responding. Try “Restart the TV display” "
                "on the control panel."
            )
        return ""

    def open_meeting(self, meeting: Meeting, *, manual: bool = True) -> bool:
        """Put the TV into a meeting."""
        if not meeting.has_link:
            return False

        # Clear a mirroring session so the meeting is actually visible.
        if self.airplay.sharing:
            self.airplay.force_stop_sharing()

        ok = self.browser.open_meeting(
            meeting, reason="pressed Join" if manual else "scheduled start"
        )
        if not ok:
            return False

        tz = self.config.tz()
        active = ActiveMeeting(
            meeting_id=meeting.uid,
            title=meeting.title,
            provider_id=meeting.provider_id,
            scheduled_end=meeting.end,
            opened_at=datetime.now(tz),
            opened_manually=manual,
        )
        with self._lock:
            self._state.active = active
            self._state.mode = MODE_MEETING
        self._opened_meeting_ids.add(meeting.uid)
        # Keep the "already opened" set from growing without bound.
        if len(self._opened_meeting_ids) > 200:
            self._opened_meeting_ids = set(list(self._opened_meeting_ids)[-100:])
        self._record_action(f"opened {meeting.provider_name or 'meeting'}")

        log_event(
            log, logging.INFO, "meeting.upcoming_detected" if not manual else "meeting.join_requested",
            provider=meeting.provider_id or "unknown",
            manual=manual,
            starts=meeting.start.isoformat(timespec="minutes"),
        )
        return True

    def join_next(self) -> tuple[bool, str]:
        """Join the meeting a person would expect: the current one, else the next."""
        tz = self.config.tz()
        now = datetime.now(tz)
        current, upcoming = self.calendar.current_and_upcoming(now)

        for candidate in ([current] if current else []) + upcoming:
            if candidate is not None and candidate.has_link and not candidate.cancelled:
                if self.open_meeting(candidate, manual=True):
                    return True, candidate.title
                return False, self.browser_problem() or "The meeting could not be opened."
        if current is not None or upcoming:
            return False, "The next meeting has no online meeting link."
        return False, "There is no meeting to join."

    def join_meeting_id(self, meeting_id: str) -> tuple[bool, str]:
        meeting = self.calendar.find(meeting_id)
        if meeting is None:
            return False, "That meeting is no longer on the calendar."
        if not meeting.has_link:
            return False, "That meeting has no online meeting link."
        if self.open_meeting(meeting, manual=True):
            return True, meeting.title
        return False, self.browser_problem() or "The meeting could not be opened."

    def leave_meeting(self, *, reason: str = "requested") -> bool:
        with self._lock:
            was_active = self._state.active is not None
        self.browser.leave_meeting(reason=reason)
        self._clear_active(reason)
        self._record_action("left the meeting")
        return was_active

    def go_home(self, *, reason: str = "requested") -> bool:
        self.browser.go_home(reason=reason)
        self._clear_active(reason)
        self._record_action("returned to the dashboard")
        return True

    def _return_home(self, reason: str) -> None:
        log_event(log, logging.INFO, "room.returning_to_dashboard", reason=reason)
        self.browser.go_home(reason=reason)
        self._clear_active(reason)

    def _clear_active(self, reason: str) -> None:
        with self._lock:
            self._state.active = None
            if self._state.mode == MODE_MEETING:
                self._state.mode = MODE_HOME

    def retry_join_automation(self) -> bool:
        return self.browser.retry_join()

    # -- remote button actions -------------------------------------------
    def dispatch_action(self, action: str) -> dict[str, object]:
        """Handle an action from the Poly remote or the control panel."""
        action = (action or "").strip().lower()

        if action == "join":
            ok, detail = self.join_next()
            return {"ok": ok, "detail": detail}
        if action in ("hangup", "leave"):
            return {"ok": self.leave_meeting(reason="remote hang-up"), "detail": "left"}
        if action == "home":
            self.go_home(reason="remote home button")
            return {"ok": True, "detail": "dashboard"}
        if action == "mute":
            # Mute the microphone at the OS level, and in the meeting page too so
            # the on-screen indicator agrees with reality.
            muted = self.poly.set_mute()
            with self._lock:
                in_meeting = self._state.active is not None
            if in_meeting:
                self.browser.toggle_meeting_mute()
            return {"ok": muted is not None, "muted": muted}
        if action == "volume_up":
            level = self.poly.adjust_volume(self.config.int_("POLY_VOLUME_STEP"))
            return {"ok": level is not None, "volume": level}
        if action == "volume_down":
            level = self.poly.adjust_volume(-self.config.int_("POLY_VOLUME_STEP"))
            return {"ok": level is not None, "volume": level}
        if action == "camera":
            return {"ok": self.browser.toggle_meeting_camera()}

        return {"ok": False, "detail": f"Unknown action: {action}"}

    # -- data for the UI -------------------------------------------------
    def dashboard_payload(self) -> dict[str, object]:
        """Everything the dashboard needs, in one response."""
        tz = self.config.tz()
        now = datetime.now(tz)
        show_titles = self.config.bool_("CALENDAR_SHOW_TITLES")
        current, upcoming = self.calendar.current_and_upcoming(now)
        limit = self.config.int_("CALENDAR_UPCOMING_COUNT")
        snapshot = self.calendar.snapshot

        with self._lock:
            state = self._state
            mode = state.mode
            active = state.active
            network_ok = state.network_ok

        # "Next" is the meeting in progress if there is one, else the next up.
        next_meeting = current or (upcoming[0] if upcoming else None)
        available = current is None

        return {
            "mode": mode,
            "room": {
                "name": self.config.str_("ROOM_NAME"),
                "subtitle": self.config.str_("ROOM_SUBTITLE"),
                "available": available,
                "busy_until": current.end.isoformat() if current else None,
            },
            "now": now.isoformat(),
            "time_format_24h": self.config.bool_("TIME_FORMAT_24H"),
            "current": current.to_dict(show_titles=show_titles) if current else None,
            "next": next_meeting.to_dict(show_titles=show_titles) if next_meeting else None,
            "upcoming": [
                meeting.to_dict(show_titles=show_titles)
                for meeting in upcoming[: max(1, limit)]
            ],
            "active_meeting": active.to_dict() if active else None,
            "calendar": {
                "ok": snapshot.ok,
                "stale": snapshot.stale,
                "error": snapshot.error if not snapshot.ok else "",
                "configured": self.calendar.provider.is_configured(),
                "source": snapshot.source,
                "age_seconds": round(snapshot.age_seconds) if snapshot.age_seconds is not None else None,
            },
            "airplay": self.airplay.status(),
            "network_ok": network_ok,
            "setup_required": self.config.setup_required(),
            "join_available": bool(
                (current and current.has_link) or (upcoming and upcoming[0].has_link)
            ),
        }
