"""Shared test fixtures.

Every test runs against a throw-away configuration and state directory, so the
suite can never touch a real installation's config.yaml, calendar cache or
browser profile.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def room_dirs(tmp_path, monkeypatch):
    """Point the appliance's paths at a temporary directory."""
    var_dir = tmp_path / "var"
    config_dir = tmp_path / "config"
    var_dir.mkdir()
    config_dir.mkdir()

    monkeypatch.setenv("ROOM_APPLIANCE_VAR", str(var_dir))
    monkeypatch.setenv("ROOM_APPLIANCE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("ROOM_APPLIANCE_QUIET", "1")
    # Any real setting leaking in from the developer's shell would make tests
    # depend on their machine.
    for name in list(os.environ):
        if name.startswith("ROOM_") and name not in (
            "ROOM_APPLIANCE_VAR",
            "ROOM_APPLIANCE_CONFIG_DIR",
            "ROOM_APPLIANCE_QUIET",
            "ROOM_PYTHON",
        ):
            monkeypatch.delenv(name, raising=False)

    from app import paths

    importlib.reload(paths)
    yield {"var": var_dir, "config": config_dir, "file": config_dir / "config.yaml"}
    importlib.reload(paths)


@pytest.fixture()
def config(room_dirs):
    """A fresh ConfigManager backed by the temporary directory."""
    from app.config import ConfigManager

    return ConfigManager(room_dirs["file"])


@pytest.fixture()
def mock_config(config):
    """A configuration that needs no hardware and no network."""
    changed, errors = config.update(
        {
            "CALENDAR_SOURCE": "mock",
            "DEV_MODE": True,
            "KIOSK_ENABLED": False,
            "AIRPLAY_ENABLED": True,
            "ROOM_NAME": "Test Room",
            "TIMEZONE": "UTC",
        }
    )
    assert not errors, errors
    return config


@pytest.fixture()
def ics_now():
    """The instant the sample feed is built around, to the minute."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(second=0, microsecond=0)


@pytest.fixture()
def sample_ics(tmp_path, ics_now):
    """A small but realistic ICS feed, written to a file.

    Events are placed relative to *now* rather than on fixed dates, so the same
    feed exercises both the parser and the live refresh loop (which only looks
    a day ahead) whenever the suite happens to run.

      now + 1h   Engineering Daily   recurring daily, Teams link in DESCRIPTION
      now + 3h   Supplier Call       Google Meet link in LOCATION
      now + 5h   In-person Workshop  no online link at all
    """
    from datetime import timedelta

    def stamp(moment):
        return moment.strftime("%Y%m%dT%H%M%SZ")

    standup = ics_now + timedelta(hours=1)
    supplier = ics_now + timedelta(hours=3)
    workshop = ics_now + timedelta(hours=5)

    path = tmp_path / "room.ics"
    path.write_text(
        "\r\n".join(
            [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//Tests//Room//EN",
                "BEGIN:VEVENT",
                "UID:standup@example.com",
                f"DTSTAMP:{stamp(ics_now)}",
                f"DTSTART:{stamp(standup)}",
                f"DTEND:{stamp(standup + timedelta(minutes=30))}",
                "RRULE:FREQ=DAILY",
                "SUMMARY:Engineering Daily",
                "LOCATION:Microsoft Teams Meeting",
                "DESCRIPTION:Join here: https://teams.microsoft.com/l/meetup-join/"
                "19%3ameeting_ABC%40thread.v2/0",
                "END:VEVENT",
                "BEGIN:VEVENT",
                "UID:supplier@example.com",
                f"DTSTAMP:{stamp(ics_now)}",
                f"DTSTART:{stamp(supplier)}",
                f"DTEND:{stamp(supplier + timedelta(minutes=45))}",
                "SUMMARY:Supplier Call",
                "LOCATION:https://meet.google.com/abc-defg-hij",
                "END:VEVENT",
                "BEGIN:VEVENT",
                "UID:offline@example.com",
                f"DTSTAMP:{stamp(ics_now)}",
                f"DTSTART:{stamp(workshop)}",
                f"DTEND:{stamp(workshop + timedelta(hours=1))}",
                "SUMMARY:In-person Workshop",
                "LOCATION:Room 4",
                "END:VEVENT",
                "END:VCALENDAR",
            ]
        ),
        encoding="utf-8",
    )
    return path
