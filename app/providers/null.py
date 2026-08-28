"""A calendar that is switched off."""

from __future__ import annotations

from datetime import datetime

from ..models import Meeting
from .base import CalendarProvider


class NullCalendarProvider(CalendarProvider):
    source_id = "none"
    display_name = "Calendar disabled"

    def fetch(self, window_start: datetime, window_end: datetime) -> list[Meeting]:
        return []

    def describe(self) -> str:
        return "Calendar disabled — the dashboard shows the room as available."
