"""Screen sharing from a PC: the session state machine, signalling and access.

The interesting cases here are the unhappy ones. A laptop that walks out of the
room mid-share, a room screen that reloads, two people pressing Share at once,
a meeting starting on top of it — every one of those has to leave the room
showing something sensible, because the alternative is a TV stuck on a frozen
screen with nobody in the building who knows how to fix it.
"""

from __future__ import annotations

import shutil

import pytest

from app.cast_service import (
    CONNECT_TIMEOUT_SECONDS,
    SENDER_TIMEOUT_SECONDS,
    SESSION_TIMEOUT_SECONDS,
    CastService,
)
from app.models import FAIL, OFF, OK, UNKNOWN

OFFER = {"type": "offer", "sdp": "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n"}
ANSWER = {"type": "answer", "sdp": "v=0\r\no=- 2 2 IN IP4 127.0.0.1\r\n"}
CANDIDATE = {"candidate": "candidate:1 1 udp 2122 192.168.1.9 5000 typ host", "sdpMLineIndex": 0}


@pytest.fixture()
def cast(mock_config):
    mock_config.update({"CAST_ENABLED": True})
    return CastService(mock_config)


@pytest.fixture()
def clock(monkeypatch):
    """Move the service's clocks forward without sleeping through it.

    The cast service reads time through two seams (``_elapsed`` for timeouts,
    ``_now`` for timestamps), so a test can age a session by an hour instantly
    and nothing outside the service is affected.
    """
    from datetime import timedelta

    from app import cast_service

    real_elapsed = cast_service._elapsed
    real_now = cast_service._now
    offset = {"seconds": 0.0}

    monkeypatch.setattr(
        cast_service, "_elapsed", lambda: real_elapsed() + offset["seconds"]
    )
    monkeypatch.setattr(
        cast_service, "_now", lambda: real_now() + timedelta(seconds=offset["seconds"])
    )

    def advance(seconds: float, *, service=None) -> None:
        """Age everything by ``seconds``.

        Pass ``service`` to keep its sender's heartbeat current, so a test about
        one timeout is not silently testing a different one.
        """
        offset["seconds"] += seconds
        session = getattr(service, "_session", None) if service else None
        if session is not None:
            session.touch_sender()

    return advance


def connect(cast: CastService) -> str:
    """Run a full handshake and return the session id."""
    session = cast.start_session(client="Windows PC (Chrome)")["session"]
    assert cast.submit_offer(session, OFFER)
    assert cast.poll_receiver()["messages"][0]["type"] == "offer"
    assert cast.submit_answer(session, ANSWER)
    return session


# ---------------------------------------------------------------------------
# The handshake
# ---------------------------------------------------------------------------


class TestHandshake:
    def test_a_new_session_is_not_sharing_yet(self, cast):
        """Pressing Share must not blank the dashboard before anything arrives."""
        cast.start_session(client="Windows PC")
        assert cast.pending is True
        assert cast.sharing is False

    def test_the_offer_reaches_the_room_screen(self, cast):
        session = cast.start_session()["session"]
        cast.submit_offer(session, OFFER)

        waiting = cast.poll_receiver()
        assert waiting["session"] == session
        assert waiting["messages"] == [{"type": "offer", "sdp": OFFER}]

    def test_a_message_is_only_delivered_once(self, cast):
        session = cast.start_session()["session"]
        cast.submit_offer(session, OFFER)

        assert len(cast.poll_receiver()["messages"]) == 1
        assert cast.poll_receiver()["messages"] == []

    def test_the_answer_starts_the_share(self, cast):
        seen: list[bool] = []
        cast.on_change(seen.append)

        session = connect(cast)

        assert cast.sharing is True
        assert cast.pending is False
        assert seen == [True]
        assert cast.poll_sender(session)["messages"][0]["type"] == "answer"

    def test_candidates_go_to_the_other_side_only(self, cast):
        session = connect(cast)
        cast.poll_sender(session)  # drain the answer

        cast.submit_candidate(session, CANDIDATE, from_sender=True)
        assert cast.poll_sender(session)["messages"] == []
        assert cast.poll_receiver()["messages"] == [
            {"type": "candidate", "candidate": CANDIDATE}
        ]

        cast.submit_candidate(session, CANDIDATE, from_sender=False)
        assert cast.poll_receiver()["messages"] == []
        assert cast.poll_sender(session)["messages"] == [
            {"type": "candidate", "candidate": CANDIDATE}
        ]

    def test_the_sender_learns_the_room_screen_is_listening(self, cast):
        """The sender page says "waiting for the room" only while that is true."""
        session = cast.start_session()["session"]
        cast.submit_offer(session, OFFER)
        assert cast.poll_sender(session)["receiver_ready"] is False

        cast.poll_receiver()
        assert cast.poll_sender(session)["receiver_ready"] is True

    def test_a_stale_session_id_is_refused(self, cast):
        """A laptop replaying yesterday's id must not reach the room's screen."""
        session = connect(cast)
        cast.stop_session(session)

        assert cast.poll_sender(session) is None
        assert cast.submit_offer(session, OFFER) is False
        assert cast.submit_answer(session, ANSWER) is False
        assert cast.submit_candidate(session, CANDIDATE, from_sender=True) is False


# ---------------------------------------------------------------------------
# Ending, one way or another
# ---------------------------------------------------------------------------


class TestEnding:
    def test_stopping_ends_the_share(self, cast):
        seen: list[bool] = []
        session = connect(cast)
        cast.on_change(seen.append)

        assert cast.stop_session(session) is True
        assert cast.sharing is False
        assert seen == [False]

    def test_a_meeting_takes_the_screen_back(self, cast):
        session = connect(cast)
        assert cast.end_current(reason="a meeting is starting") is True
        assert cast.sharing is False
        assert cast.poll_sender(session) is None
        # And the laptop can be told why, rather than just losing the picture.
        assert cast.why_ended(session) == "a meeting is starting"

    def test_the_reason_is_only_handed_out_once(self, cast):
        session = connect(cast)
        cast.stop_session(session, reason="you stopped sharing")

        assert cast.why_ended(session) == "you stopped sharing"
        assert cast.why_ended(session) == ""

    def test_a_displaced_sender_is_told_it_was_displaced(self, cast):
        first = connect(cast)
        cast.start_session(client="Another PC")

        assert "somebody else" in cast.why_ended(first)

    def test_an_expired_session_records_why(self, cast, clock):
        session = connect(cast)
        clock(SENDER_TIMEOUT_SECONDS + 1)
        assert cast.sharing is False

        assert "stopped responding" in cast.why_ended(session)

    def test_the_record_of_endings_does_not_grow_without_limit(self, cast):
        """A room left running for months must not accumulate these."""
        for _ in range(50):
            session = cast.start_session()["session"]
            cast.stop_session(session)

        assert len(cast._ended) <= 8

    def test_ending_nothing_is_harmless(self, cast):
        """Called on every join, so it must be free when nobody is sharing."""
        assert cast.end_current(reason="a meeting is starting") is False

    def test_a_silent_sender_expires(self, cast, clock):
        """A closed laptop must not leave the TV showing a frozen screen."""
        connect(cast)
        assert cast.sharing is True

        clock(SENDER_TIMEOUT_SECONDS + 1)
        assert cast.sharing is False

    def test_a_share_that_never_connects_expires(self, cast, clock):
        """Someone who opened the picker and wandered off blocks nobody.

        The sender is kept talking throughout, so what runs out here is the
        patience for a handshake and not the sender timeout.
        """
        cast.start_session()
        clock(CONNECT_TIMEOUT_SECONDS + 1, service=cast)

        assert cast.pending is False
        assert "never established" in str(cast.status()["last_error"])

    def test_expiry_tells_the_room_the_share_is_over(self, cast, clock):
        seen: list[bool] = []
        connect(cast)
        cast.on_change(seen.append)

        clock(SENDER_TIMEOUT_SECONDS + 1)
        assert cast.sharing is False
        assert seen == [False]

    def test_a_room_screen_that_stops_collecting_ends_the_share(self, cast, clock):
        """The kiosk died. The laptop is fine, but nothing is on the TV."""
        connect(cast)
        clock(SESSION_TIMEOUT_SECONDS + 1, service=cast)

        assert cast.sharing is False

    def test_a_room_screen_that_cannot_play_hands_the_session_back(self, cast):
        session = connect(cast)
        assert cast.receiver_failed(session, reason="decoder gave up") is True
        assert cast.sharing is False
        assert "decoder gave up" in str(cast.status()["last_error"])


# ---------------------------------------------------------------------------
# Two people, one screen
# ---------------------------------------------------------------------------


class TestTakeover:
    def test_the_second_person_takes_over(self, cast):
        """Refusing would be worse: the first laptop may already have left."""
        first = connect(cast)
        second = cast.start_session(client="Another PC")["session"]

        assert second != first
        assert cast.poll_sender(first) is None
        assert cast.submit_offer(second, OFFER) is True

    def test_taking_over_a_live_share_keeps_the_room_in_sharing_mode(self, cast):
        """The TV is still showing a shared screen, so nothing should flicker."""
        seen: list[bool] = []
        connect(cast)
        cast.on_change(seen.append)

        cast.start_session(client="Another PC")
        assert seen == []


# ---------------------------------------------------------------------------
# A room screen that reloads mid-share
# ---------------------------------------------------------------------------


class TestReconnection:
    def test_a_reloaded_room_screen_asks_for_a_new_offer(self, cast):
        session = connect(cast)
        cast.poll_sender(session)  # drain

        assert cast.request_renegotiation(session) is True
        assert cast.poll_sender(session)["messages"] == [{"type": "renegotiate"}]

    def test_renegotiation_is_not_queued_twice(self, cast):
        """Two requests would have the laptop rebuilding its connection twice."""
        session = connect(cast)
        cast.poll_sender(session)

        cast.request_renegotiation(session)
        cast.request_renegotiation(session)
        assert cast.poll_sender(session)["messages"] == [{"type": "renegotiate"}]

    def test_renegotiation_before_the_first_offer_is_refused(self, cast):
        """Otherwise a well-timed poll makes the sender restart mid-handshake."""
        session = cast.start_session()["session"]
        assert cast.request_renegotiation(session) is False

    def test_the_share_survives_renegotiation(self, cast):
        """The picture goes briefly; the room must not swing back to the dashboard."""
        session = connect(cast)
        cast.request_renegotiation(session)
        assert cast.sharing is True

        cast.submit_offer(session, OFFER)
        assert cast.submit_answer(session, ANSWER) is True
        assert cast.sharing is True


# ---------------------------------------------------------------------------
# The sharing code
# ---------------------------------------------------------------------------


class TestSharingCode:
    def test_no_code_means_anyone_on_the_network(self, cast):
        assert cast.check_pin("") is True
        assert cast.check_pin("whatever") is True

    def test_a_code_is_required_when_set(self, mock_config):
        mock_config.update({"CAST_ENABLED": True, "CAST_PIN": "4821"})
        cast = CastService(mock_config)

        assert cast.check_pin("4821") is True
        assert cast.check_pin("4822") is False
        assert cast.check_pin("") is False

    def test_surrounding_space_is_forgiven(self, mock_config):
        """People paste codes. Rejecting a trailing space teaches nothing."""
        mock_config.update({"CAST_ENABLED": True, "CAST_PIN": "4821"})
        assert CastService(mock_config).check_pin(" 4821 ") is True


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_disabled_reports_off_and_nothing_else(self, mock_config):
        mock_config.update({"CAST_ENABLED": False})
        status = CastService(mock_config).status()

        assert status == {"enabled": False, "status": OFF, "sharing": False}

    def test_without_a_certificate_it_reports_a_fault(self, cast, monkeypatch):
        """Present but unusable is a fault, not "switched off": a browser will
        not share a screen without HTTPS, and the room should say so."""
        monkeypatch.setattr(cast, "tls_ready", lambda: False)
        cast.note_listeners(running=True)
        assert cast.status()["status"] == FAIL

    def test_a_healthy_idle_room_is_ok(self, cast, monkeypatch):
        monkeypatch.setattr(cast, "tls_ready", lambda: True)
        cast.note_listeners(running=True)
        status = cast.status()

        assert status["status"] == OK
        assert status["sharing"] is False
        assert status["pin_required"] is False

    def test_a_port_that_would_not_open_is_a_fault(self, cast, monkeypatch):
        """Otherwise the only symptom is a laptop that never appears on the TV."""
        monkeypatch.setattr(cast, "tls_ready", lambda: True)
        cast.note_listeners(running=False, error="Port 8000 could not be opened")
        status = cast.status()

        assert status["status"] == FAIL
        assert "8000" in str(status["listener_error"])

    def test_a_listener_that_has_not_reported_yet_is_unknown_not_broken(self, cast):
        """The second between the services starting and the listeners opening
        must not flash a red light on the TV at every boot."""
        assert cast.status()["listening"] is None
        assert cast.status()["status"] == UNKNOWN

    def test_sharing_is_reported_with_who(self, cast, monkeypatch):
        monkeypatch.setattr(cast, "tls_ready", lambda: True)
        cast.note_listeners(running=True)
        connect(cast)
        status = cast.status()

        assert status["sharing"] is True
        assert status["client"] == "Windows PC (Chrome)"
        assert status["since"]

    def test_a_hostile_device_name_is_cut_down_to_size(self, cast):
        cast.start_session(client="x" * 500)
        assert len(str(cast.status()["client"])) <= 60


# ---------------------------------------------------------------------------
# The listeners
# ---------------------------------------------------------------------------


@pytest.fixture()
def appliance(mock_config):
    """A wired-up appliance with no background threads running."""
    from app.main import create_app

    mock_config.update({"CAST_ENABLED": True})
    app = create_app(mock_config, start_services=False)
    return app.config["ROOM_APPLIANCE"]


@pytest.fixture()
def sender(appliance):
    """A test client for the HTTPS sender application."""
    from app.cast_web import create_cast_app

    application = create_cast_app(appliance)
    application.config.update(TESTING=True)
    return application.test_client()


class TestSenderListener:
    def test_the_page_offers_the_one_button(self, sender):
        body = sender.get("/").get_data(as_text=True)

        assert "Share this screen" in body
        assert 'id="share-button"' in body

    def test_the_aliases_land_on_the_page(self, sender):
        for path in ("/cast", "/share"):
            assert sender.get(path).status_code == 302

    def test_the_page_never_links_to_the_rooms_settings(self, sender):
        """It is handed to visitors. One tap from Settings is one tap too few."""
        body = sender.get("/").get_data(as_text=True)

        assert "/settings" not in body
        assert "/panel" not in body
        assert "/diagnostics" not in body

    def test_administration_is_not_reachable_on_this_port(self, sender):
        """The whole reason it is a separate application."""
        for path in ("/settings", "/panel", "/diagnostics", "/api/state",
                     "/api/settings", "/api/logs", "/controller"):
            response = sender.get(path)
            # Anything unknown lands back on the sharing page; nothing serves.
            assert response.status_code in (302, 404), path
            if response.status_code == 302:
                assert response.headers["Location"] in ("/", "http://localhost/")

    def test_a_full_handshake_over_http(self, sender, appliance):
        started = sender.post("/api/cast/start", json={"label": "Windows PC"}).get_json()
        assert started["ok"]
        session = started["session"]

        assert sender.post(
            "/api/cast/offer", json={"session": session, "sdp": OFFER}
        ).get_json()["ok"]
        assert appliance.cast.poll_receiver()["messages"][0]["type"] == "offer"

        appliance.cast.submit_answer(session, ANSWER)
        polled = sender.get(f"/api/cast/poll?session={session}").get_json()
        assert polled["ok"]
        assert polled["messages"][0]["type"] == "answer"
        assert appliance.cast.sharing is True

        assert sender.post("/api/cast/stop", json={"session": session}).get_json()["ok"]
        assert appliance.cast.sharing is False

    def test_an_unknown_session_is_told_to_start_again(self, sender):
        response = sender.post("/api/cast/offer", json={"session": "nope", "sdp": OFFER})

        assert response.status_code == 409
        assert response.get_json()["ended"] is True

    def test_the_laptop_is_told_why_the_room_stopped_it(self, sender, appliance):
        """"Press Share again" is useless advice when a meeting has the TV."""
        session = sender.post("/api/cast/start", json={}).get_json()["session"]
        sender.post("/api/cast/offer", json={"session": session, "sdp": OFFER})
        appliance.cast.submit_answer(session, ANSWER)

        appliance.cast.end_current(reason="a meeting is starting in this room")
        response = sender.get(f"/api/cast/poll?session={session}")

        assert response.status_code == 409
        body = response.get_json()
        assert body["ended"] is True
        assert "a meeting is starting in this room" in body["error"]

    def test_rubbish_is_refused_before_it_reaches_the_room(self, sender):
        session = sender.post("/api/cast/start", json={}).get_json()["session"]

        assert sender.post(
            "/api/cast/offer", json={"session": session, "sdp": "not-an-object"}
        ).status_code == 400
        assert sender.post(
            "/api/cast/candidate", json={"session": session, "candidate": []}
        ).status_code == 400

    def test_an_enormous_offer_is_refused(self, sender):
        """A peer cannot be allowed to park megabytes in the room's memory."""
        session = sender.post("/api/cast/start", json={}).get_json()["session"]
        huge = {"type": "offer", "sdp": "v=0\r\n" + "a" * 200_000}

        assert sender.post(
            "/api/cast/offer", json={"session": session, "sdp": huge}
        ).status_code == 400

    def test_the_wrong_code_is_refused(self, appliance, sender):
        appliance.config.update({"CAST_PIN": "4821"})

        refused = sender.post("/api/cast/start", json={"pin": "0000"})
        assert refused.status_code == 403
        assert refused.get_json()["needs_pin"] is True

        assert sender.post("/api/cast/start", json={"pin": "4821"}).get_json()["ok"]

    def test_sharing_is_refused_while_the_tv_is_in_a_meeting(self, appliance, sender):
        """Answered on the laptop, rather than by a screen that never appears."""
        from app.meeting_service import ActiveMeeting
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        appliance.room._state.active = ActiveMeeting(
            meeting_id="m1", title="Standup", provider_id="teams",
            scheduled_end=now + timedelta(minutes=30), opened_at=now,
        )

        response = sender.post("/api/cast/start", json={})
        assert response.status_code == 409
        assert "inside the meeting" in response.get_json()["error"]

    def test_sharing_is_refused_when_switched_off(self, appliance, sender):
        appliance.config.update({"CAST_ENABLED": False})

        assert sender.post("/api/cast/start", json={}).status_code == 409


@pytest.fixture()
def entry(appliance):
    """A test client for the plain-HTTP page whose address goes on the TV."""
    from app.cast_web import create_entry_app

    application = create_entry_app(appliance)
    application.config.update(TESTING=True)
    return application.test_client()


class TestEntryListener:
    def test_it_warns_about_the_certificate_before_the_browser_does(
        self, entry, appliance, monkeypatch
    ):
        monkeypatch.setattr(appliance.cast, "tls_ready", lambda: True)
        body = entry.get("/").get_data(as_text=True)

        assert "Continue to sharing" in body
        assert "Advanced" in body

    def test_it_sends_people_to_the_secure_port_on_the_host_they_typed(
        self, entry, appliance, monkeypatch
    ):
        """Somebody who typed room.local must not be redirected to an address:
        the certificate covers the name they used, not the other one."""
        monkeypatch.setattr(appliance.cast, "tls_ready", lambda: True)
        appliance.config.update({"CAST_SECURE_PORT": 8443})

        body = entry.get("/", headers={"Host": "room.local:8000"}).get_data(as_text=True)
        assert "https://room.local:8443/" in body

    def test_it_says_so_when_there_is_no_certificate(self, entry, appliance, monkeypatch):
        monkeypatch.setattr(appliance.cast, "tls_ready", lambda: False)
        body = entry.get("/").get_data(as_text=True)

        assert "cannot offer screen sharing yet" in body
        # And points at the route that does work today.
        assert "Screen Mirroring" in body

    def test_it_says_so_when_sharing_is_switched_off(self, entry, appliance):
        appliance.config.update({"CAST_ENABLED": False})
        assert "switched off" in entry.get("/").get_data(as_text=True)

    def test_anything_else_lands_on_the_one_page(self, entry):
        assert entry.get("/settings").status_code == 302
        assert entry.get("/api/state").status_code == 302


# ---------------------------------------------------------------------------
# The receiving half, on the dashboard's own port
# ---------------------------------------------------------------------------


@pytest.fixture()
def dashboard(mock_config):
    from app.main import create_app

    mock_config.update({"CAST_ENABLED": True})
    application = create_app(mock_config, start_services=False)
    application.config.update(TESTING=True)
    return application


class TestReceiverEndpoints:
    @staticmethod
    def _token(client) -> str:
        body = client.get("/").get_data(as_text=True)
        marker = 'data-csrf="'
        start = body.index(marker) + len(marker)
        return body[start : body.index('"', start)]

    def test_the_dashboard_page_carries_the_receiver(self, dashboard):
        body = dashboard.test_client().get("/").get_data(as_text=True)

        assert 'id="cast-stage"' in body
        assert "cast_receiver.js" in body
        assert 'data-cast="on"' in body

    def test_the_receiver_is_not_loaded_when_sharing_is_off(self, mock_config):
        from app.main import create_app

        mock_config.update({"CAST_ENABLED": False})
        client = create_app(mock_config, start_services=False).test_client()

        assert 'data-cast="off"' in client.get("/").get_data(as_text=True)

    def test_the_room_screen_can_answer_a_share(self, dashboard):
        client = dashboard.test_client()
        token = self._token(client)
        appliance = dashboard.config["ROOM_APPLIANCE"]

        session = appliance.cast.start_session(client="Windows PC")["session"]
        appliance.cast.submit_offer(session, OFFER)

        polled = client.get("/api/cast/receiver").get_json()
        assert polled["messages"][0]["type"] == "offer"

        answered = client.post(
            "/api/cast/receiver/answer",
            json={"session": session, "sdp": ANSWER},
            headers={"X-Room-Token": token},
        )
        assert answered.get_json()["ok"]
        assert appliance.cast.sharing is True

    def test_answering_needs_the_page_token(self, dashboard):
        """The kiosk has one; a stray page in the same browser does not."""
        client = dashboard.test_client()
        appliance = dashboard.config["ROOM_APPLIANCE"]
        session = appliance.cast.start_session()["session"]

        response = client.post(
            "/api/cast/receiver/answer", json={"session": session, "sdp": ANSWER}
        )
        assert response.status_code == 403
        assert appliance.cast.sharing is False

    def test_the_network_cannot_answer_for_the_room(self, dashboard):
        """Only the kiosk on this machine decides what the TV displays."""
        client = dashboard.test_client()
        token = self._token(client)

        response = client.get(
            "/api/cast/receiver", environ_overrides={"REMOTE_ADDR": "192.168.1.50"}
        )
        assert response.status_code == 403

        response = client.post(
            "/api/cast/receiver/answer",
            json={"session": "x", "sdp": ANSWER},
            headers={"X-Room-Token": token},
            environ_overrides={"REMOTE_ADDR": "192.168.1.50"},
        )
        assert response.status_code == 403

    def test_the_receiver_endpoints_are_shut_when_sharing_is_off(self, mock_config):
        from app.main import create_app

        mock_config.update({"CAST_ENABLED": False})
        application = create_app(mock_config, start_services=False)
        application.config.update(TESTING=True)

        assert application.test_client().get("/api/cast/receiver").status_code == 409

    def test_a_reloaded_page_asks_the_laptop_to_offer_again(self, dashboard):
        client = dashboard.test_client()
        token = self._token(client)
        appliance = dashboard.config["ROOM_APPLIANCE"]

        session = appliance.cast.start_session()["session"]
        appliance.cast.submit_offer(session, OFFER)
        appliance.cast.poll_receiver()
        appliance.cast.submit_answer(session, ANSWER)
        appliance.cast.poll_sender(session)

        response = client.post(
            "/api/cast/receiver/renegotiate",
            json={"session": session},
            headers={"X-Room-Token": token},
        )
        assert response.get_json()["ok"]
        assert appliance.cast.poll_sender(session)["messages"] == [
            {"type": "renegotiate"}
        ]

    def test_a_failure_on_the_tv_frees_the_room(self, dashboard):
        client = dashboard.test_client()
        token = self._token(client)
        appliance = dashboard.config["ROOM_APPLIANCE"]

        session = appliance.cast.start_session()["session"]
        appliance.cast.submit_offer(session, OFFER)
        appliance.cast.submit_answer(session, ANSWER)
        assert appliance.cast.sharing is True

        client.post(
            "/api/cast/receiver/failed",
            json={"session": session, "reason": "no decoder"},
            headers={"X-Room-Token": token},
        )
        assert appliance.cast.sharing is False


# ---------------------------------------------------------------------------
# The state the dashboard renders
# ---------------------------------------------------------------------------


class TestDashboardState:
    def test_the_state_carries_the_sharing_address(self, dashboard):
        payload = dashboard.test_client().get("/api/state").get_json()

        assert "cast" in payload
        assert payload["cast"]["enabled"] is True
        assert "url" in payload["cast"]
        assert payload["cast"]["show_on_tv"] is True

    def test_the_address_is_empty_when_nothing_is_listening(self, dashboard):
        """No listener means no address, so the TV shows no instruction at all."""
        payload = dashboard.test_client().get("/api/state").get_json()
        assert payload["cast"]["url"] == ""

    def test_a_live_share_puts_the_room_in_sharing_mode(self, dashboard):
        appliance = dashboard.config["ROOM_APPLIANCE"]
        session = appliance.cast.start_session(client="Windows PC")["session"]
        appliance.cast.submit_offer(session, OFFER)
        appliance.cast.submit_answer(session, ANSWER)

        assert appliance.room.tick() == "screen-sharing"
        payload = dashboard.test_client().get("/api/state").get_json()
        assert payload["mode"] == "screen-sharing"
        assert payload["cast"]["client"] == "Windows PC"

    def test_the_sharing_address_is_not_a_secret(self, dashboard):
        """Unlike the controller code, it leads to a page with one button, so a
        laptop on the LAN reading /api/state is welcome to it."""
        payload = dashboard.test_client().get(
            "/api/state", environ_overrides={"REMOTE_ADDR": "192.168.1.50"}
        ).get_json()

        assert "cast" in payload
        assert "url" in payload["cast"]


# ---------------------------------------------------------------------------
# The listeners themselves
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """A port nothing is using, so the suite does not fight the machine."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl is not installed")
class TestListeners:
    """These bind real ports, because the bugs worth catching here are the ones
    that only appear when a socket is actually held."""

    @pytest.fixture()
    def server(self, appliance):
        appliance.config.update(
            {"CAST_PORT": _free_port(), "CAST_SECURE_PORT": _free_port()}
        )
        yield appliance.cast_server
        appliance.cast_server.stop()

    def test_both_listeners_come_up(self, server, appliance):
        assert server.start() is True
        assert server.running is True
        assert server.port and server.secure_port
        assert appliance.cast.status()["listening"] is True

    def test_stopping_gives_the_ports_back(self, server):
        """`shutdown()` alone leaves the socket bound; only `server_close()`
        releases it, and without that a settings change leaks a port and the
        next start finds its own port taken."""
        assert server.start() is True
        port = server.port
        server.stop()

        import socket

        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))  # raises if the port was leaked

    def test_it_can_be_restarted_repeatedly(self, server):
        """A room's sharing settings can be changed more than once a lifetime."""
        assert server.start() is True
        for _ in range(3):
            assert server.restart() is True, server.error
            assert server.running is True

    def test_switching_it_off_closes_the_listeners(self, server, appliance):
        assert server.start() is True
        appliance.config.update({"CAST_ENABLED": False})

        assert server.restart() is False
        assert server.running is False
        assert appliance.cast.status() == {
            "enabled": False, "status": OFF, "sharing": False
        }

    def test_a_busy_secure_port_is_reported_not_raised(self, server, appliance):
        """The calendar and the join button must survive a port clash."""
        import socket

        with socket.socket() as held:
            held.bind(("0.0.0.0", appliance.config.int_("CAST_SECURE_PORT")))
            held.listen(1)

            assert server.start() is False
            assert server.running is False
            assert "could not be opened" in server.error
            assert appliance.cast.status()["status"] == FAIL

    def test_a_busy_entry_port_still_leaves_sharing_usable(self, server, appliance):
        """Losing the short address is a nuisance; losing sharing is a fault."""
        import socket

        with socket.socket() as held:
            held.bind(("0.0.0.0", appliance.config.int_("CAST_PORT")))
            held.listen(1)

            assert server.start() is True
            assert server.running is True
            assert server.secure_port
            assert server.port == 0  # nothing worth putting on a TV

    def test_identical_ports_are_refused(self, server, appliance):
        same = _free_port()
        appliance.config.update({"CAST_PORT": same, "CAST_SECURE_PORT": same})

        assert server.start() is False
        assert "must differ" in server.error

    def test_the_certificate_covers_the_rooms_own_names(self, server):
        names = server.certificate_names()

        assert names, "a certificate with no names gives a second browser warning"
        # The hostname's .local form is what Avahi publishes and what a person
        # can actually type.
        assert any(name.endswith(".local") for name in names) or len(names) >= 1


# ---------------------------------------------------------------------------
# The room's own certificate
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl is not installed")
class TestCertificate:
    def test_it_is_written_inside_the_configured_var_directory(self, room_dirs):
        """The paths are resolved per call, not baked in at import time — the
        appliance's var directory is relocatable and the tests move it."""
        from app import tls

        assert tls.cert_file().parent == room_dirs["var"]

    def test_one_is_generated_on_first_run(self, room_dirs):
        from app import tls

        assert tls.certificate_present() is False
        result = tls.ensure_certificate(["192.168.1.9", "room.local"], common_name="Room 4")

        assert result is not None
        assert tls.certificate_present() is True
        assert tls.cert_file().read_text().startswith("-----BEGIN CERTIFICATE-----")

    def test_the_private_key_is_not_world_readable(self, room_dirs):
        from app import tls

        tls.ensure_certificate(["192.168.1.9"])
        assert tls.key_file().stat().st_mode & 0o077 == 0

    def test_it_covers_the_rooms_own_addresses(self, room_dirs):
        from app import tls
        from app.system_service import run

        tls.ensure_certificate(["192.168.1.9", "room.local"])
        text = run(["openssl", "x509", "-text", "-noout", "-in", str(tls.cert_file())]).stdout

        assert "192.168.1.9" in text
        assert "room.local" in text

    def test_it_is_not_regenerated_for_no_reason(self, room_dirs):
        """Every regeneration costs everybody in the building another click."""
        from app import tls

        tls.ensure_certificate(["192.168.1.9"])
        first = tls.cert_file().read_text()

        tls.ensure_certificate(["192.168.1.9"])
        assert tls.cert_file().read_text() == first

    def test_a_new_address_gets_a_new_certificate(self, room_dirs):
        """Otherwise a DHCP lease change quietly turns one warning into two."""
        from app import tls

        tls.ensure_certificate(["192.168.1.9"])
        first = tls.cert_file().read_text()

        assert tls.needs_regeneration(["192.168.1.40"])
        tls.ensure_certificate(["192.168.1.40"])
        assert tls.cert_file().read_text() != first

    def test_the_summary_reads_like_something_a_human_can_act_on(self, room_dirs):
        from app import tls

        tls.ensure_certificate(["192.168.1.9", "room.local"])
        summary = tls.certificate_summary()

        assert summary["present"] is True
        assert summary["names"] == ["192.168.1.9", "room.local"]
        assert summary["expires"]

    def test_a_missing_openssl_is_reported_not_fatal(self, room_dirs, monkeypatch):
        """A room with no openssl still shows the calendar and joins meetings."""
        from app import tls

        monkeypatch.setattr(tls, "which", lambda _binary: "")
        assert tls.ensure_certificate(["192.168.1.9"]) is None
