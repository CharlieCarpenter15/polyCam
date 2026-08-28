"""The room state machine: modes, auto-open, and never getting stuck."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.airplay_service import AirPlayService
from app.browser_service import BrowserService
from app.calendar_service import CalendarService
from app.meeting_service import MeetingService
from app.models import MODE_HOME, MODE_MEETING, MODE_OFFLINE, MODE_SHARING, Meeting
from app.poly_service import PolyService
from app.system_service import SystemService


class FakeBrowser:
    """Stands in for Chromium: records what it was asked to show."""

    def __init__(self):
        self.opened: list[str] = []
        self.home_count = 0
        self.left = 0
        self.alive = True
        self.retried = 0
        self.fail_open = False

    def open_meeting(self, meeting, *, reason=""):
        if self.fail_open:
            return False
        self.opened.append(meeting.uid)
        return True

    def go_home(self, *, reason=""):
        self.home_count += 1
        return True

    def leave_meeting(self, *, reason=""):
        self.left += 1
        return self.go_home(reason=reason)

    def bring_to_front(self):
        return True

    def retry_join(self):
        self.retried += 1
        return True

    def toggle_meeting_mute(self):
        return True

    def toggle_meeting_camera(self):
        return True

    def restart_browser(self, *, reason=""):
        return True

    def status(self):
        return {"enabled": True, "alive": self.alive, "ok": self.alive}


@pytest.fixture()
def room(mock_config):
    """A room wired up with a fake browser and no real hardware."""
    config = mock_config
    system = SystemService(config)
    calendar = CalendarService(config)
    airplay = AirPlayService(config, system)
    poly = PolyService(config)
    browser = FakeBrowser()
    service = MeetingService(config, calendar, browser, airplay, poly, system)
    return {
        "config": config,
        "calendar": calendar,
        "browser": browser,
        "airplay": airplay,
        "room": service,
    }


def set_meetings(calendar, meetings):
    calendar._snapshot.meetings = meetings
    calendar._snapshot.ok = True
    calendar._snapshot.stale = False
    calendar._snapshot.fetched_at = datetime.now(timezone.utc)


def teams_meeting(uid, start, minutes=30, link=True):
    return Meeting(
        uid=uid,
        title=f"Meeting {uid}",
        start=start,
        end=start + timedelta(minutes=minutes),
        provider_id="teams" if link else "",
        join_url="https://teams.microsoft.com/l/meetup-join/19%3aX/0" if link else "",
    )


class TestModes:
    def test_an_empty_room_shows_the_dashboard(self, room):
        set_meetings(room["calendar"], [])
        assert room["room"].tick() == MODE_HOME

    def test_no_network_shows_the_offline_state(self, room):
        set_meetings(room["calendar"], [])
        room["room"].set_network_ok(False)
        assert room["room"].tick() == MODE_OFFLINE

    def test_screen_sharing_takes_priority(self, room):
        set_meetings(room["calendar"], [])
        room["airplay"].handle_event("connected", client="A MacBook")
        assert room["room"].tick() == MODE_SHARING

    def test_screen_sharing_beats_offline(self, room):
        """Mirroring works on a LAN with no internet; say so honestly."""
        set_meetings(room["calendar"], [])
        room["room"].set_network_ok(False)
        room["airplay"].handle_event("connected")
        assert room["room"].tick() == MODE_SHARING

    def test_sharing_ends_and_the_dashboard_returns(self, room):
        set_meetings(room["calendar"], [])
        room["airplay"].handle_event("connected")
        assert room["room"].tick() == MODE_SHARING
        room["airplay"].handle_event("disconnected")
        assert room["room"].tick() == MODE_HOME


class TestAutoOpen:
    def test_a_meeting_opens_shortly_before_it_starts(self, room):
        now = datetime.now(timezone.utc)
        set_meetings(room["calendar"], [teams_meeting("soon", now + timedelta(seconds=30))])
        assert room["room"].tick() == MODE_MEETING
        assert room["browser"].opened == ["soon"]

    def test_a_distant_meeting_is_left_alone(self, room):
        now = datetime.now(timezone.utc)
        set_meetings(room["calendar"], [teams_meeting("later", now + timedelta(hours=2))])
        assert room["room"].tick() == MODE_HOME
        assert room["browser"].opened == []

    def test_a_meeting_with_no_link_is_never_opened(self, room):
        now = datetime.now(timezone.utc)
        set_meetings(
            room["calendar"], [teams_meeting("nolink", now + timedelta(seconds=10), link=False)]
        )
        assert room["room"].tick() == MODE_HOME
        assert room["browser"].opened == []

    def test_auto_open_can_be_switched_off(self, room):
        room["config"].update({"AUTO_OPEN_MEETING": False})
        now = datetime.now(timezone.utc)
        set_meetings(room["calendar"], [teams_meeting("soon", now + timedelta(seconds=10))])
        assert room["room"].tick() == MODE_HOME

    def test_a_meeting_already_running_is_joined_after_a_restart(self, room):
        """Rebooting mid-meeting should put the room back into the call."""
        now = datetime.now(timezone.utc)
        set_meetings(room["calendar"], [teams_meeting("running", now - timedelta(minutes=5))])
        assert room["room"].tick() == MODE_MEETING
        assert room["browser"].opened == ["running"]

    def test_a_meeting_is_not_reopened_after_someone_leaves(self, room):
        now = datetime.now(timezone.utc)
        set_meetings(room["calendar"], [teams_meeting("once", now + timedelta(seconds=10))])
        room["room"].tick()
        room["room"].leave_meeting()
        assert room["room"].tick() == MODE_HOME
        assert room["browser"].opened == ["once"], "must not reopen what was just left"

    def test_a_failed_open_does_not_claim_to_be_in_a_meeting(self, room):
        room["browser"].fail_open = True
        now = datetime.now(timezone.utc)
        set_meetings(room["calendar"], [teams_meeting("soon", now + timedelta(seconds=10))])
        assert room["room"].tick() == MODE_HOME

    def test_sharing_is_cleared_when_a_meeting_starts(self, room):
        now = datetime.now(timezone.utc)
        room["airplay"].handle_event("connected", client="A MacBook")
        set_meetings(room["calendar"], [teams_meeting("soon", now + timedelta(seconds=10))])
        room["room"].open_meeting(room["calendar"].meetings()[0])
        assert room["airplay"].sharing is False


class TestNeverStuck:
    def test_the_room_leaves_a_finished_meeting(self, room):
        now = datetime.now(timezone.utc)
        meeting = teams_meeting("done", now - timedelta(minutes=1), minutes=30)
        set_meetings(room["calendar"], [meeting])
        assert room["room"].tick() == MODE_MEETING

        # The meeting finishes: move its end well past the grace period.
        meeting.end = now - timedelta(minutes=30)
        assert room["room"].tick() == MODE_HOME
        assert room["browser"].home_count >= 1

    def test_a_meeting_that_already_ended_is_not_opened(self, room):
        """Arriving late to an over-and-done meeting should not hijack the TV."""
        now = datetime.now(timezone.utc)
        set_meetings(
            room["calendar"], [teams_meeting("over", now - timedelta(hours=2), minutes=30)]
        )
        assert room["room"].tick() == MODE_HOME
        assert room["browser"].opened == []

    def test_the_grace_period_is_respected(self, room):
        now = datetime.now(timezone.utc)
        room["config"].update({"RETURN_HOME_MINUTES": 10})
        meeting = teams_meeting("recent", now - timedelta(minutes=40), minutes=35)
        set_meetings(room["calendar"], [meeting])
        room["room"].open_meeting(meeting, manual=True)
        # Ended 5 minutes ago, grace is 10 -> still on the meeting.
        assert room["room"].tick() == MODE_MEETING

    def test_a_meeting_removed_from_the_calendar_is_left(self, room):
        now = datetime.now(timezone.utc)
        meeting = teams_meeting("vanishes", now - timedelta(minutes=20), minutes=10)
        set_meetings(room["calendar"], [meeting])
        room["room"].open_meeting(meeting, manual=True)
        set_meetings(room["calendar"], [])          # a successful refresh, now empty
        assert room["room"].tick() == MODE_HOME

    def test_an_unreachable_calendar_does_not_end_a_meeting_early(self, room):
        """A calendar outage must not kick the room out of a live call."""
        now = datetime.now(timezone.utc)
        meeting = teams_meeting("live", now - timedelta(minutes=5), minutes=60)
        set_meetings(room["calendar"], [meeting])
        room["room"].open_meeting(meeting, manual=True)

        room["calendar"]._snapshot.meetings = []
        room["calendar"]._snapshot.ok = False
        room["calendar"]._snapshot.stale = True

        assert room["room"].tick() == MODE_MEETING

    def test_the_hard_limit_always_wins(self, room):
        """Even a calendar claiming an endless meeting cannot pin the screen."""
        now = datetime.now(timezone.utc)
        room["config"].update({"MAX_MEETING_MINUTES": 10})
        forever = teams_meeting("forever", now - timedelta(hours=6), minutes=60 * 24)
        set_meetings(room["calendar"], [forever])
        room["room"].open_meeting(forever, manual=True)

        # Pretend it was opened six hours ago.
        state = room["room"].state()
        state.active.opened_at = now - timedelta(hours=6)

        assert room["room"].tick() == MODE_HOME

    def test_a_cancelled_meeting_is_left(self, room):
        now = datetime.now(timezone.utc)
        meeting = teams_meeting("cancelled", now - timedelta(minutes=1), minutes=60)
        set_meetings(room["calendar"], [meeting])
        room["room"].open_meeting(meeting, manual=True)
        meeting.cancelled = True
        assert room["room"].tick() == MODE_HOME


class TestManualJoin:
    def test_join_next_picks_the_running_meeting_first(self, room):
        now = datetime.now(timezone.utc)
        set_meetings(
            room["calendar"],
            [
                teams_meeting("running", now - timedelta(minutes=5)),
                teams_meeting("later", now + timedelta(hours=2)),
            ],
        )
        ok, detail = room["room"].join_next()
        assert ok and room["browser"].opened == ["running"]

    def test_join_next_falls_through_to_the_next_meeting(self, room):
        now = datetime.now(timezone.utc)
        set_meetings(room["calendar"], [teams_meeting("upcoming", now + timedelta(hours=3))])
        ok, _ = room["room"].join_next()
        assert ok and room["browser"].opened == ["upcoming"]

    def test_join_next_explains_an_empty_calendar(self, room):
        set_meetings(room["calendar"], [])
        ok, detail = room["room"].join_next()
        assert not ok and "no meeting" in detail.lower()

    def test_join_next_explains_a_meeting_with_no_link(self, room):
        now = datetime.now(timezone.utc)
        set_meetings(
            room["calendar"], [teams_meeting("nolink", now + timedelta(hours=1), link=False)]
        )
        ok, detail = room["room"].join_next()
        assert not ok and "no online meeting link" in detail.lower()

    def test_joining_a_specific_meeting(self, room):
        now = datetime.now(timezone.utc)
        set_meetings(
            room["calendar"],
            [
                teams_meeting("first", now + timedelta(hours=1)),
                teams_meeting("second", now + timedelta(hours=4)),
            ],
        )
        ok, _ = room["room"].join_meeting_id("second")
        assert ok and room["browser"].opened == ["second"]

    def test_joining_a_meeting_that_has_gone(self, room):
        set_meetings(room["calendar"], [])
        ok, detail = room["room"].join_meeting_id("ghost")
        assert not ok and "no longer" in detail.lower()


class TestRemoteActions:
    def test_join_and_hangup(self, room):
        now = datetime.now(timezone.utc)
        set_meetings(room["calendar"], [teams_meeting("m", now + timedelta(minutes=30))])
        assert room["room"].dispatch_action("join")["ok"]
        assert room["room"].dispatch_action("hangup")["ok"]
        assert room["browser"].left == 1

    def test_home_returns_to_the_dashboard(self, room):
        assert room["room"].dispatch_action("home")["ok"]
        assert room["browser"].home_count >= 1

    def test_mute_and_volume_work_in_mock_mode(self, room):
        assert room["room"].dispatch_action("mute")["ok"]
        up = room["room"].dispatch_action("volume_up")
        assert up["ok"] and isinstance(up["volume"], int)

    def test_an_unknown_action_is_refused(self, room):
        result = room["room"].dispatch_action("self_destruct")
        assert not result["ok"] and "Unknown action" in result["detail"]


class TestDashboardPayload:
    def test_the_payload_has_what_the_screen_needs(self, room):
        now = datetime.now(timezone.utc)
        set_meetings(
            room["calendar"],
            [
                teams_meeting("next", now + timedelta(hours=1)),
                teams_meeting("after", now + timedelta(hours=3)),
            ],
        )
        payload = room["room"].dashboard_payload()

        assert payload["mode"] in (MODE_HOME, MODE_MEETING, MODE_SHARING, MODE_OFFLINE)
        assert payload["room"]["name"] == "Test Room"
        assert payload["room"]["available"] is True
        assert payload["next"]["id"] == "next"
        assert payload["next"]["provider_name"] == "Microsoft Teams"
        assert payload["join_available"] is True
        assert len(payload["upcoming"]) == 2

    def test_meeting_urls_never_reach_the_browser(self, room):
        """The dashboard joins by id, so a link cannot leak into page source."""
        now = datetime.now(timezone.utc)
        set_meetings(room["calendar"], [teams_meeting("secret", now + timedelta(hours=1))])
        payload = room["room"].dashboard_payload()
        rendered = repr(payload)
        assert "meetup-join" not in rendered
        assert "join_url" not in rendered

    def test_titles_can_be_hidden_for_privacy(self, room):
        now = datetime.now(timezone.utc)
        room["config"].update({"CALENDAR_SHOW_TITLES": False})
        set_meetings(room["calendar"], [teams_meeting("m", now + timedelta(hours=1))])
        payload = room["room"].dashboard_payload()
        assert payload["next"]["title"] == "Busy"

    def test_a_running_meeting_marks_the_room_busy(self, room):
        now = datetime.now(timezone.utc)
        set_meetings(room["calendar"], [teams_meeting("now", now - timedelta(minutes=5))])
        payload = room["room"].dashboard_payload()
        assert payload["room"]["available"] is False
        assert payload["current"]["id"] == "now"
