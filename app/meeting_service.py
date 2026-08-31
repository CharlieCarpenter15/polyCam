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

Joining is deliberately idempotent. Every JOIN button in the building ends up in
:meth:`MeetingService.open_meeting` — the TV, a phone, the Poly remote, the
scheduled auto-open — and asking for the meeting that is already on screen
brings the page forward and has another go at its Join buttons, rather than
reloading it mid-join.

Whether the scheduled auto-open happens at all is ``MEETING_JOIN_MODE``: an
"automatic" room puts the meeting on the TV by itself, a "manual" one waits for
somebody to press JOIN. Every other way into a meeting works the same in both.
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

#: How long the last room-button press is worth showing. The TV and the phone
#: controller render it as a brief confirmation ("Microphone muted"), so an old
#: entry is not history, it is a lie about what just happened.
REMOTE_ACTION_TTL_SECONDS = 15.0


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
class RemoteAction:
    """The most recent room-button press, for the TV and the phone controller."""

    action: str
    detail: str
    ok: bool
    at: datetime
    source: str = "remote"

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "detail": self.detail,
            "ok": self.ok,
            "at": self.at.isoformat(),
            "source": self.source,
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
        # Held for the whole of open_meeting so two callers — the TV, a phone,
        # the scheduled auto-open — cannot both navigate the browser.
        self._open_lock = threading.Lock()
        self._state = RoomState()
        self._last_remote: RemoteAction | None = None
        # The meeting page never says which way its camera control went, so the
        # room keeps its own idea of it, purely to word the confirmation.
        self._camera_on = True
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

    def join_mode(self) -> str:
        """Either "automatic" — the room opens meetings itself — or "manual"."""
        mode = self.config.str_("MEETING_JOIN_MODE").strip().lower()
        return "manual" if mode == "manual" else "automatic"

    def joins_automatically(self) -> bool:
        """True when the room puts a meeting on the TV without being asked."""
        return self.join_mode() == "automatic"

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

        # 2. Open an upcoming meeting, unless this room joins by hand.
        if active is None and self.joins_automatically():
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

    def _reopen_window(self) -> timedelta:
        """How long a meeting counts as "already open".

        The auto-join timeout is the honest answer: while the automation is
        still working through the pre-join screens, navigating again throws all
        of that away and starts the sign-in over — which is what people in the
        room describe as "it logs in several times". Once the automation has
        given up, a fresh navigation is a reasonable way to try again. Floored
        at 30 seconds so a short configured timeout still covers someone
        pressing JOIN on the TV and then on their phone.
        """
        return timedelta(
            seconds=max(30.0, float(self.config.int_("AUTO_JOIN_TIMEOUT_SECONDS")))
        )

    def _already_open(self, meeting: Meeting) -> bool:
        with self._lock:
            active = self._state.active
        if active is None or active.meeting_id != meeting.uid:
            return False
        return datetime.now(self.config.tz()) - active.opened_at < self._reopen_window()

    def open_meeting(self, meeting: Meeting, *, manual: bool = True) -> bool:
        """Put the TV into a meeting.

        Idempotent on purpose. JOIN on the TV, JOIN on a phone, the Poly remote
        and the scheduled auto-open all arrive here, often within seconds of one
        another, and re-navigating would reload the meeting page mid-join. So a
        request for the meeting already on screen never navigates: it brings the
        page to the front, and — when a person asked for it — runs the join
        buttons over the page again, which is the only thing that can help
        somebody standing in the room pressing JOIN at a page that has stopped
        short of the call.
        """
        if not meeting.has_link:
            return False

        # Clear a mirroring session so the meeting is actually visible. This
        # happens before the lock on purpose: stopping the mirror fires the
        # AirPlay callback, which ticks the room, which can come straight back
        # in here — and the lock below is not reentrant.
        if self.airplay.sharing:
            self.airplay.force_stop_sharing()

        with self._open_lock:
            if self._already_open(meeting):
                self.browser.bring_to_front()
                # A person pressing JOIN for the meeting already on screen is
                # not asking for the page again — they are saying the room is
                # not in the call yet. Reloading would throw the half-finished
                # sign-in away, so run the join buttons over the page as it
                # stands. Nothing to retry with the automation switched off:
                # somebody is joining that page by hand, and clicking at it
                # from here is the last thing they need.
                retried = False
                if manual and self.config.bool_("AUTO_CLICK_JOIN"):
                    retried = self.browser.retry_join()
                log_event(
                    log, logging.DEBUG, "meeting.join_already_open",
                    provider=meeting.provider_id or "unknown", manual=manual,
                    retried=retried,
                )
                return True

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
            self._camera_on = True
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
    def dispatch_action(self, action: str, *, source: str = "remote") -> dict[str, object]:
        """Handle an action from the Poly remote, the TV or a phone.

        Every branch returns a ``detail`` sentence. The same words go back to
        whoever pressed the button and into ``dashboard_payload()["remote"]``,
        so the phone that pressed mute and the TV across the room tell the same
        story. ``source`` says which of them it was.
        """
        action = (action or "").strip().lower()
        result = self._perform_action(action)
        self._record_remote_action(action, result, source=source)
        return result

    def _perform_action(self, action: str) -> dict[str, object]:
        if action == "join":
            ok, detail = self.join_next()
            return {"ok": ok, "detail": f"Joining {detail}" if ok else detail}
        if action in ("hangup", "leave"):
            ok = self.leave_meeting(reason="remote hang-up")
            return {
                "ok": ok,
                "detail": "Left the meeting" if ok else "There was no meeting to leave",
            }
        if action == "home":
            self.go_home(reason="remote home button")
            return {"ok": True, "detail": "Showing the room dashboard"}
        if action == "mute":
            # Mute the microphone at the OS level, and in the meeting page too so
            # the on-screen indicator agrees with reality.
            muted = self.poly.set_mute()
            with self._lock:
                in_meeting = self._state.active is not None
            if in_meeting:
                self.browser.toggle_meeting_mute()
            if muted is None:
                return {"ok": False, "muted": None, "detail": "No microphone is available"}
            return {
                "ok": True,
                "muted": muted,
                "detail": "Microphone muted" if muted else "Microphone on",
            }
        if action in ("volume_up", "volume_down"):
            step = self.config.int_("POLY_VOLUME_STEP")
            level = self.poly.adjust_volume(step if action == "volume_up" else -step)
            return {
                "ok": level is not None,
                "volume": level,
                "detail": f"Volume {level}%" if level is not None else "No speaker is available",
            }
        if action == "camera":
            if not self.browser.toggle_meeting_camera():
                return {"ok": False, "detail": "No camera control on this page"}
            # The page never reports which way its control went, so the room
            # keeps its own idea of the camera and words the confirmation from
            # that. It starts each meeting on, which is how they all start.
            self._camera_on = not self._camera_on
            return {
                "ok": True,
                "detail": "Camera turned on" if self._camera_on else "Camera turned off",
            }

        return {"ok": False, "detail": f"Unknown action: {action}"}

    def _record_remote_action(
        self, action: str, result: dict[str, object], *, source: str
    ) -> None:
        if not action:
            return
        detail = str(result.get("detail") or "")
        with self._lock:
            self._last_remote = RemoteAction(
                action=action,
                detail=detail,
                ok=bool(result.get("ok")),
                at=datetime.now(self.config.tz()),
                source=source or "remote",
            )
        self._record_action(detail or action)

    def recent_remote_action(self, now: datetime | None = None) -> dict[str, object] | None:
        """The last button press, or None once it is too old to show."""
        with self._lock:
            recent = self._last_remote
        if recent is None:
            return None
        now = now or datetime.now(self.config.tz())
        age = (now - recent.at).total_seconds()
        if age < 0 or age > REMOTE_ACTION_TTL_SECONDS:
            return None
        return recent.to_dict()

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
            "remote": self.recent_remote_action(now),
            "join": {
                "mode": self.join_mode(),
                "automation": self.browser.join_state(),
            },
            "airplay": self.airplay.status(),
            "network_ok": network_ok,
            "setup_required": self.config.setup_required(),
            "join_available": bool(
                (current and current.has_link) or (upcoming and upcoming[0].has_link)
            ),
        }
