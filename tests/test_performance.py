"""Hardware detection and the performance profile it chooses.

The appliance's defaults were written for a Raspberry Pi. These tests pin down
what happens when it is not running on one — and, just as importantly, that a
setting somebody typed in by hand is never overridden by a guess about the
machine.
"""

from __future__ import annotations

import pytest

from app.hardware_profile import (
    BALANCED,
    HIGH,
    LOW,
    PROFILES,
    TUNINGS,
    Machine,
    classify,
    detect_machine,
    report,
    resolve,
    tuning_for,
)


def pi(generation: int, memory_gb: float, cores: int = 4) -> Machine:
    return Machine(
        cores=cores,
        memory_gb=memory_gb,
        model=f"Raspberry Pi {generation} Model B Rev 1.0",
        architecture="aarch64",
        is_raspberry_pi=True,
        pi_generation=generation,
    )


def pc(cores: int, memory_gb: float, model: str = "Intel NUC13ANKi5") -> Machine:
    return Machine(
        cores=cores,
        memory_gb=memory_gb,
        model=model,
        architecture="x86_64",
    )


class TestClassification:
    @pytest.mark.parametrize(
        "machine,expected",
        [
            (pc(8, 32.0), HIGH),
            (pc(4, 16.0), HIGH),
            (pc(4, 8.0), HIGH),
            # A real computer, but a small one: no reason to push it hard.
            (pc(2, 8.0), BALANCED),
            (pc(4, 4.0), BALANCED),
            # The hardware the defaults were written for.
            (pi(5, 8.0), BALANCED),
            (pi(4, 4.0), BALANCED),
            # A Pi 3 has 1 GB and no hardware video encode.
            (pi(3, 1.0, cores=4), LOW),
            (pi(2, 1.0, cores=4), LOW),
            # Small is small, whatever it calls itself.
            (pc(8, 1.5), LOW),
        ],
    )
    def test_machines_land_in_the_right_profile(self, machine, expected):
        assert classify(machine) == expected

    def test_an_unreadable_machine_is_treated_as_ordinary(self):
        """Detection failing must never make the room behave strangely."""
        assert classify(Machine()) == BALANCED

    def test_a_pi_is_never_called_a_powerful_computer(self):
        """Even a well-specified Pi 5 is not a NUC, and pretending otherwise
        would strip the timing headroom its browser genuinely needs."""
        assert classify(pi(5, 16.0, cores=8)) != HIGH


class TestTuning:
    def test_every_profile_has_tuning(self):
        for name in PROFILES:
            if name == "auto":
                continue
            assert name in TUNINGS, name

    def test_a_faster_machine_waits_less_and_gives_up_sooner(self):
        fast, ordinary, slow = TUNINGS[HIGH], TUNINGS[BALANCED], TUNINGS[LOW]
        assert fast.settle_multiplier < ordinary.settle_multiplier < slow.settle_multiplier
        assert fast.join_timeout_seconds < ordinary.join_timeout_seconds
        assert slow.join_timeout_seconds > ordinary.join_timeout_seconds
        assert fast.poll_ms < ordinary.poll_ms < slow.poll_ms

    def test_only_the_fast_profile_asks_for_more_of_the_machine(self):
        assert TUNINGS[HIGH].chromium_args, "the whole point is the extra flags"
        assert not TUNINGS[BALANCED].chromium_args, "balanced is today's behaviour"

    def test_chromium_flags_are_flags(self):
        """A stray word here would be passed to Chromium as a URL to open."""
        for name, tuning in TUNINGS.items():
            for argument in tuning.chromium_args:
                assert argument.startswith("--"), f"{name}: {argument}"
            for feature in tuning.enable_features + tuning.disable_features:
                assert "," not in feature and " " not in feature, f"{name}: {feature}"


class TestResolution:
    def test_an_explicit_choice_beats_the_guess(self):
        profile, _machine = resolve("low")
        assert profile == LOW
        assert tuning_for("high").profile == HIGH

    def test_nonsense_falls_back_to_the_guess(self):
        assert resolve("turbo")[0] == resolve("auto")[0]

    def test_the_report_says_where_the_answer_came_from(self):
        automatic = report("auto")
        assert automatic["automatic"] is True
        assert automatic["profile"] in TUNINGS
        assert automatic["summary"]
        assert automatic["machine"]["description"]

        manual = report("low")
        assert manual["automatic"] is False
        assert manual["profile"] == LOW

    def test_this_machine_can_be_detected_without_raising(self):
        machine = detect_machine()
        assert machine.cores >= 0
        assert machine.memory_gb >= 0
        assert isinstance(machine.describe(), str)


class TestAppliesToTheRoom:
    def test_the_configuration_exposes_the_profile(self, mock_config):
        assert mock_config.str_("PERFORMANCE_PROFILE") == "auto"
        assert mock_config.performance().profile in TUNINGS
        assert mock_config.performance_report()["machine"]["description"]

    def test_choosing_a_profile_changes_the_tuning(self, mock_config):
        mock_config.update({"PERFORMANCE_PROFILE": "low"})
        assert mock_config.performance().profile == LOW
        mock_config.update({"PERFORMANCE_PROFILE": "high"})
        assert mock_config.performance().profile == HIGH

    def test_a_hand_set_join_timeout_is_never_overridden(self, mock_config):
        """The profile supplies defaults. It does not argue with the operator."""
        from app.browser_service import BrowserService
        from app.system_service import SystemService

        mock_config.update({"PERFORMANCE_PROFILE": "high", "AUTO_JOIN_TIMEOUT_SECONDS": 240})
        browser = BrowserService(mock_config, SystemService(mock_config))
        assert browser._join_timeout(mock_config.performance()) == 240

    def test_an_untouched_join_timeout_follows_the_machine(self, mock_config):
        from app.browser_service import BrowserService
        from app.system_service import SystemService

        system = SystemService(mock_config)

        mock_config.update({"PERFORMANCE_PROFILE": "high"})
        browser = BrowserService(mock_config, system)
        assert browser._join_timeout(mock_config.performance()) == TUNINGS[HIGH].join_timeout_seconds

        mock_config.update({"PERFORMANCE_PROFILE": "low"})
        assert browser._join_timeout(mock_config.performance()) == TUNINGS[LOW].join_timeout_seconds

    def test_the_dashboard_is_told_how_often_to_ask(self, mock_config):
        from app.main import create_app

        mock_config.update({"PERFORMANCE_PROFILE": "low"})
        client = create_app(mock_config, start_services=False).test_client()
        display = client.get("/api/state").get_json()["display"]
        assert display["poll_ms"] == TUNINGS[LOW].poll_ms

    def test_health_reports_the_machine(self, mock_config):
        from app.main import create_app

        client = create_app(mock_config, start_services=False).test_client()
        performance = client.get("/api/health").get_json()["performance"]
        assert performance["profile"] in TUNINGS
        assert "description" in performance["machine"]
