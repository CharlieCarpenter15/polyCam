"""Plain data structures shared across the appliance."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .meeting_links import provider_label

#: Operating modes reported by ``/api/health`` and ``/api/state``.
MODE_HOME = "home"
MODE_MEETING = "meeting"
MODE_SHARING = "screen-sharing"
MODE_OFFLINE = "offline"
MODES = (MODE_HOME, MODE_MEETING, MODE_SHARING, MODE_OFFLINE)

#: Component status values.
OK = "ok"
WARN = "warning"
FAIL = "error"
OFF = "disabled"
UNKNOWN = "unknown"


@dataclass
class Meeting:
    """One calendar event, already resolved to a join link where possible."""

    uid: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    organizer: str = ""
    provider_id: str = ""
    join_url: str = ""
    cancelled: bool = False
    private: bool = False

    # -- derived ---------------------------------------------------------
    @property
    def provider_name(self) -> str:
        return provider_label(self.provider_id) if self.provider_id else ""

    @property
    def has_link(self) -> bool:
        return bool(self.join_url)

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def is_current(self, now: datetime) -> bool:
        return self.start <= now < self.end

    def minutes_until(self, now: datetime) -> float:
        return (self.start - now).total_seconds() / 60.0

    def minutes_since_end(self, now: datetime) -> float:
        return (now - self.end).total_seconds() / 60.0

    def display_title(self, show_titles: bool = True) -> str:
        if not show_titles or self.private:
            return "Busy"
        return self.title or "Meeting"

    def to_dict(self, *, show_titles: bool = True, include_url: bool = False) -> dict[str, Any]:
        """JSON form for the dashboard.

        The join URL is withheld by default: the dashboard asks the backend to
        open a meeting by id, so a meeting link never needs to travel to the
        browser or appear in a page source.
        """
        payload: dict[str, Any] = {
            "id": self.uid,
            "title": self.display_title(show_titles),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "all_day": self.all_day,
            "location": self.location if show_titles else "",
            "organizer": self.organizer if show_titles else "",
            "provider": self.provider_id,
            "provider_name": self.provider_name,
            "has_link": self.has_link,
            "cancelled": self.cancelled,
        }
        if include_url:
            payload["join_url"] = self.join_url
        return payload


@dataclass
class CalendarSnapshot:
    """The result of a calendar refresh, successful or not."""

    meetings: list[Meeting] = field(default_factory=list)
    fetched_at: datetime | None = None
    ok: bool = False
    error: str = ""
    source: str = ""
    #: True when ``meetings`` came from the on-disk cache rather than the network.
    stale: bool = False

    @property
    def age_seconds(self) -> float | None:
        if self.fetched_at is None:
            return None
        now = datetime.now(self.fetched_at.tzinfo)
        return max(0.0, (now - self.fetched_at).total_seconds())
