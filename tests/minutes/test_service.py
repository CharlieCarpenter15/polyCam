"""The orchestrator: when to record, what to do afterwards, and what to skip.

These tests stand in for the rest of the appliance with plain fakes, because
the point of the design is that this module only ever *reads* the room's state.
If a fake with a ``state()`` method is enough to drive the whole lifecycle, then
nothing here can reach back into the room and break it — which is the property
the whole feature depends on.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.minutes import paths as mpaths
from app.minutes import service as svc_module
from app.minutes.service import (
    STAGE_CAPTURED,
    STAGE_RECORDING,
    MinutesService,
)
from app.minutes.transcript import (
    SOURCE_MANUAL,
    TRACK_ROOM,
    Segment,
    SessionMeta,
    Transcript,
)


class FakeActive:
    def __init__(self, meeting_id="evt-1", title="Design review", provider="teams"):
        self.meeting_id = meeting_id
        self.title = title
        self.provider_id = provider


class FakeState:
    def __init__(self, active):
        self.active = active
        self.mode = "meeting" if active else "home"


class FakeRoom:
    def __init__(self):
        self.active = None

    def state(self):
        return FakeState(self.active)


class FakeMeeting:
    organizer = "charlie@example.com"
    attendees = ["priya@example.com", "sam@example.com"]

    def __init__(self, minutes_away=None):
        self._away = minutes_away

    @property
    def has_link(self):
        return True

    def minutes_until(self, now):
        return self._away if self._away is not None else 999.0


class FakeCalendar:
    def __init__(self, upcoming=()):
        self._upcoming = list(upcoming)

    def find(self, meeting_id):
        return FakeMeeting()

    def current_and_upcoming(self, now):
        return None, list(self._upcoming)


class FakeBrowser:
    target = "meeting"

    def read_meeting_page(self, script, timeout=6.0):
        return None


@pytest.fixture()
def service(room_dirs, config):
    config.update(
        {
            "DEV_MODE": True,
            "CALENDAR_SOURCE": "mock",
            "MINUTES_ENABLED": True,
            "MINUTES_MIN_MEETING_SECONDS": 0,
            "TIMEZONE": "UTC",
        }
    )
    mpaths.ensure_dirs()
    service = MinutesService(config, FakeCalendar(), FakeRoom(), None, FakeBrowser())
    service.people.load()
    return service


def record_a_meeting(service, *, active=None):
    service.room.active = active or FakeActive()
    service.tick()
    service.room.active = None
    service.tick()
    sessions = service.list_sessions()
    return sessions[0]["session_id"] if sessions else ""


class TestTheSwitch:
    def test_nothing_happens_when_it_is_off(self, room_dirs, config):
        config.update({"DEV_MODE": True, "MINUTES_ENABLED": False})
        service = MinutesService(config, FakeCalendar(), FakeRoom(), None, FakeBrowser())
        service.room.active = FakeActive()
        service.tick()
        assert service.status()["recording"] is None
        assert service.list_sessions() == []

    def test_start_does_not_run_a_thread_when_it_is_off(self, room_dirs, config):
        config.update({"DEV_MODE": True, "MINUTES_ENABLED": False})
        service = MinutesService(config, FakeCalendar(), FakeRoom(), None, FakeBrowser())
        service.start()
        assert service._supervisor is None

    def test_the_dashboard_says_nothing_when_it_is_off(self, room_dirs, config):
        config.update({"DEV_MODE": True, "MINUTES_ENABLED": False})
        service = MinutesService(config, FakeCalendar(), FakeRoom(), None, FakeBrowser())
        payload = service.dashboard_payload()
        assert payload == {"enabled": False, "recording": False, "notice": ""}


class TestTheRecordingLifecycle:
    def test_a_meeting_starts_and_stops_a_recording(self, service):
        service.room.active = FakeActive()
        service.tick()
        assert service.status()["recording"]["title"] == "Design review"

        service.room.active = None
        service.tick()
        assert service.status()["recording"] is None
        assert len(service.list_sessions()) == 1

    def test_the_calendar_invitation_is_carried_into_the_session(self, service):
        session_id = record_a_meeting(service)
        detail = service.get_session(session_id)
        assert detail["meta"]["invited"] == ["priya@example.com", "sam@example.com"]
        assert detail["meta"]["organizer"] == "charlie@example.com"

    def test_one_meeting_straight_after_another_makes_two_sessions(self, service):
        service.room.active = FakeActive("evt-1", "Standup")
        service.tick()
        service.room.active = FakeActive("evt-2", "Design review")
        service.tick()
        service.room.active = None
        service.tick()
        titles = {s["title"] for s in service.list_sessions()}
        assert titles == {"Standup", "Design review"}

    def test_a_misfire_is_thrown_away(self, service):
        service.config.update({"MINUTES_MIN_MEETING_SECONDS": 600})
        record_a_meeting(service)
        assert service.list_sessions() == [], "a meeting joined and left is not a meeting"

    def test_the_recording_notice_changes_while_recording(self, service):
        assert "are recorded" in service.dashboard_payload()["notice"]
        service.room.active = FakeActive()
        service.tick()
        payload = service.dashboard_payload()
        assert payload["recording"] is True
        assert "Recording" in payload["notice"]

    def test_the_notice_can_be_turned_off(self, service):
        service.config.update({"MINUTES_SHOW_RECORDING_NOTICE": False})
        assert service.dashboard_payload()["notice"] == ""

    def test_stopping_finishes_the_recording_in_progress(self, service):
        service.room.active = FakeActive()
        service.tick()
        service.stop()
        assert service.list_sessions(), "a shutdown must not lose the meeting"


class TestProcessing:
    def test_a_captured_session_becomes_a_transcript(self, service):
        session_id = record_a_meeting(service)
        ok, error = service.process(session_id)
        assert ok, error
        detail = service.get_session(session_id)
        assert detail["meta"]["stage"] == "transcribed"
        assert detail["transcript"] is not None

    def test_the_audio_is_deleted_once_it_has_been_written_down(self, service):
        session_id = record_a_meeting(service)
        service.process(session_id)
        directory = mpaths.session_dir(session_id)
        assert not list(directory.glob("*.wav"))

    def test_the_audio_is_kept_when_asked_for(self, service):
        service.config.update({"MINUTES_KEEP_AUDIO_DAYS": 7})
        session_id = record_a_meeting(service)
        service.process(session_id)
        directory = mpaths.session_dir(session_id)
        assert list(directory.glob("*.wav"))

    def test_processing_twice_does_not_transcribe_twice(self, service):
        session_id = record_a_meeting(service)
        service.process(session_id)
        first = service.get_session(session_id)["text"]
        service.process(session_id)
        assert service.get_session(session_id)["text"] == first

    def test_an_unknown_session_is_refused(self, service):
        ok, error = service.process("20200101-000000-deadbeef")
        assert not ok and "No such recording" in error

    def test_a_session_with_no_meta_is_refused(self, service):
        directory = mpaths.SESSIONS_DIR / "20260101-090000-abcdef12"
        directory.mkdir(parents=True)
        ok, error = service.process(directory.name)
        assert not ok and "meta" in error

    def test_the_recorder_notices_reach_the_reader(self, service):
        session_id = record_a_meeting(service)
        service.process(session_id)
        notices = service.get_session(session_id)["transcript"]["notices"]
        assert notices, "the transcript should say the recording was a mock one"


class TestPriorSummaries:
    def _write_session(self, service, session_id, title, summary_text):
        directory = mpaths.SESSIONS_DIR / session_id
        directory.mkdir(parents=True, exist_ok=True)
        meta = SessionMeta(
            session_id=session_id,
            title=title,
            started_at="2026-08-21T09:00:00+00:00",
            stage="summarised",
        )
        (directory / "meta.json").write_text(json.dumps(meta.to_dict()))
        (directory / "summary.json").write_text(
            json.dumps({"text": summary_text, "ok": True, "model": "test"})
        )

    def test_an_earlier_meeting_of_the_same_series_is_found(self, service):
        """The regression: the search used to stop at the newest session, which
        is always the one being written up."""
        self._write_session(service, "20260821-090000-aaaaaaaa", "Standup", "Last week we agreed X.")
        meta = SessionMeta(session_id="20260828-090000-bbbbbbbb", title="Standup")
        prior = service._prior_summaries(meta)
        assert len(prior) == 1
        assert prior[0]["summary"] == "Last week we agreed X."

    def test_the_date_is_readable_rather_than_a_timestamp(self, service):
        self._write_session(service, "20260821-090000-aaaaaaaa", "Standup", "text")
        meta = SessionMeta(session_id="20260828-090000-bbbbbbbb", title="Standup")
        assert service._prior_summaries(meta)[0]["date"] == "21 August 2026"

    def test_a_different_meeting_is_not_included(self, service):
        self._write_session(service, "20260821-090000-aaaaaaaa", "Retro", "text")
        meta = SessionMeta(session_id="20260828-090000-bbbbbbbb", title="Standup")
        assert service._prior_summaries(meta) == []

    def test_the_limit_is_respected(self, service):
        service.config.update({"MINUTES_SUMMARY_CONTEXT_MEETINGS": 1})
        for index, stamp in enumerate(("20260814-090000-aaaaaaaa", "20260821-090000-bbbbbbbb")):
            self._write_session(service, stamp, "Standup", f"summary {index}")
        meta = SessionMeta(session_id="20260828-090000-cccccccc", title="Standup")
        assert len(service._prior_summaries(meta)) == 1

    def test_zero_turns_it_off(self, service):
        service.config.update({"MINUTES_SUMMARY_CONTEXT_MEETINGS": 0})
        self._write_session(service, "20260821-090000-aaaaaaaa", "Standup", "text")
        meta = SessionMeta(session_id="20260828-090000-bbbbbbbb", title="Standup")
        assert service._prior_summaries(meta) == []


class TestCorrectingASpeaker:
    def _transcribed(self, service):
        session_id = record_a_meeting(service)
        service.process(session_id)
        return session_id

    def test_a_label_can_be_given_a_name(self, service):
        session_id = self._transcribed(service)
        charlie, _ = service.people.add("Charlie", "charlie@example.com")
        ok, message = service.relabel(session_id, "Room speaker", charlie.id)
        assert ok, message
        assert "Charlie" in service.get_session(session_id)["speakers"]

    def test_the_corrected_person_joins_the_participants(self, service):
        session_id = self._transcribed(service)
        charlie, _ = service.people.add("Charlie", "charlie@example.com")
        service.relabel(session_id, "Room speaker", charlie.id)
        detail = service.get_session(session_id)
        assert "charlie@example.com" in detail["recipients"]

    def test_a_correction_outranks_everything_else(self, service):
        session_id = self._transcribed(service)
        charlie, _ = service.people.add("Charlie")
        service.relabel(session_id, "Room speaker", charlie.id)
        segments = service.get_session(session_id)["transcript"]["segments"]
        assert all(s["source"] == SOURCE_MANUAL for s in segments if s["speaker"] == "Charlie")

    def test_one_line_is_said_in_the_singular(self, service):
        session_id = self._transcribed(service)
        charlie, _ = service.people.add("Charlie")
        ok, message = service.relabel(session_id, "Room speaker", charlie.id)
        assert ok
        assert "1 line is" in message or "lines are" in message
        assert "1 lines" not in message

    def test_an_unknown_label_is_refused(self, service):
        session_id = self._transcribed(service)
        charlie, _ = service.people.add("Charlie")
        ok, message = service.relabel(session_id, "Nobody", charlie.id)
        assert not ok and "Nobody" in message

    def test_an_unknown_person_is_refused(self, service):
        session_id = self._transcribed(service)
        ok, message = service.relabel(session_id, "Room speaker", "ffffffffffff")
        assert not ok and "No such person" in message


class TestTheCameraStandsAside:
    def test_it_waits_when_a_meeting_is_due_soon(self, service):
        service.config.update({"MINUTES_IDENTIFY_FACES": True})
        service.calendar = FakeCalendar([FakeMeeting(minutes_away=1.0)])
        looked = []
        service.look_at_room_now = lambda: looked.append(1)
        service._maybe_look_at_room()
        assert looked == [], "a sweep must not be holding the camera when a meeting joins"

    def test_it_looks_when_the_next_meeting_is_hours_away(self, service):
        service.config.update({"MINUTES_IDENTIFY_FACES": True})
        service.calendar = FakeCalendar([FakeMeeting(minutes_away=180.0)])
        looked = []
        service.look_at_room_now = lambda: looked.append(1)
        service._maybe_look_at_room()
        assert looked == [1]

    def test_it_waits_for_the_browser_to_let_go_after_a_meeting(self, service):
        service.config.update({"MINUTES_IDENTIFY_FACES": True})
        service._meeting_ended_at = datetime.now(timezone.utc)
        looked = []
        service.look_at_room_now = lambda: looked.append(1)
        service._maybe_look_at_room()
        assert looked == []

    def test_it_does_nothing_at_all_when_switched_off(self, service):
        looked = []
        service.look_at_room_now = lambda: looked.append(1)
        service._maybe_look_at_room()
        assert looked == []


class TestRetention:
    def _old_session(self, service, days):
        stamp = datetime.now(timezone.utc) - timedelta(days=days)
        session_id = f"{stamp.strftime('%Y%m%d-%H%M%S')}-abcdef12"
        directory = mpaths.SESSIONS_DIR / session_id
        directory.mkdir(parents=True, exist_ok=True)
        meta = SessionMeta(session_id=session_id, title="Old", stage="summarised")
        (directory / "meta.json").write_text(json.dumps(meta.to_dict()))
        return session_id

    def test_an_expired_meeting_is_deleted(self, service):
        service.config.update({"MINUTES_KEEP_DAYS": 30})
        session_id = self._old_session(service, 40)
        assert service.sweep() == 1
        assert mpaths.session_dir(session_id).exists() is False

    def test_a_recent_meeting_is_kept(self, service):
        service.config.update({"MINUTES_KEEP_DAYS": 30})
        session_id = self._old_session(service, 5)
        service.sweep()
        assert mpaths.session_dir(session_id).exists()

    def test_deleting_by_hand_works(self, service):
        session_id = record_a_meeting(service)
        assert service.delete_session(session_id) is True
        assert service.get_session(session_id) is None

    def test_deleting_something_that_is_not_there_is_not_an_error(self, service):
        assert service.delete_session("20200101-000000-deadbeef") is False


class TestRecoveringFromARestart:
    def test_an_interrupted_recording_is_picked_up(self, service):
        session_id = "20260828-090000-abcdef12"
        directory = mpaths.SESSIONS_DIR / session_id
        directory.mkdir(parents=True, exist_ok=True)
        meta = SessionMeta(session_id=session_id, title="Interrupted", stage=STAGE_RECORDING)
        (directory / "meta.json").write_text(json.dumps(meta.to_dict()))

        service._recover_unfinished()
        assert service._queue.qsize() == 1
        recovered = service._read_meta(directory)
        assert recovered.stage == STAGE_CAPTURED
        assert "restarted" in recovered.error

    def test_a_finished_session_is_left_alone(self, service):
        session_id = record_a_meeting(service)
        service.process(session_id)
        while not service._queue.empty():
            service._queue.get()
        service._recover_unfinished()
        assert service._queue.qsize() == 0


class TestStatus:
    def test_it_explains_every_moving_part(self, service):
        status = service.status()
        assert set(status["capabilities"]) == {
            "audio", "transcribe", "roster", "faces", "voices", "summary", "email",
        }
        for name, capability in status["capabilities"].items():
            assert isinstance(capability["ok"], bool)
            if not capability["ok"]:
                assert capability["detail"], f"{name} is off with no explanation"

    def test_it_counts_what_is_on_disk(self, service):
        record_a_meeting(service)
        status = service.status()
        assert status["sessions"] == 1
        assert status["people"]["people"] == 0
