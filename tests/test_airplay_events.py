"""What the AirPlay supervisor makes of UxPlay's output.

The dashboard steps aside for a screen share because ``scripts/start-airplay.sh``
recognises a handful of lines in UxPlay's log. Getting that set wrong is silent
and miserable: the TV hides the room for a phone that was only browsing the
Screen Mirroring menu, or drops the sharing screen while someone is still
sharing. The lines below are real UxPlay output at its default log level, so a
future change to the matching has to keep agreeing with them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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
