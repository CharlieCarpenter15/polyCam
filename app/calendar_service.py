"""Background calendar refresh with a last-known-good cache.

Design rules, in priority order:

1. A calendar or network failure must never take the room screen down.
2. The screen keeps showing the last meetings it successfully retrieved, marked
   stale, until fresh data arrives.
3. Successful data is written to disk, so a reboot during an internet outage
   still shows this morning's meetings rather than an empty room.
4. Failures back off (so a broken URL is not hammered every 30 seconds) but
   always recover on their own.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Callable

from . import paths
from .config import ConfigManager
from .logging_setup import get_logger, log_event
from .models import CalendarSnapshot, Meeting
from .providers import build_provider
from .providers.base import CalendarFetchError, CalendarProvider
from .store import read_json, write_json

log = get_logger("calendar")

#: Failures multiply the wait by this factor, up to MAX_BACKOFF_SECONDS.
BACKOFF_FACTOR = 2.0
MAX_BACKOFF_SECONDS = 600.0
#: Cached meetings older than this are not trusted for display.
CACHE_MAX_AGE_HOURS = 18


class CalendarService:
    """Owns the calendar refresh thread and the current snapshot."""

    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._provider: CalendarProvider = build_provider(config)
        self._snapshot = CalendarSnapshot(source=self._provider.source_id)
        self._consecutive_failures = 0
        self._last_attempt: datetime | None = None
        self._listeners: list[Callable[[CalendarSnapshot], None]] = []
        self._load_cache()
        config.on_change(self._on_config_change)

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="calendar-refresh", daemon=True
        )
        self._thread.start()
        log_event(log, logging.INFO, "calendar.service_started",
                  source=self._provider.source_id)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    def refresh_now(self) -> None:
        """Ask the refresh thread to fetch immediately (used by the UI)."""
        self._wake.set()

    def on_update(self, callback: Callable[[CalendarSnapshot], None]) -> None:
        with self._lock:
            self._listeners.append(callback)

    # -- state -----------------------------------------------------------
    @property
    def snapshot(self) -> CalendarSnapshot:
        with self._lock:
            return self._snapshot

    @property
    def provider(self) -> CalendarProvider:
        with self._lock:
            return self._provider

    def meetings(self) -> list[Meeting]:
        return list(self.snapshot.meetings)

    def status(self) -> dict[str, object]:
        """Calendar health for ``/api/health``."""
        snap = self.snapshot
        with self._lock:
            failures = self._consecutive_failures
            provider = self._provider
        return {
            "source": provider.source_id,
            "description": provider.describe(),
            "configured": provider.is_configured(),
            "ok": snap.ok,
            "stale": snap.stale,
            "error": snap.error,
            "meeting_count": len(snap.meetings),
            "last_success": snap.fetched_at.isoformat() if snap.fetched_at else None,
            "age_seconds": round(snap.age_seconds) if snap.age_seconds is not None else None,
            "consecutive_failures": failures,
        }

    def current_and_upcoming(
        self, now: datetime | None = None
    ) -> tuple[Meeting | None, list[Meeting]]:
        """``(meeting happening now or None, meetings still to come)``."""
        tz = self.config.tz()
        now = now or datetime.now(tz)
        current: Meeting | None = None
        upcoming: list[Meeting] = []
        for meeting in self.snapshot.meetings:
            if meeting.cancelled or meeting.all_day:
                continue
            if meeting.is_current(now):
                # The one that started most recently wins on overlap.
                if current is None or meeting.start > current.start:
                    current = meeting
            elif meeting.start > now:
                upcoming.append(meeting)
        upcoming.sort(key=lambda m: m.start)
        return current, upcoming

    def find(self, meeting_id: str) -> Meeting | None:
        for meeting in self.snapshot.meetings:
            if meeting.uid == meeting_id:
                return meeting
        return None

    # -- refresh loop ----------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            interval = float(self.config.int_("CALENDAR_REFRESH_SECONDS"))
            try:
                self._refresh_once()
            except Exception:  # pragma: no cover - the loop must never die
                log.exception("calendar.refresh_crashed")
                with self._lock:
                    self._consecutive_failures += 1

            with self._lock:
                failures = self._consecutive_failures
            if failures:
                interval = min(
                    MAX_BACKOFF_SECONDS,
                    interval * (BACKOFF_FACTOR ** min(failures, 8)),
                )

            self._wake.wait(timeout=interval)
            self._wake.clear()

    def _refresh_once(self) -> None:
        with self._lock:
            provider = self._provider
        tz = self.config.tz()
        now = datetime.now(tz)
        self._last_attempt = now

        if not provider.is_configured():
            with self._lock:
                self._snapshot = CalendarSnapshot(
                    meetings=[],
                    fetched_at=None,
                    ok=False,
                    error="Calendar has not been set up yet.",
                    source=provider.source_id,
                )
            return

        window_start = now - timedelta(hours=2)
        window_end = now + timedelta(hours=self.config.int_("CALENDAR_LOOKAHEAD_HOURS"))

        try:
            meetings = provider.fetch(window_start, window_end)
        except CalendarFetchError as exc:
            self._record_failure(str(exc))
            return
        except Exception as exc:
            # A provider bug should look like a calendar failure, not a crash.
            log.exception("calendar.provider_error")
            self._record_failure(f"Unexpected calendar error ({exc.__class__.__name__}).")
            return

        with self._lock:
            previous = self._snapshot
            recovered = bool(previous.error) or previous.stale
            self._snapshot = CalendarSnapshot(
                meetings=meetings,
                fetched_at=now,
                ok=True,
                error="",
                source=provider.source_id,
                stale=False,
            )
            self._consecutive_failures = 0
            snapshot = self._snapshot

        if recovered:
            log_event(log, logging.INFO, "calendar.recovered", events=len(meetings))
        log_event(
            log,
            logging.DEBUG if not recovered else logging.INFO,
            "calendar.refreshed",
            events=len(meetings),
            source=provider.source_id,
        )
        self._save_cache(snapshot)
        self._notify(snapshot)

    def _record_failure(self, message: str) -> None:
        with self._lock:
            self._consecutive_failures += 1
            failures = self._consecutive_failures
            previous = self._snapshot
            # Keep the meetings we already have; just mark them stale.
            self._snapshot = CalendarSnapshot(
                meetings=previous.meetings,
                fetched_at=previous.fetched_at,
                ok=False,
                error=message,
                source=previous.source,
                stale=bool(previous.meetings),
            )
            snapshot = self._snapshot

        # Log the first failure loudly, then only every tenth, so a long outage
        # does not fill the journal.
        level = logging.WARNING if failures == 1 or failures % 10 == 0 else logging.DEBUG
        log_event(
            log,
            level,
            "calendar.refresh_failed",
            error=message,
            consecutive_failures=failures,
            showing_cached=len(snapshot.meetings),
        )
        self._notify(snapshot)

    # -- cache -----------------------------------------------------------
    def _save_cache(self, snapshot: CalendarSnapshot) -> None:
        payload = {
            "version": 1,
            "source": snapshot.source,
            "fetched_at": snapshot.fetched_at.isoformat() if snapshot.fetched_at else None,
            "meetings": [
                {
                    "uid": m.uid,
                    "title": m.title,
                    "start": m.start.isoformat(),
                    "end": m.end.isoformat(),
                    "all_day": m.all_day,
                    "location": m.location,
                    "organizer": m.organizer,
                    "provider_id": m.provider_id,
                    "join_url": m.join_url,
                    "cancelled": m.cancelled,
                    "private": m.private,
                    "attendees": list(m.attendees),
                }
                for m in snapshot.meetings
            ],
        }
        # The cache holds meeting URLs, so keep it owner-readable only.
        write_json(paths.CALENDAR_CACHE, payload, mode=0o600)

    def _load_cache(self) -> None:
        payload = read_json(paths.CALENDAR_CACHE, default=None)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return
        # Never show meetings cached from a different calendar source: switching
        # from the mock calendar to a real one must not leave invented meetings
        # on the screen.
        cached_source = str(payload.get("source") or "")
        if cached_source and cached_source != self._provider.source_id:
            log_event(
                log, logging.INFO, "calendar.cache_ignored_other_source",
                cached=cached_source, current=self._provider.source_id,
            )
            return
        try:
            fetched_raw = payload.get("fetched_at")
            fetched_at = datetime.fromisoformat(fetched_raw) if fetched_raw else None
        except (TypeError, ValueError):
            fetched_at = None

        if fetched_at is not None:
            age = datetime.now(fetched_at.tzinfo) - fetched_at
            if age > timedelta(hours=CACHE_MAX_AGE_HOURS):
                log_event(
                    log, logging.INFO, "calendar.cache_too_old",
                    age_hours=round(age.total_seconds() / 3600, 1),
                )
                return

        meetings: list[Meeting] = []
        for row in payload.get("meetings") or []:
            try:
                meetings.append(
                    Meeting(
                        uid=str(row["uid"]),
                        title=str(row.get("title") or "Meeting"),
                        start=datetime.fromisoformat(row["start"]),
                        end=datetime.fromisoformat(row["end"]),
                        all_day=bool(row.get("all_day")),
                        location=str(row.get("location") or ""),
                        organizer=str(row.get("organizer") or ""),
                        provider_id=str(row.get("provider_id") or ""),
                        join_url=str(row.get("join_url") or ""),
                        cancelled=bool(row.get("cancelled")),
                        private=bool(row.get("private")),
                        attendees=[
                            str(a) for a in (row.get("attendees") or []) if str(a).strip()
                        ],
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue

        if not meetings:
            return
        with self._lock:
            self._snapshot = CalendarSnapshot(
                meetings=meetings,
                fetched_at=fetched_at,
                ok=False,
                error="Showing saved meetings until the calendar can be reached.",
                source=str(payload.get("source") or ""),
                stale=True,
            )
        log_event(log, logging.INFO, "calendar.cache_loaded", events=len(meetings))

    # -- reacting to settings changes ------------------------------------
    def _on_config_change(self, values: dict[str, object], changed: set[str]) -> None:
        calendar_keys = {
            "CALENDAR_SOURCE",
            "CALENDAR_ICS_URL",
            "CALENDAR_LOOKAHEAD_HOURS",
            "CALENDAR_IGNORE_ALL_DAY",
            "CALENDAR_IGNORE_DECLINED",
            "TIMEZONE",
        }
        if not changed & calendar_keys:
            return
        if "CALENDAR_SOURCE" in changed:
            with self._lock:
                self._provider = build_provider(self.config)
                self._snapshot = CalendarSnapshot(source=self._provider.source_id)
            log_event(
                log, logging.INFO, "calendar.source_changed",
                source=self.config.str_("CALENDAR_SOURCE"),
            )
        with self._lock:
            self._consecutive_failures = 0
        self.refresh_now()

    def _notify(self, snapshot: CalendarSnapshot) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(snapshot)
            except Exception:  # pragma: no cover
                log.exception("calendar.listener_failed")
