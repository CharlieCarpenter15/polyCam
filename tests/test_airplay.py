"""The contract between the AirPlay supervisor and the appliance.

Two halves, both of which used to get it wrong in ways nobody could see.

*What the supervisor makes of UxPlay's output.* The dashboard steps aside for a
screen share because ``scripts/start-airplay.sh`` recognises a handful of lines
in UxPlay's log. Getting that set wrong is silent and miserable: the TV hides
the room for a phone that was only browsing the Screen Mirroring menu, or drops
the sharing screen while someone is still sharing. The lines used below are real
UxPlay output at its default log level, so a future change to the matching has
to keep agreeing with them.

*What the appliance concludes from it.* UxPlay exits rather than carrying on
when it cannot register with mDNS, so a Pi without avahi-daemon runs a receiver
that dies every few seconds and advertises nothing. Reporting that as healthy is
the worst answer available: the room never appears in Screen Mirroring and the
dashboard swears everything is fine.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import app.airplay_service as airplay_module
from app.airplay_service import CRASH_LOOP_EXITS, AirPlayService
from app.models import FAIL, OK, WARN

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "start-airplay.sh"

#: Printed for every TCP connection UxPlay accepts or closes — which includes
#: the ones an Apple device opens merely to ask what this receiver is. None of
#: these describe a screen share, so none of them may produce an event.
SOCKET_NOISE = [
    "Accepted IPv4 client on socket 7, port 51234",
    "Accepted IPv6 client on socket 9, port 51235",
    "Connection closed on socket 7",
    "Removing connection for socket 7",
    "Max connections reached",
    "Initialized server socket(s)",
    "httpd request received on socket 7, connection 2",
    "Disconnecting on software request",
    # Sent when a client offers AirPlay *video* rather than mirroring, and
    # promptly ignored. Nothing is on screen, so nothing should be reported.
    "ignoring AirPlay video streaming request (use option -hls to activate HLS support)",
    # Start-up chatter that happens to contain some of the same words.
    "UxPlay 1.68: An Open-Source AirPlay mirroring and audio-streaming server.",
    "Local : 192.168.1.10",
    "Remote: 192.168.1.44",
]

#: line -> the event the supervisor should derive from it.
SESSION_LINES = [
    (
        "connection request from Charlie's iPhone (iPhone14,2) with deviceID = AA:BB:CC:DD:EE:FF",
        "client Charlie's iPhone",
    ),
    (
        "connection request from MacBook Pro (MacBookPro18,3) with deviceID = 11:22:33:44:55:66",
        "client MacBook Pro",
    ),
    ("raop_rtp_mirror starting mirroring", "connected"),
    ("raop_rtp_mirror->running is no longer true", "disconnected"),
    ("*** ERROR lost connection with client (network problem?)", "disconnected"),
    # -nohold names the new device by IP address, and the mirroring line that
    # follows reports the session properly. Reporting this one would replace a
    # readable device name with "192.168.1.44".
    ('*****"nohold" feature: switch to new connection request from 192.168.1.44', ""),
]


def classify(line: str) -> str:
    """Ask the supervisor's matcher what it makes of one line."""
    result = subprocess.run(
        ["bash", "-c", f'. "{SCRIPT}"; airplay_event_for_line "$1"', "--", line],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "ROOM_AIRPLAY_MATCH_ONLY": "1"},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def is_failure(line: str) -> bool:
    """Would the supervisor keep this line as the reason UxPlay exited?"""
    result = subprocess.run(
        ["bash", "-c", f'. "{SCRIPT}"; airplay_line_is_failure "$1"', "--", line],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "ROOM_AIRPLAY_MATCH_ONLY": "1"},
    )
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


class TestEventMatching:
    @pytest.mark.parametrize("line", SOCKET_NOISE)
    def test_connection_churn_is_not_a_screen_share(self, line):
        assert classify(line) == "", (
            f"{line!r} would move the TV in or out of screen-sharing mode, but it "
            "describes a socket rather than a session"
        )

    @pytest.mark.parametrize("line,expected", SESSION_LINES, ids=lambda v: v[:40])
    def test_session_lines_are_recognised(self, line, expected):
        assert classify(line) == expected

    def test_a_whole_session_reads_in_order(self):
        """The full sequence UxPlay prints for one screen share."""
        session = [
            "Accepted IPv4 client on socket 7, port 51234",
            "connection request from Charlie's iPhone (iPhone14,2) with deviceID = AA:BB",
            "Accepted IPv4 client on socket 9, port 51236",
            # A probe socket closing mid-handshake used to end the session here.
            "Connection closed on socket 7",
            "raop_rtp_mirror starting mirroring",
            "Connection closed on socket 9",
            "raop_rtp_mirror->running is no longer true",
        ]
        assert [classify(line) for line in session] == [
            "",
            "client Charlie's iPhone",
            "",
            "",
            "connected",
            "",
            "disconnected",
        ]

    def test_the_device_name_survives_a_space(self):
        """"Charlie's iPhone", not "Charlie's"."""
        line = "connection request from Meeting Room Mac (Macmini9,1) with deviceID = AA:BB"
        assert classify(line) == "client Meeting Room Mac"


class TestFailureMatching:
    """UxPlay says why it is giving up on the line before it exits."""

    @pytest.mark.parametrize(
        "line",
        [
            # avahi-daemon not running: the receiver cannot advertise itself, so
            # the room never appears in Screen Mirroring however healthy it looks.
            "Could not initialize dnssd library!: error -65537",
            "dnssd_register_raop failed with error code -65537",
            "dnssd_register_airplay failed with error code -65537",
            "GStreamer error: cannot open display",
            "failed to start video renderer",
        ],
    )
    def test_a_reason_to_report_is_recognised(self, line):
        assert is_failure(line), f"{line!r} is why UxPlay died and would be dropped"

    @pytest.mark.parametrize(
        "line",
        [
            "raop_rtp_mirror starting mirroring",
            "Accepted IPv4 client on socket 7, port 51234",
            "Connection closed on socket 7",
            "UxPlay 1.68: An Open-Source AirPlay mirroring and audio-streaming server.",
            "using network ports UDP 7011 6001 6000 TCP 7100 7000 7001",
            # A client leaving is ordinary and already reported as a session
            # event; recording it would bury the reason UxPlay actually died.
            "*** ERROR lost connection with client (network problem?)",
        ],
    )
    def test_ordinary_output_is_not_a_reason(self, line):
        assert not is_failure(line)


class TestMatchOnlyGuard:
    def test_sourcing_for_tests_starts_no_receiver(self):
        """The guard must stop before the script would launch anything."""
        result = subprocess.run(
            ["bash", "-c", f'. "{SCRIPT}"; echo reached-the-end'],
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "ROOM_AIRPLAY_MATCH_ONLY": "1"},
        )
        assert result.returncode == 0, result.stderr
        assert "reached-the-end" in result.stdout
        assert "airplay.starting" not in result.stderr


# --------------------------------------------------------------------------
# What the appliance concludes
# --------------------------------------------------------------------------

#: What UxPlay prints when avahi-daemon is not running, just before it exits.
NO_AVAHI = "Could not initialize dnssd library!: error -65537"


class FakeSystem:
    """systemd's view: the supervisor unit is up and being restarted happily."""

    def __init__(self, unit_state="active"):
        self._unit_state = unit_state
        self.restarts = []

    def unit_state(self, unit):
        return self._unit_state

    def restart(self, unit, *, reason="", min_interval=None):
        self.restarts.append(reason)
        return True


@pytest.fixture()
def airplay(mock_config, monkeypatch):
    """A real receiver on a machine that has uxplay installed."""
    changed, errors = mock_config.update({"DEV_MODE": False, "AIRPLAY_ENABLED": True})
    assert not errors, errors
    monkeypatch.setattr(airplay_module, "which", lambda name: "/usr/bin/uxplay")
    return AirPlayService(mock_config, FakeSystem())


def crash_loop(service, times=CRASH_LOOP_EXITS, reason=NO_AVAHI):
    """Drive the events a supervisor sends while UxPlay refuses to stay up."""
    for _ in range(times):
        service.handle_event("started", running=True)
        service.handle_event("exited", detail=reason)


class TestReceiverHealth:
    def test_a_running_receiver_is_ok(self, airplay):
        airplay.handle_event("started", running=True)
        status = airplay.status()
        assert status["status"] == OK
        assert status["detail"] == ""

    def test_a_receiver_that_will_not_stay_up_is_a_failure(self, airplay):
        """The unit is active and the supervisor is working. Nothing is advertised."""
        crash_loop(airplay)
        status = airplay.status()
        assert status["status"] == FAIL
        assert status["crash_looping"] is True

    def test_the_failure_says_why(self, airplay):
        """Otherwise this is a hunt across the network for a fault on this Pi."""
        crash_loop(airplay)
        assert NO_AVAHI in airplay.status()["detail"]

    def test_a_heartbeat_does_not_vouch_for_a_dead_receiver(self, airplay):
        """The old bug: the supervisor's own pulse was read as UxPlay's."""
        airplay.handle_event("started", running=True)
        airplay.handle_event("exited", detail=NO_AVAHI)
        airplay.handle_event("heartbeat", running=False)
        status = airplay.status()
        assert status["uxplay_running"] is False
        assert status["status"] != OK

    def test_a_heartbeat_can_vouch_for_a_live_one(self, airplay):
        """A backend that restarted mid-session must not have to wait for a crash."""
        airplay.handle_event("heartbeat", running=True)
        assert airplay.status()["status"] == OK

    def test_a_heartbeat_from_an_older_supervisor_changes_nothing(self, airplay):
        """No `running` field: leave the last thing actually observed standing."""
        airplay.handle_event("started", running=True)
        airplay.handle_event("heartbeat")
        assert airplay.status()["uxplay_running"] is True

    def test_one_exit_is_not_a_crash_loop(self, airplay):
        """UxPlay is restarted for ordinary reasons too; do not cry wolf."""
        airplay.handle_event("started", running=True)
        airplay.handle_event("exited", detail="")
        assert airplay.status()["status"] == WARN

    def test_a_deliberate_restart_forgets_the_crash_history(self, airplay):
        """The new supervisor gets to speak for itself."""
        crash_loop(airplay)
        assert airplay.status()["crash_looping"] is True
        airplay.restart(reason="test")
        airplay.handle_event("started", running=True)
        assert airplay.status()["crash_looping"] is False
        assert airplay.status()["status"] == OK

    def test_a_missing_uxplay_still_reports_itself(self, mock_config, monkeypatch):
        changed, errors = mock_config.update({"DEV_MODE": False, "AIRPLAY_ENABLED": True})
        assert not errors, errors
        monkeypatch.setattr(airplay_module, "which", lambda name: None)
        service = AirPlayService(mock_config, FakeSystem())
        status = service.status()
        assert status["status"] == FAIL
        assert "not installed" in status["detail"]

    def test_a_dying_receiver_ends_the_screen_share(self, airplay):
        """The TV must not sit on a sharing screen that no longer has a source."""
        airplay.handle_event("started", running=True)
        airplay.handle_event("connected", client="Charlie's iPhone")
        assert airplay.sharing is True
        airplay.handle_event("exited", detail=NO_AVAHI)
        assert airplay.sharing is False


class TestInternalEvent:
    def test_the_reason_travels_over_the_internal_api(self, client, mock_config, monkeypatch):
        """The supervisor is a separate process; the detail has to survive the trip."""
        from app.web_security import internal_token

        changed, errors = mock_config.update({"DEV_MODE": False})
        assert not errors, errors
        monkeypatch.setattr(airplay_module, "which", lambda name: "/usr/bin/uxplay")

        headers = {"X-Room-Internal-Token": internal_token()}
        for _ in range(CRASH_LOOP_EXITS):
            client.post(
                "/api/internal/airplay",
                json={"event": "started", "running": True},
                headers=headers,
            )
            client.post(
                "/api/internal/airplay",
                json={"event": "exited", "detail": NO_AVAHI, "running": False},
                headers=headers,
            )

        health = client.get("/api/health").get_json()
        assert NO_AVAHI in health["airplay"]["detail"]
