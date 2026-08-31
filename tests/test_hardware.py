"""Poly conference bar, AirPlay and the remote — including their absence.

The appliance must behave sensibly when there is no conference bar, no UxPlay
and no remote, because that is exactly what a half-finished installation or an
unplugged cable looks like.
"""

from __future__ import annotations

from app.airplay_service import AirPlayService
from app.models import FAIL, OFF, OK, UNKNOWN, WARN
from app.poly_service import AudioDevice, CameraDevice, PolyService
from app.remote_service import ACTIONS, KEY_BINDINGS, RemoteService
from app.system_service import SystemService, run


class TestPolyDetection:
    def test_match_words_are_configurable_and_case_insensitive(self, config):
        service = PolyService(config)
        assert service._matches("Polycom Poly Studio X30")
        assert service._matches("PLANTRONICS Voyager")
        assert not service._matches("Logitech BRIO")

        config.update({"POLY_USB_MATCH": "logitech"})
        assert service._matches("Logitech BRIO")
        assert not service._matches("Polycom Poly Studio")

    def test_no_hardware_is_reported_not_crashed(self, config):
        state = PolyService(config).detect()
        assert state.usb_present is False
        assert state.camera is None
        assert state.microphone_status() == FAIL

    def test_a_disabled_bar_reports_as_disabled(self, config):
        config.update({"POLY_ENABLED": False})
        status = PolyService(config).status()
        assert status["enabled"] is False
        assert status["camera"]["status"] == OFF

    def test_mock_mode_pretends_a_bar_is_present(self, mock_config):
        status = PolyService(mock_config).status()
        assert status["mock"] is True
        assert status["camera"]["status"] == OK
        assert status["speaker"]["status"] == OK

    def test_mock_mode_accepts_volume_and_mute(self, mock_config):
        service = PolyService(mock_config)
        assert service.set_volume(55) is True
        assert service.set_mute(True) is True
        assert service.set_mute(False) is False

    def test_an_explicit_device_name_is_honoured(self, config):
        service = PolyService(config)
        devices = [
            AudioDevice(name="alsa_output.hdmi", description="HDMI Audio"),
            AudioDevice(name="alsa_output.poly", description="Poly Studio", matched=True),
        ]
        assert service._choose_audio(devices, "alsa_output.hdmi").name == "alsa_output.hdmi"
        assert service._choose_audio(devices, "hdmi").name == "alsa_output.hdmi"
        assert service._choose_audio(devices, "auto").name == "alsa_output.poly"

    def test_an_unknown_explicit_device_selects_nothing(self, config):
        service = PolyService(config)
        devices = [AudioDevice(name="alsa_output.hdmi", description="HDMI")]
        assert service._choose_audio(devices, "alsa_output.missing") is None

    def test_the_system_default_is_used_when_nothing_matches(self, config):
        service = PolyService(config)
        devices = [
            AudioDevice(name="a", description="Some Speaker"),
            AudioDevice(name="b", description="Another", is_default=True),
        ]
        assert service._choose_audio(devices, "auto").name == "b"

    def test_a_configured_camera_path_wins(self, config):
        config.update({"CAMERA_DEVICE": "/dev/video3"})
        service = PolyService(config)
        cameras = [CameraDevice(path="/dev/video0", name="Poly Studio", matched=True)]
        # The configured path is not enumerated and does not exist, so nothing
        # is selected rather than silently using the wrong camera.
        assert service._choose_camera(cameras) is None

    def test_inventory_lists_what_the_diagnostics_page_shows(self, config):
        service = PolyService(config)
        service.detect()
        inventory = service.inventory()
        assert set(inventory) >= {"cameras", "sources", "sinks", "match_words"}


class TestAirPlay:
    def test_events_track_a_sharing_session(self, config):
        service = AirPlayService(config, SystemService(config))
        assert service.sharing is False

        service.handle_event("started")
        assert service.sharing is False

        service.handle_event("connected", client="A MacBook")
        assert service.sharing is True

        service.handle_event("disconnected")
        assert service.sharing is False

    def test_duplicate_connect_events_are_idempotent(self, config):
        service = AirPlayService(config, SystemService(config))
        changes = []
        service.on_change(changes.append)
        service.handle_event("connected")
        service.handle_event("connected")
        assert changes == [True]

    def test_a_restart_ends_any_session(self, config):
        service = AirPlayService(config, SystemService(config))
        service.handle_event("connected")
        service.handle_event("restarted")
        assert service.sharing is False
        assert service.status()["restarts"] == 1

    def test_uxplay_stopping_ends_the_session(self, config):
        service = AirPlayService(config, SystemService(config))
        service.handle_event("connected")
        service.handle_event("stopped")
        assert service.sharing is False

    def test_a_disabled_receiver_reports_as_disabled(self, config):
        config.update({"AIRPLAY_ENABLED": False})
        status = AirPlayService(config, SystemService(config)).status()
        assert status["enabled"] is False
        assert status["status"] == OFF

    def test_a_missing_uxplay_is_an_error_not_a_crash(self, config):
        status = AirPlayService(config, SystemService(config)).status()
        assert status["uxplay_installed"] is False
        assert status["status"] == FAIL

    def test_the_advertised_name_falls_back_to_the_room_name(self, config):
        config.update({"ROOM_NAME": "The Bridge", "AIRPLAY_NAME": ""})
        assert AirPlayService(config, SystemService(config)).status()["name"] == "The Bridge"

    def test_an_explicit_airplay_name_is_used(self, config):
        config.update({"ROOM_NAME": "The Bridge", "AIRPLAY_NAME": "Bridge TV"})
        assert AirPlayService(config, SystemService(config)).status()["name"] == "Bridge TV"

    def test_a_broken_listener_does_not_break_event_handling(self, config):
        service = AirPlayService(config, SystemService(config))

        def explode(sharing):
            raise RuntimeError("listener bug")

        service.on_change(explode)
        assert service.handle_event("connected")["sharing"] is True


class TestRemote:
    def test_every_binding_maps_to_a_real_action(self):
        for action in KEY_BINDINGS.values():
            assert action in ACTIONS

    def test_the_default_mapping_is_complete(self, config):
        mappings = RemoteService(config, lambda action: None).mappings()
        assert mappings["KEY_ENTER"] == "join"
        assert mappings["KEY_ESC"] == "hangup"
        assert mappings["KEY_MUTE"] == "mute"

    def test_an_empty_binding_is_simply_unmapped(self, config):
        config.update({"POLY_CAMERA_KEY": ""})
        assert "camera" not in RemoteService(config, lambda a: None).mappings().values()

    def test_remapping_a_button_takes_effect(self, config):
        config.update({"POLY_ANSWER_KEY": "KEY_F1"})
        mappings = RemoteService(config, lambda a: None).mappings()
        assert mappings["KEY_F1"] == "join"
        assert "KEY_ENTER" not in mappings

    def test_a_disabled_remote_starts_nothing(self, config):
        service = RemoteService(config, lambda a: None)
        service.start()
        assert service.status()["enabled"] is False

    def test_missing_evdev_is_explained_not_fatal(self, config):
        config.update({"POLY_REMOTE_ENABLED": True})
        service = RemoteService(config, lambda a: None)
        service.start()
        status = service.status()
        if not status["available"]:
            assert "evdev" in status["error"]
            assert service.capture_keys(2)["ok"] is False

    def test_debouncing_ignores_a_repeated_press(self, config):
        fired = []
        service = RemoteService(config, fired.append)
        mappings = {"KEY_ENTER": "join"}
        service._handle_key("KEY_ENTER", mappings)
        service._handle_key("KEY_ENTER", mappings)
        assert fired == ["join"], "a double press must not join twice"

    def test_an_unmapped_button_does_nothing(self, config):
        fired = []
        service = RemoteService(config, fired.append)
        service._handle_key("KEY_COFFEE", {"KEY_ENTER": "join"})
        assert fired == []

    def test_a_failing_action_does_not_stop_the_listener(self, config):
        def explode(action):
            raise RuntimeError("action bug")

        service = RemoteService(config, explode)
        service._handle_key("KEY_ENTER", {"KEY_ENTER": "join"})
        assert service.status()["last_action"] == "join"

    def test_a_watcher_with_no_dispatcher_reports_but_never_acts(self, config):
        """The web backend's copy: two of them would act on every press twice.

        Mute and camera both toggle, so a doubled press left them exactly where
        they started — the remote looked dead.
        """
        service = RemoteService(config)
        service._handle_key("KEY_MUTE", {"KEY_MUTE": "mute"})
        status = service.status()
        assert status["last_action"] == "mute", "still worth showing in Diagnostics"
        assert status["presses"] == 1

    def test_the_backend_does_not_act_on_buttons_itself(self, mock_config):
        """room-remote.service forwards them to /api/internal/action instead."""
        from app.main import RoomAppliance

        appliance = RoomAppliance(mock_config)
        assert appliance.remote.dispatch is None


class TestSystemService:
    def test_only_our_own_units_can_be_restarted(self, config):
        service = SystemService(config)
        assert service.restart("sshd.service") is False
        assert service.restart("../../etc/passwd") is False

    def test_user_scope_needs_no_privileges(self, config):
        service = SystemService(config)
        command = service._systemctl("restart", "room-kiosk.service")
        assert command[:2] == ["systemctl", "--user"]
        assert "sudo" not in " ".join(command)

    def test_a_missing_binary_is_reported(self):
        result = run(["definitely-not-installed-anywhere"])
        assert not result.ok and "not installed" in result.stderr

    def test_a_hung_command_is_killed(self):
        result = run(["sleep", "5"], timeout=0.4)
        assert not result.ok and "timed out" in result.stderr

    def test_an_empty_command_is_refused(self):
        assert run([]).ok is False

    def test_host_facts_are_readable_or_absent(self, config):
        service = SystemService(config)
        assert service.uptime_seconds() >= 0
        assert len(service.load_average()) == 3
        assert service.hostname()
        # These may be None on non-Pi hardware; they must never raise.
        service.temperature_celsius()
        service.disk_free_percent()
        service.memory_available_mb()

    def test_restarts_are_rate_limited(self, mock_config):
        service = SystemService(mock_config)
        assert service.restart("room-kiosk.service", min_interval=60) is True
        assert service.restart("room-kiosk.service", min_interval=60) is False

    def test_reboot_is_rate_limited(self, mock_config):
        service = SystemService(mock_config)
        assert service.reboot(min_interval=3600) is True
        assert service.reboot(min_interval=3600) is False
