"""Calendar parsing and outage resilience."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.calendar_service import CalendarService
from app.providers import build_provider
from app.providers.base import CalendarFetchError
from app.providers.ics import IcsCalendarProvider
from app.providers.mock import MockCalendarProvider

def fmt(moment) -> str:
    """UTC timestamp in the form an ICS file uses."""
    return moment.strftime("%Y%m%dT%H%M%SZ")


@pytest.fixture()
def window(ics_now):
    """A window that covers every event in the sample feed."""
    return ics_now - timedelta(hours=1), ics_now + timedelta(hours=12)


@pytest.fixture()
def ics_config(config, sample_ics):
    config.update(
        {
            "CALENDAR_SOURCE": "ics",
            "CALENDAR_ICS_URL": str(sample_ics),
            "TIMEZONE": "UTC",
            "CALENDAR_REFRESH_SECONDS": 10,
        }
    )
    return config


class TestIcsParsing:
    def test_events_are_parsed_with_their_providers(self, ics_config, window):
        meetings = IcsCalendarProvider(ics_config).fetch(*window)
        by_title = {m.title: m for m in meetings}

        assert by_title["Engineering Daily"].provider_id == "teams"
        assert by_title["Supplier Call"].provider_id == "meet"
        assert by_title["In-person Workshop"].provider_id == ""
        assert by_title["In-person Workshop"].has_link is False

    def test_recurring_events_are_expanded(self, ics_config, ics_now):
        """A daily stand-up must appear on a later day, not just its first."""
        later_start = ics_now + timedelta(days=5, hours=-1)
        meetings = IcsCalendarProvider(ics_config).fetch(
            later_start, later_start + timedelta(hours=3)
        )
        assert [m.title for m in meetings] == ["Engineering Daily"]

    def test_occurrences_get_distinct_ids(self, ics_config, ics_now):
        provider = IcsCalendarProvider(ics_config)
        today = provider.fetch(ics_now, ics_now + timedelta(hours=2))
        tomorrow = provider.fetch(
            ics_now + timedelta(days=1), ics_now + timedelta(days=1, hours=2)
        )
        first = next(m for m in today if m.title == "Engineering Daily")
        second = next(m for m in tomorrow if m.title == "Engineering Daily")
        assert first.uid != second.uid

    def test_meetings_are_sorted_by_start(self, ics_config, window):
        meetings = IcsCalendarProvider(ics_config).fetch(*window)
        assert meetings == sorted(meetings, key=lambda m: m.start)

    def test_location_holding_only_the_url_is_cleared(self, ics_config, window):
        meetings = IcsCalendarProvider(ics_config).fetch(*window)
        supplier = next(m for m in meetings if m.title == "Supplier Call")
        assert supplier.location == ""

    def test_all_day_and_cancelled_events_are_filtered(self, config, tmp_path, ics_now):
        path = tmp_path / "mixed.ics"
        path.write_text(
            "\r\n".join(
                [
                    "BEGIN:VCALENDAR",
                    "VERSION:2.0",
                    "PRODID:-//T//EN",
                    "BEGIN:VEVENT",
                    "UID:holiday@x",
                    f"DTSTAMP:{fmt(ics_now)}",
                    f"DTSTART;VALUE=DATE:{ics_now:%Y%m%d}",
                    f"DTEND;VALUE=DATE:{ics_now + timedelta(days=1):%Y%m%d}",
                    "SUMMARY:Public Holiday",
                    "END:VEVENT",
                    "BEGIN:VEVENT",
                    "UID:gone@x",
                    f"DTSTAMP:{fmt(ics_now)}",
                    f"DTSTART:{fmt(ics_now + timedelta(hours=1))}",
                    f"DTEND:{fmt(ics_now + timedelta(hours=2))}",
                    "SUMMARY:Cancelled Thing",
                    "STATUS:CANCELLED",
                    "END:VEVENT",
                    "BEGIN:VEVENT",
                    "UID:real@x",
                    f"DTSTAMP:{fmt(ics_now)}",
                    f"DTSTART:{fmt(ics_now + timedelta(hours=3))}",
                    f"DTEND:{fmt(ics_now + timedelta(hours=4))}",
                    "SUMMARY:Real Meeting",
                    "END:VEVENT",
                    "END:VCALENDAR",
                ]
            ),
            encoding="utf-8",
        )
        config.update({"CALENDAR_SOURCE": "ics", "CALENDAR_ICS_URL": str(path), "TIMEZONE": "UTC"})
        titles = [
            m.title
            for m in IcsCalendarProvider(config).fetch(
                ics_now - timedelta(hours=1), ics_now + timedelta(hours=8)
            )
        ]
        assert titles == ["Real Meeting"]

    def test_private_events_hide_their_title(self, config, tmp_path, ics_now, window):
        path = tmp_path / "private.ics"
        path.write_text(
            "\r\n".join(
                [
                    "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//T//EN",
                    "BEGIN:VEVENT",
                    "UID:p@x", f"DTSTAMP:{fmt(ics_now)}",
                    f"DTSTART:{fmt(ics_now + timedelta(hours=2))}",
                    f"DTEND:{fmt(ics_now + timedelta(hours=3))}",
                    "SUMMARY:Performance Review", "CLASS:PRIVATE",
                    "END:VEVENT", "END:VCALENDAR",
                ]
            ),
            encoding="utf-8",
        )
        config.update({"CALENDAR_SOURCE": "ics", "CALENDAR_ICS_URL": str(path), "TIMEZONE": "UTC"})
        meeting = IcsCalendarProvider(config).fetch(*window)[0]
        assert meeting.display_title(True) == "Busy"
        assert meeting.to_dict()["title"] == "Busy"

    def test_an_event_with_no_end_still_gets_a_duration(self, config, tmp_path, ics_now, window):
        path = tmp_path / "noend.ics"
        path.write_text(
            "\r\n".join(
                [
                    "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//T//EN",
                    "BEGIN:VEVENT",
                    "UID:n@x", f"DTSTAMP:{fmt(ics_now)}",
                    f"DTSTART:{fmt(ics_now + timedelta(hours=2))}", "SUMMARY:No End",
                    "END:VEVENT", "END:VCALENDAR",
                ]
            ),
            encoding="utf-8",
        )
        config.update({"CALENDAR_SOURCE": "ics", "CALENDAR_ICS_URL": str(path), "TIMEZONE": "UTC"})
        meeting = IcsCalendarProvider(config).fetch(*window)[0]
        assert meeting.end > meeting.start

    def test_floating_times_use_the_room_timezone(self, config, tmp_path, ics_now):
        path = tmp_path / "floating.ics"
        path.write_text(
            "\r\n".join(
                [
                    "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//T//EN",
                    "BEGIN:VEVENT",
                    "UID:f@x", f"DTSTAMP:{fmt(ics_now)}",
                    f"DTSTART:{ics_now + timedelta(days=1):%Y%m%dT}090000",
                    f"DTEND:{ics_now + timedelta(days=1):%Y%m%dT}093000",
                    "SUMMARY:Floating", "END:VEVENT", "END:VCALENDAR",
                ]
            ),
            encoding="utf-8",
        )
        config.update(
            {"CALENDAR_SOURCE": "ics", "CALENDAR_ICS_URL": str(path), "TIMEZONE": "Asia/Singapore"}
        )
        meeting = IcsCalendarProvider(config).fetch(
            ics_now, ics_now + timedelta(days=2)
        )[0]
        assert meeting.start.hour == 9
        assert str(meeting.start.tzinfo) == "Asia/Singapore"


class TestIcsErrors:
    def test_a_missing_file_is_reported_clearly(self, config, window):
        config.update({"CALENDAR_SOURCE": "ics", "CALENDAR_ICS_URL": "/nope/missing.ics"})
        with pytest.raises(CalendarFetchError, match="Cannot read"):
            IcsCalendarProvider(config).fetch(*window)

    def test_a_sign_in_page_gets_a_helpful_message(self, config, tmp_path, window):
        path = tmp_path / "login.html"
        path.write_text("<!DOCTYPE html><html><body>Sign in</body></html>", encoding="utf-8")
        config.update({"CALENDAR_SOURCE": "ics", "CALENDAR_ICS_URL": str(path)})
        with pytest.raises(CalendarFetchError, match="web page was returned"):
            IcsCalendarProvider(config).fetch(*window)

    def test_an_empty_feed_is_reported(self, config, tmp_path, window):
        path = tmp_path / "empty.ics"
        path.write_text("", encoding="utf-8")
        config.update({"CALENDAR_SOURCE": "ics", "CALENDAR_ICS_URL": str(path)})
        with pytest.raises(CalendarFetchError, match="empty"):
            IcsCalendarProvider(config).fetch(*window)

    def test_no_url_configured(self, config, window):
        config.update({"CALENDAR_SOURCE": "ics"})
        provider = IcsCalendarProvider(config)
        assert not provider.is_configured()
        with pytest.raises(CalendarFetchError):
            provider.fetch(*window)


class TestProviderSelection:
    def test_source_setting_picks_the_provider(self, config):
        config.update({"CALENDAR_SOURCE": "mock"})
        assert isinstance(build_provider(config), MockCalendarProvider)
        config.update({"CALENDAR_SOURCE": "ics"})
        assert isinstance(build_provider(config), IcsCalendarProvider)

    def test_the_disabled_provider_returns_nothing(self, config, window):
        config.update({"CALENDAR_SOURCE": "none"})
        provider = build_provider(config)
        assert provider.fetch(*window) == []

    def test_the_mock_provider_invents_a_believable_day(self, config):
        config.update({"CALENDAR_SOURCE": "mock", "TIMEZONE": "UTC"})
        now = datetime.now(timezone.utc)
        meetings = MockCalendarProvider(config).fetch(now, now + timedelta(hours=12))
        assert len(meetings) >= 4
        assert {m.provider_id for m in meetings} >= {"teams", "meet", "zoom"}
        assert all(m.end > m.start for m in meetings)

    def test_a_secret_url_is_not_in_the_description(self, ics_config):
        """The diagnostics page shows describe(); it must not leak the token."""
        ics_config.update({"CALENDAR_ICS_URL": "https://example.com/x.ics?token=SECRET"})
        assert "SECRET" not in IcsCalendarProvider(ics_config).describe()


class TestOutageResilience:
    def test_meetings_survive_a_calendar_outage(self, ics_config, sample_ics):
        service = CalendarService(ics_config)
        service._refresh_once()
        healthy = len(service.meetings())
        assert healthy > 0 and service.snapshot.ok

        sample_ics.unlink()
        service._refresh_once()

        assert len(service.meetings()) == healthy, "cached meetings must stay on screen"
        assert service.snapshot.stale
        assert not service.snapshot.ok
        assert service.snapshot.error

    def test_recovery_clears_the_stale_flag(self, ics_config, sample_ics):
        service = CalendarService(ics_config)
        service._refresh_once()
        contents = sample_ics.read_text(encoding="utf-8")
        sample_ics.unlink()
        service._refresh_once()
        assert service.snapshot.stale

        sample_ics.write_text(contents, encoding="utf-8")
        service._refresh_once()
        assert service.snapshot.ok and not service.snapshot.stale

    def test_a_reboot_during_an_outage_restores_the_cache(self, ics_config, sample_ics):
        first = CalendarService(ics_config)
        first._refresh_once()
        expected = len(first.meetings())

        sample_ics.unlink()
        second = CalendarService(ics_config)  # as if the process restarted
        assert len(second.meetings()) == expected
        assert second.snapshot.stale

    def test_the_cache_is_scoped_to_its_source(self, config, room_dirs):
        """Switching from the mock calendar must not leave invented meetings up."""
        config.update({"CALENDAR_SOURCE": "mock"})
        mock_service = CalendarService(config)
        mock_service._refresh_once()
        assert mock_service.meetings()

        config.update({"CALENDAR_SOURCE": "ics", "CALENDAR_ICS_URL": "/nope.ics"})
        real_service = CalendarService(config)
        assert real_service.meetings() == []

    def test_the_cache_file_is_not_world_readable(self, ics_config):
        from app import paths

        service = CalendarService(ics_config)
        service._refresh_once()
        mode = paths.CALENDAR_CACHE.stat().st_mode & 0o777
        assert mode == 0o600, "the cache holds meeting URLs"

    def test_failures_are_counted_for_backoff(self, config):
        config.update({"CALENDAR_SOURCE": "ics", "CALENDAR_ICS_URL": "/nope.ics"})
        service = CalendarService(config)
        for _ in range(3):
            service._refresh_once()
        assert service.status()["consecutive_failures"] == 3


class TestQueries:
    def test_current_and_upcoming_are_separated(self, config):
        from app.models import Meeting

        config.update({"CALENDAR_SOURCE": "mock", "TIMEZONE": "UTC"})
        service = CalendarService(config)
        now = datetime.now(timezone.utc)
        service._snapshot.meetings = [
            Meeting("running", "Running", now - timedelta(minutes=5), now + timedelta(minutes=25)),
            Meeting("later", "Later", now + timedelta(hours=1), now + timedelta(hours=2)),
            Meeting("past", "Past", now - timedelta(hours=2), now - timedelta(hours=1)),
        ]
        current, upcoming = service.current_and_upcoming(now)
        assert current.uid == "running"
        assert [m.uid for m in upcoming] == ["later"]

    def test_the_most_recent_start_wins_on_overlap(self, config):
        from app.models import Meeting

        config.update({"CALENDAR_SOURCE": "mock", "TIMEZONE": "UTC"})
        service = CalendarService(config)
        now = datetime.now(timezone.utc)
        service._snapshot.meetings = [
            Meeting("early", "Early", now - timedelta(hours=1), now + timedelta(hours=1)),
            Meeting("late", "Late", now - timedelta(minutes=1), now + timedelta(minutes=29)),
        ]
        current, _ = service.current_and_upcoming(now)
        assert current.uid == "late"

    def test_lookup_by_id(self, ics_config):
        service = CalendarService(ics_config)
        service._refresh_once()
        first = service.meetings()[0]
        assert service.find(first.uid) is first
        assert service.find("nonexistent") is None
