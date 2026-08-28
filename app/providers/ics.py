"""ICS / iCal feed provider.

Works with any calendar that can publish an iCal URL: Microsoft 365 / Outlook
("Publish a calendar"), Google Calendar ("Secret address in iCal format"),
Exchange, Nextcloud, Fastmail and so on. A local file path is also accepted,
which makes testing easy.

Recurring events are expanded with ``recurring-ical-events``, which handles
RRULE, EXDATE and modified occurrences (RECURRENCE-ID) correctly.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from ..logging_setup import get_logger, redact_url
from ..meeting_links import extract_meeting
from ..models import Meeting
from .base import CalendarFetchError, CalendarProvider

log = get_logger("calendar.ics")

#: Guard against a mis-typed URL pointing at something enormous.
MAX_FEED_BYTES = 12 * 1024 * 1024


class IcsCalendarProvider(CalendarProvider):
    source_id = "ics"
    display_name = "ICS / iCal feed"

    def is_configured(self) -> bool:
        return bool(self.config.str_("CALENDAR_ICS_URL"))

    def describe(self) -> str:
        url = self.config.str_("CALENDAR_ICS_URL")
        if not url:
            return "No calendar URL has been entered yet."
        if url.startswith("/") or url.lower().startswith("file://"):
            return f"ICS file at {Path(url.removeprefix('file://')).name}"
        return f"ICS feed at {redact_url(url)}"

    # -- fetching --------------------------------------------------------
    def fetch(self, window_start: datetime, window_end: datetime) -> list[Meeting]:
        raw = self._load_bytes()
        return self._parse(raw, window_start, window_end)

    def _load_bytes(self) -> bytes:
        url = self.config.str_("CALENDAR_ICS_URL")
        if not url:
            raise CalendarFetchError("No calendar URL configured.")

        # webcal:// is just https:// with a different label.
        if url.lower().startswith("webcal://"):
            url = "https://" + url[len("webcal://") :]

        if url.startswith("/") or url.lower().startswith("file://"):
            path = Path(url[len("file://") :] if url.lower().startswith("file://") else url)
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise CalendarFetchError(f"Cannot read {path.name}: {exc.strerror or exc}") from exc
            if len(data) > MAX_FEED_BYTES:
                raise CalendarFetchError("Calendar file is unreasonably large.")
            return self._require_calendar(data)

        timeout = self.config.int_("CALENDAR_TIMEOUT_SECONDS")
        try:
            response = requests.get(
                url,
                timeout=(min(10, timeout), timeout),
                headers={
                    "User-Agent": "room-appliance/1.0 (+meeting-room dashboard)",
                    "Accept": "text/calendar, text/plain, */*",
                },
                stream=True,
                allow_redirects=True,
            )
        except requests.exceptions.SSLError as exc:
            raise CalendarFetchError("TLS error contacting the calendar server.") from exc
        except requests.exceptions.ConnectTimeout as exc:
            raise CalendarFetchError("Timed out connecting to the calendar server.") from exc
        except requests.exceptions.ReadTimeout as exc:
            raise CalendarFetchError("Calendar server did not answer in time.") from exc
        except requests.exceptions.ConnectionError as exc:
            raise CalendarFetchError("Cannot reach the calendar server.") from exc
        except requests.RequestException as exc:
            raise CalendarFetchError(f"Calendar request failed: {exc.__class__.__name__}") from exc

        with response:
            if response.status_code in (401, 403):
                raise CalendarFetchError(
                    f"Calendar refused the request (HTTP {response.status_code}). "
                    "Check that the ICS link is still valid."
                )
            if response.status_code == 404:
                raise CalendarFetchError(
                    "Calendar not found (HTTP 404). The published link may have been reset."
                )
            if response.status_code >= 400:
                raise CalendarFetchError(f"Calendar returned HTTP {response.status_code}.")

            chunks: list[bytes] = []
            total = 0
            try:
                for chunk in response.iter_content(64 * 1024):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > MAX_FEED_BYTES:
                        raise CalendarFetchError(
                            "Calendar feed is larger than 12 MB — is the URL correct?"
                        )
            except requests.RequestException as exc:
                raise CalendarFetchError("Calendar download was interrupted.") from exc

        data = b"".join(chunks)
        return self._require_calendar(data)

    @staticmethod
    def _require_calendar(data: bytes) -> bytes:
        """Reject anything that is clearly not an iCalendar document.

        A sign-in page returned instead of the feed is the most common setup
        mistake, and this produces a message that actually explains it.
        """
        if not data.strip():
            raise CalendarFetchError("Calendar feed was empty.")
        head = data[:4096].upper()
        if b"BEGIN:VCALENDAR" not in head:
            hint = ""
            if b"<HTML" in head or b"<!DOCTYPE" in head:
                hint = (
                    " A web page was returned instead — the link may need to be the "
                    "ICS/iCal address rather than the calendar's web address, or the "
                    "feed may require sign-in."
                )
            raise CalendarFetchError(
                "That address did not return a calendar." + hint
            )
        return data

    # -- parsing ---------------------------------------------------------
    def _parse(
        self, raw: bytes, window_start: datetime, window_end: datetime
    ) -> list[Meeting]:
        import icalendar

        try:
            calendar = icalendar.Calendar.from_ical(raw)
        except Exception as exc:  # icalendar raises a variety of types
            raise CalendarFetchError(f"Calendar could not be parsed ({exc.__class__.__name__}).") from exc

        try:
            import recurring_ical_events

            events = recurring_ical_events.of(calendar).between(window_start, window_end)
        except Exception as exc:
            # Fall back to non-recurring events rather than showing nothing.
            log.warning(
                "calendar.recurrence_expansion_failed",
                extra={"fields": {"error": f"{exc.__class__.__name__}: {exc}"}},
            )
            events = [
                component
                for component in calendar.walk("VEVENT")
                if self._overlaps(component, window_start, window_end)
            ]

        ignore_all_day = self.config.bool_("CALENDAR_IGNORE_ALL_DAY")
        ignore_cancelled = self.config.bool_("CALENDAR_IGNORE_DECLINED")

        meetings: list[Meeting] = []
        for component in events:
            try:
                meeting = self._to_meeting(component)
            except Exception:
                log.debug("calendar.event_skipped", exc_info=True)
                continue
            if meeting is None:
                continue
            if meeting.all_day and ignore_all_day:
                continue
            if meeting.cancelled and ignore_cancelled:
                continue
            meetings.append(meeting)

        meetings.sort(key=lambda m: (m.start, m.end, m.title))
        return meetings

    # -- helpers ---------------------------------------------------------
    def _overlaps(self, component: Any, start: datetime, end: datetime) -> bool:
        try:
            ev_start = self._as_datetime(component.get("DTSTART"))
            ev_end = self._as_datetime(component.get("DTEND")) or ev_start
        except Exception:
            return False
        if ev_start is None or ev_end is None:
            return False
        return ev_start < end and ev_end > start

    def _to_meeting(self, component: Any) -> Meeting | None:
        start = self._as_datetime(component.get("DTSTART"))
        if start is None:
            return None
        end = self._as_datetime(component.get("DTEND"))
        if end is None:
            duration = component.get("DURATION")
            if duration is not None and getattr(duration, "dt", None):
                end = start + duration.dt
            else:
                end = start + timedelta(minutes=30)
        if end <= start:
            # Zero-length events exist in the wild; give them a sane block so
            # they still render and still trigger the join logic.
            end = start + timedelta(minutes=30)

        all_day = self._is_all_day(component)

        title = self._text(component.get("SUMMARY")) or "Meeting"
        location = self._text(component.get("LOCATION"))
        description = self._text(component.get("DESCRIPTION"))
        html_description = self._text(component.get("X-ALT-DESC"))
        url_property = self._text(component.get("URL"))
        # Teams and Zoom both add a dedicated property; prefer it when present.
        for key in ("X-MICROSOFT-SKYPETEAMSMEETINGURL", "X-GOOGLE-CONFERENCE"):
            value = self._text(component.get(key))
            if value:
                url_property = value
                break

        join_url, provider = extract_meeting(
            url=url_property,
            description=description,
            location=location,
            extra=html_description,
        )

        status = (self._text(component.get("STATUS")) or "").upper()
        organizer = self._address(component.get("ORGANIZER"))
        attendees = self._addresses(component.get("ATTENDEE"))
        classification = (self._text(component.get("CLASS")) or "").upper()

        uid = self._text(component.get("UID")) or f"{title}-{start.isoformat()}"
        # A recurring series shares one UID; qualify it with the occurrence.
        occurrence_uid = f"{uid}@{int(start.timestamp())}"

        return Meeting(
            uid=occurrence_uid,
            title=title,
            start=start,
            end=end,
            all_day=all_day,
            location=location if not join_url else self._tidy_location(location),
            organizer=organizer,
            provider_id=provider.id if provider else "",
            join_url=join_url or "",
            cancelled=status == "CANCELLED",
            private=classification in ("PRIVATE", "CONFIDENTIAL"),
            attendees=attendees,
        )

    @staticmethod
    def _tidy_location(location: str) -> str:
        """Drop a location that is only the meeting URL."""
        text = (location or "").strip()
        if text.lower().startswith(("http://", "https://")):
            return ""
        return text

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        try:
            if hasattr(value, "to_ical"):
                raw = value.to_ical()
                if isinstance(raw, bytes):
                    return raw.decode("utf-8", "replace").strip()
            return str(value).strip()
        except Exception:
            return ""

    @classmethod
    def _address(cls, value: Any) -> str:
        """One CAL-ADDRESS as a plain email address.

        ``mailto:`` is stripped because every consumer of this wants an address
        to show or to send to, and none of them wants the scheme.
        """
        text = cls._text(value)
        if text.lower().startswith("mailto:"):
            text = text[7:]
        return text.strip()

    @classmethod
    def _addresses(cls, value: Any) -> list[str]:
        """Every ATTENDEE on an event, deduplicated, in the order given.

        ``icalendar`` hands back a single value when a property appears once and
        a list when it repeats, so both shapes have to be handled. Anything that
        is not an email address — a room resource, a mailing list expressed as a
        URI — is dropped rather than passed on to something that will try to
        send mail to it.
        """
        if value is None:
            return []
        items = value if isinstance(value, (list, tuple)) else [value]
        out: list[str] = []
        seen: set[str] = set()
        for item in items:
            address = cls._address(item)
            if "@" not in address or " " in address:
                continue
            key = address.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(address)
        return out

    @staticmethod
    def _is_all_day(component: Any) -> bool:
        dtstart = component.get("DTSTART")
        value = getattr(dtstart, "dt", None)
        if isinstance(value, datetime):
            return False
        if isinstance(value, date):
            return True
        # recurring_ical_events normalises to datetimes; fall back to the param.
        try:
            return str(dtstart.params.get("VALUE", "")).upper() == "DATE"
        except Exception:
            return False

    def _as_datetime(self, value: Any) -> datetime | None:
        """Normalise DTSTART/DTEND to an aware datetime in the room's zone."""
        if value is None:
            return None
        raw = getattr(value, "dt", value)
        tz = self.config.tz()

        if isinstance(raw, datetime):
            if raw.tzinfo is None:
                # Floating time: interpret in the room's own zone.
                return raw.replace(tzinfo=tz)
            return raw.astimezone(tz)
        if isinstance(raw, date):
            return datetime.combine(raw, time.min, tzinfo=tz)
        return None
