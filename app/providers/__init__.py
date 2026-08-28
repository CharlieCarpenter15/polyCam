"""Calendar back ends.

Each provider implements :class:`~app.providers.base.CalendarProvider`, so the
rest of the appliance never knows where meetings come from. Swapping the ICS
feed for Microsoft Graph or the Google Calendar API later means adding one file
here and one entry in :data:`PROVIDER_FACTORIES` — no other code changes.
"""

from __future__ import annotations

from typing import Callable

from ..config import ConfigManager
from .base import CalendarProvider
from .ics import IcsCalendarProvider
from .mock import MockCalendarProvider
from .null import NullCalendarProvider

#: ``CALENDAR_SOURCE`` value -> factory.
PROVIDER_FACTORIES: dict[str, Callable[[ConfigManager], CalendarProvider]] = {
    "ics": IcsCalendarProvider,
    "mock": MockCalendarProvider,
    "none": NullCalendarProvider,
    # Future:
    #   "graph":  MicrosoftGraphCalendarProvider,
    #   "google": GoogleCalendarProvider,
}


def build_provider(config: ConfigManager) -> CalendarProvider:
    """Create the provider named by ``CALENDAR_SOURCE`` (falling back to mock)."""
    source = config.str_("CALENDAR_SOURCE") or "ics"
    factory = PROVIDER_FACTORIES.get(source, IcsCalendarProvider)
    return factory(config)


__all__ = [
    "CalendarProvider",
    "IcsCalendarProvider",
    "MockCalendarProvider",
    "NullCalendarProvider",
    "PROVIDER_FACTORIES",
    "build_provider",
]
