"""The calendar provider contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..config import ConfigManager
from ..models import Meeting


class CalendarFetchError(RuntimeError):
    """Raised when a provider cannot supply meetings right now.

    The calendar service catches this, keeps showing the last good data and
    retries — a calendar outage must never take the room screen down.
    """


class CalendarProvider(ABC):
    """Source of the room's meetings.

    Implementations must be safe to call from a background thread and must
    raise :class:`CalendarFetchError` (not arbitrary exceptions) for expected
    failures such as a network timeout or an HTTP error.
    """

    #: Value of ``CALENDAR_SOURCE`` that selects this provider.
    source_id: str = "base"
    #: Shown on the settings/diagnostics pages.
    display_name: str = "Calendar"

    def __init__(self, config: ConfigManager) -> None:
        self.config = config

    @abstractmethod
    def fetch(self, window_start: datetime, window_end: datetime) -> list[Meeting]:
        """Return every meeting overlapping the window, ordered by start time."""

    def describe(self) -> str:
        """One line about where meetings come from, for the diagnostics page."""
        return self.display_name

    def is_configured(self) -> bool:
        """False when the administrator still has to enter something."""
        return True
