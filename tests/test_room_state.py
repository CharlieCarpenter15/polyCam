"""The room state machine: modes, auto-open, and never getting stuck."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.airplay_service import AirPlayService
from app.browser_service import BrowserService
from app.calendar_service import CalendarService
from app.meeting_service import REMOTE_ACTION_TTL_SECONDS, MeetingService
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
        self.fronted = 0
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
        self.fronted += 1
        return True

    def retry_join(self):
        self.retried += 1
        return True

    def join_state(self):
        return {
            "running": False,
            "in_call": False,
            "waiting": False,
            "gave_up": False,
        }

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

    def test_a_manual_room_waits_to_be_asked(self, room):
        room["config"].update({"MEETING_JOIN_MODE": "manual"})
        now = datetime.now(timezone.utc)
        set_meetings(room["calendar"], [teams_meeting("soon", now + timedelta(seconds=10))])
        assert room["room"].tick() == MODE_HOME
        assert room["browser"].opened == []

    def test_a_manual_room_still_joins_when_somebody_presses_join(self, room):
        """Manual means "not by itself" — never "not at all"."""
        room["config"].update({"MEETING_JOIN_MODE": "manual"})
        now = datetime.now(timezone.utc)
        set_meetings(room["calendar"], [teams_meeting("soon", now + timedelta(seconds=10))])
        room["room"].tick()

        ok, _ = room["room"].join_next()
        assert ok and room["browser"].opened == ["soon"]
        assert room["room"].mode == MODE_MEETING

    def test_the_mode_is_reported_to_the_screens(self, room):
        assert room["room"].dashboard_payload()["join"]["mode"] == "automatic"
        room["config"].update({"MEETING_JOIN_MODE": "manual"})
        assert room["room"].dashboard_payload()["join"]["mode"] == "manual"

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


class TestJoiningIsIdempotent:
    """Every JOIN button in the room ends up in open_meeting.

    Pressing JOIN on the TV, then on a phone, then on the remote used to
    re-navigate each time, reloading the meeting page in the middle of the
    sign-in — which is what "it repeats logging into the meeting" looks like
    from a chair in the room.
    """

    def test_a_second_join_for_the_same_meeting_does_not_navigate_again(self, room):
        now = datetime.now(timezone.utc)
        meeting = teams_meeting("twice", now + timedelta(minutes=1))
        set_meetings(room["calendar"], [meeting])

        assert room["room"].open_meeting(meeting, manual=True) is True
        assert room["room"].open_meeting(meeting, manual=True) is True
        assert room["browser"].opened == ["twice"], "the page must not be reloaded"

    def test_the_second_press_still_brings_the_meeting_forward(self, room):
        now = datetime.now(timezone.utc)
        meeting = teams_meeting("front", now + timedelta(minutes=1))
        set_meetings(room["calendar"], [meeting])
        room["room"].open_meeting(meeting, manual=True)
        before = room["browser"].fronted
        room["room"].open_meeting(meeting, manual=True)
        assert room["browser"].fronted > before

    def test_a_manual_join_racing_the_scheduled_open_only_navigates_once(self, room):
        now = datetime.now(timezone.utc)
        set_meetings(room["calendar"], [teams_meeting("auto", now + timedelta(seconds=10))])
        assert room["room"].tick() == MODE_MEETING          # the scheduled open
        ok, _ = room["room"].join_meeting_id("auto")        # someone presses JOIN
        assert ok and room["browser"].opened == ["auto"]

    def test_a_different_meeting_still_opens(self, room):
        now = datetime.now(timezone.utc)
        first = teams_meeting("one", now + timedelta(minutes=1))
        second = teams_meeting("two", now + timedelta(minutes=2))
        set_meetings(room["calendar"], [first, second])
        room["room"].open_meeting(first, manual=True)
        room["room"].open_meeting(second, manual=True)
        assert room["browser"].opened == ["one", "two"]

    def test_the_meeting_opens_again_once_the_window_has_passed(self, room):
        """After the automation has had its go, JOIN means "really try again"."""
        now = datetime.now(timezone.utc)
        meeting = teams_meeting("stale", now - timedelta(minutes=1), minutes=60)
        set_meetings(room["calendar"], [meeting])
        room["room"].open_meeting(meeting, manual=True)

        active = room["room"].state().active
        active.opened_at = active.opened_at - timedelta(minutes=10)
        room["room"].open_meeting(meeting, manual=True)
        assert room["browser"].opened == ["stale", "stale"]

    def test_retrying_the_automation_never_navigates(self, room):
        now = datetime.now(timezone.utc)
        meeting = teams_meeting("retry", now + timedelta(minutes=1))
        set_meetings(room["calendar"], [meeting])
        room["room"].open_meeting(meeting, manual=True)
        assert room["room"].retry_join_automation() is True
        assert room["browser"].retried == 1
        assert room["browser"].opened == ["retry"]


class TestPressingJoinAtAPageThatStoppedShort:
    """The room is on the meeting page but not in the call, and JOIN is pressed.

    This is what "the join button does nothing" was: the page was already open,
    so every JOIN button in the room — the Poly remote, a phone, the TV —
    answered "joining…" and then sat there. Re-navigating is still wrong, but
    doing nothing at all is worse.
    """

    def test_the_remote_has_another_go_at_the_join_buttons(self, room):
        now = datetime.now(timezone.utc)
        set_meetings(room["calendar"], [teams_meeting("stuck", now - timedelta(minutes=1))])
        assert room["room"].tick() == MODE_MEETING          # opened by itself

        result = room["room"].dispatch_action("join")
        assert result["ok"] is True
        assert room["browser"].retried == 1, "the remote must retry the join"
        assert room["browser"].opened == ["stuck"], "and never reload the page"

    def test_a_phone_pressing_join_on_the_open_meeting_retries_too(self, room):
        now = datetime.now(timezone.utc)
        set_meetings(room["calendar"], [teams_meeting("stuck", now - timedelta(minutes=1))])
        room["room"].tick()

        ok, _ = room["room"].join_meeting_id("stuck")
        assert ok and room["browser"].retried == 1
        assert room["browser"].opened == ["stuck"]

    def test_the_scheduled_open_does_not_click_at_a_page_nobody_asked_about(self, room):
        """Only a person pressing JOIN means "try again"; a tick does not."""
        now = datetime.now(timezone.utc)
        meeting = teams_meeting("quiet", now - timedelta(minutes=1))
        set_meetings(room["calendar"], [meeting])
        room["room"].tick()
        room["room"].open_meeting(meeting, manual=False)
        assert room["browser"].retried == 0

    def test_a_room_that_joins_by_hand_is_left_alone(self, room):
        """With the automation off, somebody is working that page themselves."""
        room["config"].update({"AUTO_CLICK_JOIN": False})
        now = datetime.now(timezone.utc)
        set_meetings(room["calendar"], [teams_meeting("byhand", now - timedelta(minutes=1))])
        room["room"].tick()

        assert room["room"].dispatch_action("join")["ok"] is True
        assert room["browser"].retried == 0
        assert room["browser"].fronted > 0, "but the page still comes forward"


class TestLastRemoteAction:
    """What the TV and the phone controller show as a brief confirmation."""

    def test_an_action_is_recorded_in_the_shape_the_ui_expects(self, room):
        room["room"].dispatch_action("home")
        remote = room["room"].dashboard_payload()["remote"]
        assert set(remote) == {"action", "detail", "ok", "at", "source"}
        assert remote["action"] == "home"
        assert remote["detail"] == "Showing the room dashboard"
        assert remote["ok"] is True
        assert remote["source"] == "remote"
        datetime.fromisoformat(str(remote["at"]))  # ISO 8601, and parseable

    def test_nothing_is_shown_before_anything_is_pressed(self, room):
        assert room["room"].dashboard_payload()["remote"] is None

    def test_a_stale_action_disappears(self, room):
        """It is a confirmation, not a history: an old one is just wrong."""
        room["room"].dispatch_action("home")
        recent = room["room"]._last_remote
        recent.at = recent.at - timedelta(seconds=REMOTE_ACTION_TTL_SECONDS + 1)
        assert room["room"].dashboard_payload()["remote"] is None

    def test_the_source_is_remembered(self, room):
        room["room"].dispatch_action("home", source="controller")
        assert room["room"].dashboard_payload()["remote"]["source"] == "controller"

    def test_it_is_still_callable_with_one_argument(self, room):
        assert room["room"].dispatch_action("home")["ok"] is True

    def test_each_action_explains_itself(self, room):
        now = datetime.now(timezone.utc)
        set_meetings(room["calendar"], [teams_meeting("m", now + timedelta(minutes=5))])

        joined = room["room"].dispatch_action("join")
        assert joined["detail"] == "Joining Meeting m"

        muted = room["room"].dispatch_action("mute")
        assert muted["detail"] in ("Microphone muted", "Microphone on")

        volume = room["room"].dispatch_action("volume_up")
        assert volume["detail"] == f"Volume {volume['volume']}%"

        camera = room["room"].dispatch_action("camera")
        assert camera["detail"] == "Camera turned off"

        assert room["room"].dispatch_action("hangup")["detail"] == "Left the meeting"
        assert room["room"].dispatch_action("home")["detail"] == "Showing the room dashboard"

    def test_a_failed_action_is_recorded_as_a_failure(self, room):
        result = room["room"].dispatch_action("self_destruct")
        remote = room["room"].dashboard_payload()["remote"]
        assert result["ok"] is False
        assert remote["ok"] is False and "Unknown action" in remote["detail"]
