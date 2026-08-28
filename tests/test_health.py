"""Health reporting and automatic recovery."""

from __future__ import annotations

from app.airplay_service import AirPlayService
from app.calendar_service import CalendarService
from app.health_service import HealthService
from app.meeting_service import MeetingService
from app.models import FAIL, OFF, OK, UNKNOWN, WARN
from app.poly_service import PolyService
from app.system_service import SystemService
import pytest


class FakeBrowser:
    def __init__(self, alive=True, enabled=True):
        self.alive = alive
        self.enabled_flag = enabled
        self.restarts = 0
        self.enforced = 0
        self.enforce_result = "ok"

    @property
    def enabled(self):
        return self.enabled_flag

    def status(self):
        return {"enabled": self.enabled_flag, "alive": self.alive, "ok": self.alive}

    def enforce_target(self):
        self.enforced += 1
        return self.enforce_result

    def restart_browser(self, *, reason=""):
        self.restarts += 1
        self.alive = True
        return True

    def go_home(self, *, reason=""):
        return True

    def leave_meeting(self, *, reason=""):
        return True

    def bring_to_front(self):
        return True

    def open_meeting(self, meeting, *, reason=""):
        return True

    def retry_join(self):
        return True

    def toggle_meeting_mute(self):
        return True

    def toggle_meeting_camera(self):
        return True


def build(config, browser=None):
    system = SystemService(config)
    calendar = CalendarService(config)
    airplay = AirPlayService(config, system)
    poly = PolyService(config)
    browser = browser or FakeBrowser()
    room = MeetingService(config, calendar, browser, airplay, poly, system)
    health = HealthService(config, calendar, browser, airplay, poly, room, system)
    return health, browser, room, calendar


class TestReporting:
    def test_the_report_covers_every_component(self, mock_config):
        health, _, _, _ = build(mock_config)
        report = health.report(network_ok=True)
        assert set(report["components"]) == {
            "backend", "calendar", "browser", "airplay",
            "camera", "microphone", "speaker", "network",
        }

    def test_the_report_includes_the_mode(self, mock_config):
        health, _, _, _ = build(mock_config)
        assert health.report(network_ok=True)["mode"] in (
            "home", "meeting", "screen-sharing", "offline"
        )

    def test_a_disabled_kiosk_does_not_make_the_room_broken(self, mock_config):
        """Development mode has no Chromium; that is not a fault."""
        health, _, _, _ = build(mock_config, FakeBrowser(alive=False, enabled=False))
        report = health.report(network_ok=True)
        assert report["components"]["browser"] == OFF
        assert report["status"] != FAIL

    def test_a_dead_browser_is_a_failure(self, mock_config):
        health, _, _, _ = build(mock_config, FakeBrowser(alive=False, enabled=True))
        report = health.report(network_ok=True)
        assert report["components"]["browser"] == FAIL
        assert report["status"] == FAIL

    def test_no_network_is_a_warning_not_a_dead_room(self, mock_config):
        """The dashboard still works offline, so the room is degraded, not down."""
        health, _, _, _ = build(mock_config)
        report = health.report(network_ok=False)
        assert report["components"]["network"] == FAIL
        assert report["status"] == WARN

    def test_a_calendar_outage_with_cached_meetings_is_a_warning(self, mock_config):
        health, _, _, calendar = build(mock_config)
        calendar._refresh_once()                       # mock source: succeeds
        calendar._snapshot.ok = False
        calendar._snapshot.stale = True
        calendar._snapshot.error = "Cannot reach the calendar server."
        report = health.report(network_ok=True)
        assert report["components"]["calendar"] == WARN

    def test_a_calendar_outage_with_nothing_cached_is_an_error(self, mock_config):
        health, _, _, calendar = build(mock_config)
        calendar._snapshot.meetings = []
        calendar._snapshot.ok = False
        calendar._snapshot.error = "Cannot reach the calendar server."
        assert health.report(network_ok=True)["components"]["calendar"] == FAIL

    def test_a_disabled_calendar_is_not_a_problem(self, mock_config):
        mock_config.update({"CALENDAR_SOURCE": "none"})
        health, _, _, _ = build(mock_config)
        assert health.report(network_ok=True)["components"]["calendar"] == OFF

    def test_the_report_includes_host_facts(self, mock_config):
        health, _, _, _ = build(mock_config)
        host = health.report(network_ok=True)["host"]
        assert "uptime_seconds" in host and "load" in host

    def test_config_warnings_are_surfaced(self, mock_config, room_dirs):
        from app.config import ConfigManager
        import yaml

        room_dirs["file"].write_text(
            yaml.safe_dump({"CALENDAR_REFRESH_SECONDS": -1}), encoding="utf-8"
        )
        broken = ConfigManager(room_dirs["file"])
        health, _, _, _ = build(broken)
        assert health.report(network_ok=True)["backend"]["config_warnings"]


class TestRecovery:
    def test_a_healthy_browser_is_only_checked_for_drift(self, mock_config):
        health, browser, _, _ = build(mock_config)
        health._recover_browser(health.report(network_ok=True))
        assert browser.enforced == 1
        assert browser.restarts == 0

    def test_drift_is_recorded_as_a_repair(self, mock_config):
        health, browser, _, _ = build(mock_config)
        browser.enforce_result = "recovered-dashboard"
        health._recover_browser(health.report(network_ok=True))
        assert health.report(network_ok=True)["recoveries"]

    def test_a_dead_browser_is_restarted_after_a_few_misses(self, mock_config):
        """One missed probe could be a slow moment; four is a dead browser."""
        health, browser, _, _ = build(mock_config, FakeBrowser(alive=False))
        for _ in range(3):
            health._recover_browser(health.report(network_ok=True))
        assert browser.restarts == 0

        health._recover_browser(health.report(network_ok=True))
        assert browser.restarts == 1

    def test_a_disabled_kiosk_is_never_restarted(self, mock_config):
        health, browser, _, _ = build(mock_config, FakeBrowser(alive=False, enabled=False))
        for _ in range(8):
            health._recover_browser(health.report(network_ok=True))
        assert browser.restarts == 0

    def test_recovery_can_be_switched_off(self, mock_config):
        mock_config.update({"AUTO_RECOVER_BROWSER": False})
        health, browser, _, _ = build(mock_config, FakeBrowser(alive=False))
        for _ in range(8):
            health._recover_browser(health.report(network_ok=True))
        assert browser.restarts == 0 and browser.enforced == 0

    def test_the_state_file_is_written_for_the_watchdog(self, mock_config):
        from app import paths
        from app.store import read_json

        health, _, _, _ = build(mock_config)
        health.check()
        state = read_json(paths.STATE_FILE)
        assert state and "mode" in state and "overall" in state

    def test_a_network_change_is_reported_to_the_room(self, mock_config):
        health, _, room, _ = build(mock_config)
        health.room.set_network_ok(False)
        assert room.tick() == "offline"
        health.room.set_network_ok(True)
        assert room.tick() == "home"

    def test_the_recovery_log_is_bounded(self, mock_config):
        health, _, _, _ = build(mock_config)
        for index in range(40):
            health._note_recovery("browser", f"repair {index}")
        assert len(health.report(network_ok=True)["recoveries"]) <= 20
