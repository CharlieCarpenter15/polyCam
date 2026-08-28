"""The feature's own health, and its place in the appliance's.

The rule this file exists to protect: a room whose meeting minutes are broken
is not a broken room. Somebody looking at a red appliance should be looking at
the calendar, the browser or the network, and an optional feature must never be
able to send them somewhere else.
"""

from __future__ import annotations

import json
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.minutes import paths as mpaths
from app.minutes.service import MinutesService
from app.minutes.transcript import SessionMeta
from app.models import FAIL, OFF, OK, WARN

Usage = namedtuple("Usage", "total used free")


class FakeState:
    def __init__(self, active=None):
        self.active = active
        self.mode = "home"


class FakeRoom:
    active = None

    def state(self):
        return FakeState(self.active)


class FakeCalendar:
    def find(self, meeting_id):
        return None

    def current_and_upcoming(self, now):
        return None, []


class FakeBrowser:
    target = "meeting"

    def read_meeting_page(self, script, *, timeout=6.0, user_gesture=False):
        return None


@pytest.fixture()
def service(room_dirs, config):
    config.update({"DEV_MODE": True, "CALENDAR_SOURCE": "mock", "TIMEZONE": "UTC"})
    mpaths.ensure_dirs()
    return MinutesService(config, FakeCalendar(), FakeRoom(), None, FakeBrowser())


def write_session(session_id, *, stage, title="Design review", error=""):
    directory = mpaths.SESSIONS_DIR / session_id
    directory.mkdir(parents=True, exist_ok=True)
    meta = SessionMeta(session_id=session_id, title=title, stage=stage, error=error)
    (directory / "meta.json").write_text(json.dumps(meta.to_dict()))
    return directory


def session_id_days_ago(days):
    stamp = datetime.now(timezone.utc) - timedelta(days=days)
    return f"{stamp.strftime('%Y%m%d-%H%M%S')}-abcdef12"


class TestTheFeaturesOwnHealth:
    def test_switched_off_is_not_a_fault(self, service):
        report = service.health()
        assert report["status"] == OFF
        assert "off" in report["detail"].lower()

    def test_switched_on_with_nothing_installed_only_warns(self, service):
        service.config.update({"MINUTES_ENABLED": True})
        assert service.health()["status"] in (OK, WARN)

    def test_it_never_reports_a_failure(self, service):
        """Whatever is wrong, this must not be why a room looks broken."""
        service.config.update(
            {
                "MINUTES_ENABLED": True,
                "MINUTES_SUMMARY_ENABLED": True,
                "MINUTES_EMAIL_ENABLED": True,
                "MINUTES_IDENTIFY_FACES": True,
                "MINUTES_IDENTIFY_VOICES": True,
            }
        )
        write_session(session_id_days_ago(0), stage="failed", error="the disk was full")
        assert service.health()["status"] != FAIL

    def test_a_part_left_switched_off_is_not_a_trouble(self, service):
        service.config.update({"MINUTES_ENABLED": True})
        troubles = " ".join(service.health()["troubles"])
        assert "switched off" not in troubles.lower()

    def test_a_switched_on_part_that_cannot_work_is_reported(self, service):
        service.config.update({"MINUTES_ENABLED": True, "MINUTES_SUMMARY_ENABLED": True})
        report = service.health()
        assert report["status"] == WARN
        assert any("summar" in trouble.lower() for trouble in report["troubles"])

    def test_a_recent_failure_is_reported(self, service):
        service.config.update({"MINUTES_ENABLED": True})
        write_session(session_id_days_ago(0), stage="failed", error="no audio device")
        troubles = " ".join(service.health()["troubles"])
        assert "no audio device" in troubles

    def test_an_old_failure_is_history_not_a_fault(self, service):
        service.config.update({"MINUTES_ENABLED": True})
        write_session(session_id_days_ago(30), stage="failed", error="ancient history")
        troubles = " ".join(service.health()["troubles"])
        assert "ancient history" not in troubles

    def test_it_counts_what_is_there(self, service):
        service.config.update({"MINUTES_ENABLED": True})
        write_session(session_id_days_ago(1), stage="summarised")
        report = service.health()
        assert report["sessions"] == 1
        assert report["people"] == 0
        assert report["recording"] is False


class TestTheApplianceHealth:
    def _health(self, config, minutes=None):
        from app.airplay_service import AirPlayService
        from app.browser_service import BrowserService
        from app.calendar_service import CalendarService
        from app.health_service import HealthService
        from app.meeting_service import MeetingService
        from app.poly_service import PolyService
        from app.system_service import SystemService

        system = SystemService(config)
        calendar = CalendarService(config)
        browser = BrowserService(config, system)
        airplay = AirPlayService(config, system)
        poly = PolyService(config)
        room = MeetingService(config, calendar, browser, airplay, poly, system)
        return HealthService(
            config, calendar, browser, airplay, poly, room, system, minutes=minutes
        )

    def test_without_the_feature_the_report_does_not_mention_it(self, room_dirs, config):
        config.update({"DEV_MODE": True, "CALENDAR_SOURCE": "mock"})
        report = self._health(config).report(network_ok=True)
        assert "minutes" not in report["components"]
        assert report["minutes"] is None

    def test_with_it_switched_off_it_shows_as_disabled(self, room_dirs, config, service):
        config.update({"DEV_MODE": True, "CALENDAR_SOURCE": "mock"})
        report = self._health(config, minutes=service).report(network_ok=True)
        assert report["components"]["minutes"] == OFF

    def test_a_broken_feature_does_not_change_how_the_room_is_doing(
        self, room_dirs, config, service
    ):
        """Compared against the same room without it, rather than against "not
        broken" — a test appliance has no kiosk, so it is already unwell for
        reasons that have nothing to do with this."""
        config.update({"DEV_MODE": True, "CALENDAR_SOURCE": "mock"})
        without = self._health(config).report(network_ok=True)["status"]

        config.update({"MINUTES_ENABLED": True, "MINUTES_SUMMARY_ENABLED": True})
        report = self._health(config, minutes=service).report(network_ok=True)

        assert report["components"]["minutes"] == WARN
        assert report["status"] == without, "an optional feature moved the room's status"

    def test_a_feature_that_throws_is_survived(self, room_dirs, config):
        class Exploding:
            def health(self):
                raise RuntimeError("everything is on fire")

        config.update({"DEV_MODE": True, "CALENDAR_SOURCE": "mock"})
        without = self._health(config).report(network_ok=True)["status"]
        report = self._health(config, minutes=Exploding()).report(network_ok=True)

        assert report["components"]["minutes"] == WARN
        assert report["status"] == without

    def test_a_healthy_feature_is_reported_as_healthy(self, room_dirs, config, service):
        config.update(
            {"DEV_MODE": True, "CALENDAR_SOURCE": "mock", "MINUTES_ENABLED": True}
        )
        report = self._health(config, minutes=service).report(network_ok=True)
        assert report["components"]["minutes"] in (OK, WARN)
        assert report["minutes"]["sessions"] == 0


class TestDiskArithmetic:
    """The bug that made a healthy disk look full."""

    def test_free_space_is_measured_against_what_is_addressable(self, config):
        from app.system_service import SystemService

        system = SystemService(config)
        # A real machine: 252 GB filesystem, 9.3 GB used, 30.5 GB available to
        # a non-root process. Dividing by `total` gave 11.3% and refused writes.
        usage = Usage(270_600_000_000, 9_300_000_000, 30_500_000_000)
        with patch("shutil.disk_usage", return_value=usage):
            assert system.disk_free_percent() == pytest.approx(76.6, abs=0.1)

    def test_a_genuinely_full_disk_still_reads_full(self, config):
        from app.system_service import SystemService

        system = SystemService(config)
        with patch("shutil.disk_usage", return_value=Usage(1000, 990, 10)):
            assert system.disk_free_percent() == pytest.approx(1.0, abs=0.1)

    def test_nothing_addressable_is_unknown_rather_than_a_division(self, config):
        from app.system_service import SystemService

        system = SystemService(config)
        with patch("shutil.disk_usage", return_value=Usage(0, 0, 0)):
            assert system.disk_free_percent() is None

    def test_an_unreadable_path_is_unknown(self, config):
        from app.system_service import SystemService

        system = SystemService(config)
        with patch("shutil.disk_usage", side_effect=OSError("gone")):
            assert system.disk_free_percent() is None
