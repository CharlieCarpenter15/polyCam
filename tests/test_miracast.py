"""Miracast: the Windows Win+K receiver.

Two things here are worth more than the rest.

The first is that "the receiver is ready" and "somebody is mirroring" are
different events. The sink implementations announce readiness in words that read
like a connection ("the display is ready", "groupStarted"), and if the room
believes them it hides the dashboard and shows nothing — which looks precisely
like a broken appliance. So the supervisor's line matching is tested directly.

The second is that Miracast fails for hardware reasons the room cannot fix: the
Wi-Fi radio is on the network, or the driver will not be a Wi-Fi Direct group
owner. A room that reports "off" in that situation is lying, and somebody will
spend an afternoon on it. It has to report a fault and say which one.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.miracast_service import (
    HEARTBEAT_TIMEOUT_SECONDS,
    SESSION_MAX_SILENCE_SECONDS,
    MiracastService,
)
from app.models import FAIL, OFF, OK, WARN

ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR = ROOT / "scripts" / "start-miracast.sh"


@pytest.fixture()
def miracast(mock_config):
    """A receiver with Miracast on. DEV_MODE is off so status() is the real one."""
    from app.system_service import SystemService

    mock_config.update({"MIRACAST_ENABLED": True, "DEV_MODE": False})
    return MiracastService(mock_config, SystemService(mock_config))


@pytest.fixture()
def installed(miracast, monkeypatch):
    """A receiver whose backend is present, so status() gets past that gate."""
    monkeypatch.setattr(miracast, "installed_backend", lambda: "miraclecast")
    return miracast


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class TestSessions:
    def test_a_started_receiver_is_not_sharing(self, miracast):
        """The sink comes up and waits. That is not somebody mirroring."""
        miracast.handle_event("started")
        assert miracast.sharing is False

    def test_a_connection_starts_sharing(self, miracast):
        seen: list[bool] = []
        miracast.on_change(seen.append)

        miracast.handle_event("connected", client="DESKTOP-4F2K")

        assert miracast.sharing is True
        assert seen == [True]
        assert miracast.status()["client"] == "DESKTOP-4F2K"

    def test_a_second_connection_does_not_re_announce(self, miracast):
        """Sinks repeat themselves; the room must not flap the dashboard."""
        seen: list[bool] = []
        miracast.on_change(seen.append)

        miracast.handle_event("connected", client="DESKTOP-4F2K")
        miracast.handle_event("connected", client="DESKTOP-4F2K")

        assert seen == [True]

    def test_disconnecting_ends_sharing(self, miracast):
        seen: list[bool] = []
        miracast.handle_event("connected", client="DESKTOP-4F2K")
        miracast.on_change(seen.append)

        miracast.handle_event("disconnected")

        assert miracast.sharing is False
        assert seen == [False]
        assert miracast.status()["client"] == ""

    def test_the_sink_dying_ends_sharing(self, miracast):
        miracast.handle_event("connected", client="DESKTOP-4F2K")
        miracast.handle_event("stopped")

        assert miracast.sharing is False

    def test_a_restart_ends_sharing_and_is_counted(self, miracast):
        miracast.handle_event("connected", client="DESKTOP-4F2K")
        miracast.handle_event("restarted")

        assert miracast.sharing is False
        assert miracast.status()["restarts"] == 1

    def test_an_unknown_event_changes_nothing(self, miracast):
        """A future sink version saying something new must not break the room."""
        miracast.handle_event("connected", client="DESKTOP-4F2K")
        miracast.handle_event("something-new-in-2027")

        assert miracast.sharing is True

    def test_a_hostile_client_name_is_cut_down(self, miracast):
        miracast.handle_event("connected", client="x" * 400)
        assert len(str(miracast.status()["client"])) <= 60

    def test_a_listener_that_throws_does_not_break_sharing(self, miracast):
        def explode(_sharing):
            raise RuntimeError("no")

        miracast.on_change(explode)
        miracast.handle_event("connected", client="DESKTOP-4F2K")

        assert miracast.sharing is True


class TestExpiry:
    def test_a_session_the_supervisor_stopped_reporting_expires(self, miracast, monkeypatch):
        """The dashboard can never be stuck behind a screen nobody is sending."""
        import time as time_module

        from app import miracast_service

        miracast.handle_event("connected", client="DESKTOP-4F2K")
        assert miracast.sharing is True

        # Far enough that both the session limit and the heartbeat have lapsed.
        real = time_module.monotonic
        monkeypatch.setattr(
            miracast_service.time, "monotonic",
            lambda: real() + HEARTBEAT_TIMEOUT_SECONDS + 10,
        )

        from datetime import datetime, timedelta, timezone

        started = datetime.now(timezone.utc) - timedelta(
            seconds=SESSION_MAX_SILENCE_SECONDS + 10
        )
        miracast._session_started = started

        assert miracast.sharing is False

    def test_a_live_session_with_a_fresh_heartbeat_survives(self, miracast):
        """Twenty minutes of a long presentation is not a fault."""
        from datetime import datetime, timedelta, timezone

        miracast.handle_event("connected", client="DESKTOP-4F2K")
        miracast._session_started = datetime.now(timezone.utc) - timedelta(
            seconds=SESSION_MAX_SILENCE_SECONDS + 10
        )
        miracast.handle_event("heartbeat")  # the supervisor is still there

        assert miracast.sharing is True


# ---------------------------------------------------------------------------
# Saying what is wrong
# ---------------------------------------------------------------------------


class TestStatus:
    def test_disabled_reports_off(self, mock_config):
        from app.system_service import SystemService

        mock_config.update({"MIRACAST_ENABLED": False})
        service = MiracastService(mock_config, SystemService(mock_config))

        assert service.status() == {
            "enabled": False, "status": OFF, "sharing": False, "name": ""
        }

    def test_no_receiver_software_is_a_fault_that_says_what_to_run(self, miracast):
        """Neither backend is packaged, so this is the expected first state and
        the message has to lead somewhere."""
        status = miracast.status()

        assert status["status"] == FAIL
        assert "detect-miracast.sh" in str(status["blocked"])

    def test_a_blocked_radio_is_a_fault_not_silence(self, installed):
        """The failure people actually hit. Reporting "off" would send somebody
        hunting for a setting that is already correct."""
        installed.handle_event(
            "blocked", detail="wlan0 is on the room network and a receiver needs a free radio"
        )
        status = installed.status()

        assert status["status"] == FAIL
        assert "free radio" in str(status["blocked"])

    def test_starting_clears_an_earlier_block(self, installed):
        """A room fixed by plugging in Ethernet must stop complaining."""
        installed.handle_event("blocked", detail="radio busy")
        assert installed.status()["status"] == FAIL

        installed.handle_event("started")
        assert installed.status()["blocked"] == ""

    def test_a_silent_supervisor_is_a_warning(self, installed, monkeypatch):
        import time as time_module

        from app import miracast_service

        installed.handle_event("started")
        monkeypatch.setattr(installed.system, "unit_state", lambda _unit: "active")

        real = time_module.monotonic
        monkeypatch.setattr(
            miracast_service.time, "monotonic",
            lambda: real() + HEARTBEAT_TIMEOUT_SECONDS + 10,
        )
        assert installed.status()["status"] == WARN

    def test_a_healthy_receiver_is_ok(self, installed, monkeypatch):
        monkeypatch.setattr(installed.system, "unit_state", lambda _unit: "active")
        installed.handle_event("started", backend="miraclecast")
        status = installed.status()

        assert status["status"] == OK
        assert status["backend"] == "miraclecast"

    def test_a_dead_unit_is_a_fault(self, installed, monkeypatch):
        monkeypatch.setattr(installed.system, "unit_state", lambda _unit: "failed")
        assert installed.status()["status"] == FAIL

    def test_the_name_falls_back_to_the_room_name(self, miracast):
        assert miracast.status()["name"] == "Test Room"

        miracast.config.update({"MIRACAST_NAME": "Boardroom display"})
        assert miracast.status()["name"] == "Boardroom display"

    def test_development_mode_pretends_it_works(self, mock_config):
        """So the sharing screen can be designed on a laptop."""
        from app.system_service import SystemService

        mock_config.update({"MIRACAST_ENABLED": True, "DEV_MODE": True})
        service = MiracastService(mock_config, SystemService(mock_config))

        assert service.simulate_sharing(True) is True
        assert service.sharing is True
        assert service.status()["status"] == OK
        assert service.status()["mock"] is True

    def test_simulation_is_refused_on_a_real_room(self, miracast):
        assert miracast.simulate_sharing(True) is False
        assert miracast.sharing is False


class TestBackendChoice:
    def test_auto_prefers_miraclecast_when_present(self, miracast, monkeypatch):
        from app import miracast_service

        monkeypatch.setattr(
            miracast_service, "which", lambda name: "/usr/bin/x" if name == "miracle-sinkctl" else ""
        )
        assert miracast.installed_backend() == "miraclecast"

    def test_auto_falls_back_to_lazycast(self, miracast, monkeypatch, tmp_path):
        from app import miracast_service

        monkeypatch.setattr(miracast_service, "which", lambda _name: "")
        (tmp_path / "all.sh").write_text("#!/bin/sh\n")
        miracast.config.update({"MIRACAST_LAZYCAST_DIR": str(tmp_path)})

        assert miracast.installed_backend() == "lazycast"

    def test_nothing_installed_reports_nothing(self, miracast, monkeypatch):
        from app import miracast_service

        monkeypatch.setattr(miracast_service, "which", lambda _name: "")
        miracast.config.update({"MIRACAST_LAZYCAST_DIR": "/nowhere-at-all"})

        assert miracast.installed_backend() == ""

    def test_an_explicit_backend_is_not_silently_substituted(self, miracast, monkeypatch):
        """Choosing lazycast and getting MiracleCast would be a confusing lie."""
        from app import miracast_service

        monkeypatch.setattr(
            miracast_service, "which", lambda name: "/usr/bin/x" if name == "miracle-sinkctl" else ""
        )
        miracast.config.update(
            {"MIRACAST_BACKEND": "lazycast", "MIRACAST_LAZYCAST_DIR": "/nowhere-at-all"}
        )
        assert miracast.installed_backend() == ""


# ---------------------------------------------------------------------------
# The supervisor's line matching
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not shutil.which("bash"), reason="bash is not available")
class TestSupervisorLineMatching:
    """Runs ``handle_line`` from the supervisor against real sink output.

    This is where the expensive mistake lives. Both sinks announce readiness in
    words that read like a connection, and a room that believes them hides the
    dashboard and shows a blank screen.
    """

    @staticmethod
    def classify(line: str) -> str:
        """What the supervisor would report for one line of sink output."""
        script = SUPERVISOR.read_text(encoding="utf-8")
        start = script.index("handle_line() {")
        end = script.index("\n}\n", start) + 3
        harness = (
            "report() { printf '%s\\n' \"$1\"; }\n"
            + script[start:end]
            + '\nhandle_line "$1"\n'
        )
        result = subprocess.run(
            ["bash", "-c", harness, "bash", line],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    @pytest.mark.parametrize(
        "line",
        [
            "The display is ready. Name: Meeting Room",   # lazycast, waiting
            "groupStarted: {'interface_object': ...}",     # wpa_supplicant, group up
            "[ADD] Link: 3",                               # miraclecast, card found
        ],
    )
    def test_readiness_is_not_mistaken_for_a_connection(self, line):
        """The bug this class exists for: a Wi-Fi Direct group owner creates its
        group before anybody joins it, so these mean "waiting", not "sharing"."""
        assert self.classify(line) == "started"

    @pytest.mark.parametrize(
        "line",
        [
            "[CONNECTED] Peer: 4a:2b:1c:00:11:22",
            "peer connected: DESKTOP-4F2K",
            "StaAuthorized 4a:2b:1c:00:11:22",
            "RTSP connection established",
        ],
    )
    def test_a_real_connection_is_reported(self, line):
        assert self.classify(line) == "connected"

    @pytest.mark.parametrize(
        "line",
        [
            "[DISCONNECTED] Peer: 4a:2b:1c:00:11:22",
            "peer disconnected",
            "groupFinished",
            "RTSP teardown received",
        ],
    )
    def test_a_disconnection_is_reported(self, line):
        assert self.classify(line) == "disconnected"

    @pytest.mark.parametrize(
        "line",
        [
            "wlan0: Device or resource busy",
            "interface does not support P2P",
        ],
    )
    def test_a_hardware_refusal_becomes_a_blocked_event(self, line):
        """So the dashboard can explain it instead of the room looking absent."""
        assert self.classify(line) == "blocked"

    @pytest.mark.parametrize(
        "line",
        [
            "gst-launch: setting pipeline to PAUSED",
            "INFO: reading configuration",
            "",
            "wfd_video_formats: 00 00 02 04",
        ],
    )
    def test_ordinary_chatter_is_ignored(self, line):
        assert self.classify(line) == ""


# ---------------------------------------------------------------------------
# The room around it
# ---------------------------------------------------------------------------


@pytest.fixture()
def dashboard(mock_config):
    from app.main import create_app

    # DEV_MODE off deliberately: it makes every receiver report itself healthy,
    # which is right for designing the sharing screen and useless for testing
    # what the room says when something is actually wrong.
    mock_config.update(
        {"MIRACAST_ENABLED": True, "CAST_ENABLED": True, "DEV_MODE": False}
    )
    application = create_app(mock_config, start_services=False)
    application.config.update(TESTING=True)
    return application


class TestRoomIntegration:
    def test_mirroring_puts_the_room_in_sharing_mode(self, dashboard):
        appliance = dashboard.config["ROOM_APPLIANCE"]
        appliance.miracast.handle_event("connected", client="DESKTOP-4F2K")

        assert appliance.room.tick() == "screen-sharing"
        payload = dashboard.test_client().get("/api/state").get_json()
        assert payload["mode"] == "screen-sharing"
        assert payload["miracast"]["client"] == "DESKTOP-4F2K"

    def test_a_meeting_clears_a_miracast_session(self, dashboard, monkeypatch):
        """Same rule as AirPlay: the meeting has to be visible."""
        appliance = dashboard.config["ROOM_APPLIANCE"]
        appliance.miracast.handle_event("connected", client="DESKTOP-4F2K")
        assert appliance.room.sharing is True

        stopped: list[str] = []
        monkeypatch.setattr(
            appliance.miracast, "force_stop_sharing",
            lambda: stopped.append("yes") or True,
        )
        from datetime import datetime, timedelta, timezone

        from app.models import Meeting

        now = datetime.now(timezone.utc)
        appliance.room.open_meeting(
            Meeting(
                uid="m1", title="Standup", start=now, end=now + timedelta(minutes=30),
                provider_id="teams", join_url="https://teams.microsoft.com/l/meetup-join/x",
            )
        )
        assert stopped == ["yes"]

    def test_the_supervisor_endpoint_needs_the_internal_token(self, dashboard):
        client = dashboard.test_client()
        appliance = dashboard.config["ROOM_APPLIANCE"]

        refused = client.post("/api/internal/miracast", json={"event": "connected"})
        assert refused.status_code == 403
        assert appliance.miracast.sharing is False

    def test_the_supervisor_can_report_a_session(self, dashboard):
        from app.web_security import internal_token

        client = dashboard.test_client()
        appliance = dashboard.config["ROOM_APPLIANCE"]

        response = client.post(
            "/api/internal/miracast",
            json={"event": "connected", "client": "DESKTOP-4F2K", "backend": "miraclecast"},
            headers={"X-Room-Internal-Token": internal_token()},
        )
        assert response.get_json()["sharing"] is True
        assert appliance.miracast.sharing is True

    def test_the_supervisor_can_report_being_blocked(self, dashboard):
        from app.web_security import internal_token

        client = dashboard.test_client()
        appliance = dashboard.config["ROOM_APPLIANCE"]

        client.post(
            "/api/internal/miracast",
            json={"event": "blocked", "detail": "wlan0 is on the room network"},
            headers={"X-Room-Internal-Token": internal_token()},
        )
        assert "room network" in str(appliance.miracast.status()["blocked"])

    def test_miracast_is_a_restart_target(self, dashboard):
        client = dashboard.test_client()
        body = client.get("/").get_data(as_text=True)
        token = body[body.index('data-csrf="') + 11 :]
        token = token[: token.index('"')]

        response = client.post(
            "/api/actions/restart", json={"target": "miracast"},
            headers={"X-Room-Token": token},
        )
        # Either it restarted or systemd is not managing units in the test
        # environment; what matters is that the target is recognised.
        assert response.status_code in (200, 409)
        assert "Unknown target" not in response.get_json().get("error", "")

    def test_the_tv_carries_the_win_k_line(self, dashboard):
        body = dashboard.test_client().get("/").get_data(as_text=True)

        assert 'id="sharing-miracast"' in body
        assert 'id="miracast-name"' in body


class TestBrowserAddressStandsDown:
    """The browser fallback should not clutter a TV where Win+K works."""

    @staticmethod
    def _cast(dashboard):
        return dashboard.test_client().get("/api/state").get_json()["cast"]

    def test_it_is_offered_while_miracast_is_not_working(self, dashboard):
        # No receiver software installed, which is Miracast's default state.
        assert self._cast(dashboard)["show_on_tv"] is True

    def test_it_stands_down_once_miracast_is_healthy(self, dashboard, monkeypatch):
        appliance = dashboard.config["ROOM_APPLIANCE"]
        monkeypatch.setattr(
            appliance.miracast, "status",
            lambda: {"enabled": True, "status": OK, "sharing": False},
        )
        assert self._cast(dashboard)["show_on_tv"] is False

    def test_always_means_always(self, dashboard, monkeypatch):
        """For a room with Chromebooks, which have no Miracast at all."""
        appliance = dashboard.config["ROOM_APPLIANCE"]
        appliance.config.update({"CAST_SHOW_ON_TV": "always"})
        monkeypatch.setattr(
            appliance.miracast, "status",
            lambda: {"enabled": True, "status": OK, "sharing": False},
        )
        assert self._cast(dashboard)["show_on_tv"] is True

    def test_never_means_never(self, dashboard):
        appliance = dashboard.config["ROOM_APPLIANCE"]
        appliance.config.update({"CAST_SHOW_ON_TV": "never"})
        assert self._cast(dashboard)["show_on_tv"] is False


# ---------------------------------------------------------------------------
# The readiness probe
# ---------------------------------------------------------------------------


class TestProbe:
    SCRIPT = ROOT / "scripts" / "detect-miracast.sh"

    def test_it_is_shipped_and_executable(self):
        assert self.SCRIPT.is_file()
        assert self.SCRIPT.stat().st_mode & 0o111

    def test_help_works_without_a_configuration(self):
        result = subprocess.run(
            [str(self.SCRIPT), "--help"], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0
        assert "wireless display" in result.stdout.lower()

    def test_it_runs_and_reaches_a_verdict_on_any_machine(self):
        """It is the thing somebody runs when nothing works, so it must never
        itself fall over — including on a machine with no Wi-Fi at all."""
        result = subprocess.run(
            [str(self.SCRIPT)], capture_output=True, text=True, timeout=120
        )
        # 0 = ready, 1 = not ready. Anything else is a crash.
        assert result.returncode in (0, 1), result.stdout + result.stderr
        assert "Verdict" in result.stdout

    def test_it_names_the_browser_fallback_when_it_says_no(self):
        """Somebody whose hardware cannot do Miracast still needs a way to
        share, and should not have to go looking for it."""
        result = subprocess.run(
            [str(self.SCRIPT)], capture_output=True, text=True, timeout=120
        )
        if result.returncode == 1:
            assert "CAST_ENABLED" in result.stdout
