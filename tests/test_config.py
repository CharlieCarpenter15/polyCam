"""Configuration: layering, validation, persistence and recovery."""

from __future__ import annotations

import yaml

from app.config import ConfigManager, advisories, coerce, cross_check, render_yaml
from app.config_schema import FIELDS, FIELDS_BY_KEY, SECRET_KEYS, defaults


class TestDefaults:
    def test_every_field_has_a_default(self):
        values = defaults()
        assert set(values) == {f.key for f in FIELDS}

    def test_defaults_are_valid(self, config):
        """A fresh appliance must never start with a complaint."""
        assert config.warnings == []

    def test_a_fresh_room_needs_only_a_calendar(self, config):
        assert config.setup_required()
        config.update({"CALENDAR_ICS_URL": "https://example.com/room.ics"})
        assert not config.setup_required()


class TestCoercion:
    def test_booleans_accept_human_spellings(self):
        field = FIELDS_BY_KEY["TIME_FORMAT_24H"]
        for text in ("yes", "true", "on", "1", "Enabled"):
            assert coerce(field, text) is True
        for text in ("no", "false", "off", "0", ""):
            assert coerce(field, text) is False

    def test_numbers_are_bounded(self, config):
        _, errors = config.update({"CALENDAR_REFRESH_SECONDS": 2})
        assert "CALENDAR_REFRESH_SECONDS" in errors
        _, errors = config.update({"CALENDAR_REFRESH_SECONDS": 99999})
        assert "CALENDAR_REFRESH_SECONDS" in errors
        changed, errors = config.update({"CALENDAR_REFRESH_SECONDS": 45})
        assert not errors and "CALENDAR_REFRESH_SECONDS" in changed

    def test_lists_accept_newline_separated_text(self):
        field = FIELDS_BY_KEY["JOIN_BUTTON_TEXTS"]
        assert coerce(field, "Join now\nAsk to join\n\n") == ["Join now", "Ask to join"]

    def test_choices_are_case_insensitive(self):
        assert coerce(FIELDS_BY_KEY["LOG_LEVEL"], "debug") == "DEBUG"

    def test_bad_timezone_is_rejected(self, config):
        _, errors = config.update({"TIMEZONE": "Mars/Olympus_Mons"})
        assert "TIMEZONE" in errors
        changed, errors = config.update({"TIMEZONE": "Europe/London"})
        assert not errors


class TestSafetyRules:
    def test_network_admin_requires_a_pin(self, config):
        _, errors = config.update({"ADMIN_LAN_ACCESS": True})
        assert "ADMIN_PIN" in errors
        changed, errors = config.update({"ADMIN_LAN_ACCESS": True, "ADMIN_PIN": "1234"})
        assert not errors and config.bool_("ADMIN_LAN_ACCESS")

    def test_network_admin_is_switched_off_if_the_pin_disappears(self, room_dirs):
        """Editing config.yaml by hand must not leave the room wide open."""
        room_dirs["file"].write_text(
            yaml.safe_dump({"ADMIN_LAN_ACCESS": True, "ADMIN_PIN": ""}), encoding="utf-8"
        )
        manager = ConfigManager(room_dirs["file"])
        assert manager.bool_("ADMIN_LAN_ACCESS") is False
        assert any("switched off" in warning for warning in manager.warnings)

    def test_short_pins_are_rejected(self, config):
        _, errors = config.update({"ADMIN_PIN": "12"})
        assert "ADMIN_PIN" in errors
        _, errors = config.update({"ADMIN_PIN": "abcd"})
        assert "ADMIN_PIN" in errors

    def test_a_half_configured_room_can_still_save(self, config):
        """The setup page is unusable if a missing calendar blocks every save."""
        assert config.setup_required()
        changed, errors = config.update({"ROOM_NAME": "Boardroom", "THEME": "light"})
        assert not errors
        assert changed == {"ROOM_NAME", "THEME"}

    def test_missing_calendar_is_an_advisory_not_an_error(self, config):
        assert "CALENDAR_ICS_URL" not in cross_check(config.as_dict())
        assert "CALENDAR_ICS_URL" in advisories(config.as_dict())

    def test_all_problems_are_reported_together(self, config):
        _, errors = config.update(
            {
                "CALENDAR_REFRESH_SECONDS": 1,
                "ACCENT_COLOR": "not-a-colour",
                "DAILY_RESTART_TIME": "half past nine",
            }
        )
        assert set(errors) == {
            "CALENDAR_REFRESH_SECONDS",
            "ACCENT_COLOR",
            "DAILY_RESTART_TIME",
        }

    def test_a_rejected_save_changes_nothing(self, config):
        config.update({"ROOM_NAME": "Before"})
        changed, errors = config.update({"ROOM_NAME": "After", "THEME": "chartreuse"})
        assert errors and changed == set()
        assert config.str_("ROOM_NAME") == "Before"


class TestPersistence:
    def test_settings_round_trip_through_yaml(self, config, room_dirs):
        config.update(
            {
                "ROOM_NAME": "Boardroom: Level 3",
                "ROOM_SUBTITLE": "8 seats · natural light",
                "JOIN_BUTTON_TEXTS": "Join now\nAsk to join",
                "CALENDAR_ICS_URL": "https://example.com/cal.ics?token=secret",
                "TIME_FORMAT_24H": True,
                "AUTO_OPEN_MINUTES": 1.5,
            }
        )
        reloaded = ConfigManager(room_dirs["file"])
        assert reloaded.as_dict() == config.as_dict()
        assert reloaded.warnings == []

    def test_the_written_file_is_valid_yaml(self, config, room_dirs):
        config.update({"ROOM_NAME": "Quote's \"Room\"", "JOIN_BUTTON_TEXTS": "A: B\n- C"})
        raw = yaml.safe_load(room_dirs["file"].read_text(encoding="utf-8"))
        assert raw["ROOM_NAME"] == "Quote's \"Room\""
        assert raw["JOIN_BUTTON_TEXTS"] == ["A: B", "- C"]

    def test_the_file_is_not_world_readable(self, config, room_dirs):
        config.update({"CALENDAR_ICS_URL": "https://example.com/secret.ics"})
        mode = room_dirs["file"].stat().st_mode & 0o777
        assert mode == 0o600, f"config.yaml is {oct(mode)}; secrets would be readable"

    def test_rendered_yaml_documents_every_option(self):
        text = render_yaml(defaults())
        for field in FIELDS:
            assert f"{field.key}:" in text


class TestRecovery:
    def test_a_corrupt_file_falls_back_to_the_backup(self, config, room_dirs):
        config.update({"ROOM_NAME": "Good Room"})
        config.update({"ROOM_SUBTITLE": "second save creates the backup"})

        room_dirs["file"].write_text("this: [is not: valid", encoding="utf-8")
        recovered = ConfigManager(room_dirs["file"])

        assert recovered.str_("ROOM_NAME") == "Good Room"
        assert recovered.loaded_from == "config.yaml.bak"
        assert recovered.warnings

    def test_a_corrupt_file_with_no_backup_falls_back_to_defaults(self, room_dirs):
        room_dirs["file"].write_text("!!!not yaml at all: [", encoding="utf-8")
        recovered = ConfigManager(room_dirs["file"])
        assert recovered.str_("ROOM_NAME") == "Meeting Room"
        assert recovered.warnings

    def test_a_broken_file_is_kept_for_inspection(self, room_dirs):
        from app import paths

        room_dirs["file"].write_text("nope: [", encoding="utf-8")
        ConfigManager(room_dirs["file"])
        assert paths.CONFIG_BROKEN.exists()

    def test_an_out_of_range_value_in_the_file_is_reset_not_fatal(self, room_dirs):
        room_dirs["file"].write_text(
            yaml.safe_dump({"CALENDAR_REFRESH_SECONDS": -5, "ROOM_NAME": "Kept"}),
            encoding="utf-8",
        )
        manager = ConfigManager(room_dirs["file"])
        assert manager.int_("CALENDAR_REFRESH_SECONDS") == 30
        assert manager.str_("ROOM_NAME") == "Kept"
        assert any("Refresh interval" in w for w in manager.warnings)

    def test_unknown_keys_in_the_file_are_ignored(self, room_dirs):
        room_dirs["file"].write_text(
            yaml.safe_dump({"ROOM_NAME": "Kept", "LEFTOVER_OPTION": 1}), encoding="utf-8"
        )
        manager = ConfigManager(room_dirs["file"])
        assert manager.str_("ROOM_NAME") == "Kept"
        assert "LEFTOVER_OPTION" not in manager.as_dict()

    def test_reset_keeps_what_it_is_told_to(self, config):
        config.update({"ROOM_NAME": "Keep Me", "THEME": "light", "ADMIN_PIN": "4321"})
        config.reset_to_defaults(keep=("ROOM_NAME", "ADMIN_PIN"))
        assert config.str_("ROOM_NAME") == "Keep Me"
        assert config.str_("ADMIN_PIN") == "4321"
        assert config.str_("THEME") == "dark"


class TestSecrets:
    def test_secrets_are_redacted_in_api_output(self, config):
        config.update({"CALENDAR_ICS_URL": "https://example.com/x.ics", "ADMIN_PIN": "1234"})
        redacted = config.as_dict(redact=True)
        for key in SECRET_KEYS:
            if config.get(key):
                assert redacted[key] == "********"
        assert config.as_dict()["ADMIN_PIN"] == "1234"

    def test_empty_secrets_are_not_masked(self, config):
        assert config.as_dict(redact=True)["AIRPLAY_PIN"] == ""


class TestEnvironment:
    def test_environment_overrides_the_file(self, room_dirs, monkeypatch):
        manager = ConfigManager(room_dirs["file"])
        manager.update({"ROOM_NAME": "From File"})
        monkeypatch.setenv("ROOM_NAME", "From Environment")
        reloaded = ConfigManager(room_dirs["file"])
        assert reloaded.str_("ROOM_NAME") == "From Environment"
        assert "ROOM_NAME" in reloaded.env_locked_keys()

    def test_a_prefixed_variable_also_works(self, room_dirs, monkeypatch):
        monkeypatch.setenv("ROOM_APPLIANCE_DASHBOARD_PORT", "9999")
        manager = ConfigManager(room_dirs["file"])
        assert manager.int_("DASHBOARD_PORT") == 9999

    def test_an_invalid_environment_value_is_ignored(self, room_dirs, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PORT", "not-a-port")
        manager = ConfigManager(room_dirs["file"])
        assert manager.int_("DASHBOARD_PORT") == 8080


class TestChangeNotification:
    def test_listeners_are_told_what_changed(self, config):
        seen = []
        config.on_change(lambda values, changed: seen.append(changed))
        config.update({"ROOM_NAME": "New"})
        assert seen == [{"ROOM_NAME"}]

    def test_a_broken_listener_does_not_break_saving(self, config):
        def explode(values, changed):
            raise RuntimeError("listener bug")

        config.on_change(explode)
        changed, errors = config.update({"ROOM_NAME": "Still Saved"})
        assert not errors and config.str_("ROOM_NAME") == "Still Saved"

    def test_restart_units_are_reported(self, config):
        changed, _ = config.update({"AIRPLAY_NAME": "Room One"})
        assert config.restart_units_for_changes(changed) == ["room-airplay.service"]
