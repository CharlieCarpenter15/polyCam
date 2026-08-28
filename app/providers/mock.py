"""Invented meetings for development and for trying the screen out.

Selected with ``CALENDAR_SOURCE: mock``. Generates a believable day so the
dashboard, the auto-open logic and the join buttons can all be exercised on a
laptop with no calendar, no Poly bar and no TV.

The schedule is relative to *now*, so there is always a meeting a minute away
and a couple of upcoming ones behind it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..models import Meeting
from .base import CalendarProvider

#: (minutes from now until start, duration, title, provider, join url)
_SCRIPT: tuple[tuple[float, int, str, str, str], ...] = (
    (2, 30, "Engineering Daily", "teams",
     "https://teams.microsoft.com/l/meetup-join/19%3ameeting_MOCK1%40thread.v2/0"),
    (95, 45, "Supplier Call", "meet", "https://meet.google.com/mok-abcd-efg"),
    (200, 60, "Product Review", "teams",
     "https://teams.microsoft.com/l/meetup-join/19%3ameeting_MOCK2%40thread.v2/0"),
    (330, 30, "Quarterly Numbers", "zoom", "https://us02web.zoom.us/j/1234567890"),
    (420, 30, "Room Cleaning", "", ""),
)


class MockCalendarProvider(CalendarProvider):
    source_id = "mock"
    display_name = "Mock calendar (development)"

    def describe(self) -> str:
        return "Mock calendar — invented meetings for testing. Not a real room calendar."

    def fetch(self, window_start: datetime, window_end: datetime) -> list[Meeting]:
        tz = self.config.tz()
        now = datetime.now(tz).replace(second=0, microsecond=0)
        meetings: list[Meeting] = []
        for index, (offset, minutes, title, provider, url) in enumerate(_SCRIPT):
            start = now + timedelta(minutes=offset)
            end = start + timedelta(minutes=minutes)
            if end <= window_start or start >= window_end:
                continue
            meetings.append(
                Meeting(
                    uid=f"mock-{index}@room-appliance",
                    title=title,
                    start=start,
                    end=end,
                    location="Meeting Room",
                    organizer="mock@example.com",
                    provider_id=provider,
                    join_url=url,
                )
            )
        return meetings
