"""Logging: structure, and never leaking a secret."""

from __future__ import annotations

import io
import logging

import pytest

from app.logging_setup import (
    JsonFormatter,
    RedactingFilter,
    TextFormatter,
    log_event,
    redact_url,
    setup_logging,
)


@pytest.fixture()
def capture():
    """A logger writing into a string, with the real filter attached."""
    stream = io.StringIO()
    logger = logging.getLogger("test.redaction")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(TextFormatter())
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    return logger, stream


class TestRedaction:
    def test_a_calendar_token_never_reaches_the_log(self, capture):
        logger, stream = capture
        log_event(
            logger,
            logging.WARNING,
            "calendar.refresh_failed",
            url="https://outlook.office365.com/owa/calendar/abc/cal.ics?token=SUPERSECRET",
        )
        output = stream.getvalue()
        assert "SUPERSECRET" not in output
        assert "outlook.office365.com" in output, "the host is useful and not secret"

    def test_a_meeting_url_is_trimmed_to_its_host(self, capture):
        logger, stream = capture
        log_event(
            logger,
            logging.INFO,
            "meeting.opening",
            url="https://teams.microsoft.com/l/meetup-join/19%3ameeting_PRIVATE/0?context=x",
        )
        output = stream.getvalue()
        assert "meeting_PRIVATE" not in output
        assert "teams.microsoft.com" in output

    def test_a_zoom_passcode_is_removed(self, capture):
        logger, stream = capture
        log_event(logger, logging.INFO, "meeting.opening",
                  url="https://us02web.zoom.us/j/123?pwd=LETMEIN")
        assert "LETMEIN" not in stream.getvalue()

    @pytest.mark.parametrize(
        "field", ["ics_url", "admin_pin", "auth_token", "client_secret", "password"]
    )
    def test_secret_looking_fields_are_masked(self, capture, field):
        logger, stream = capture
        log_event(logger, logging.INFO, "config.updated", **{field: "sensitive-value"})
        output = stream.getvalue()
        assert "sensitive-value" not in output
        assert "<redacted>" in output

    def test_a_secret_in_the_message_itself_is_scrubbed(self, capture):
        logger, stream = capture
        logger.warning("fetch failed for https://example.com/cal.ics?sig=ABCDEF123")
        assert "ABCDEF123" not in stream.getvalue()

    def test_ordinary_values_survive(self, capture):
        logger, stream = capture
        log_event(logger, logging.INFO, "calendar.refreshed", events=7, source="ics")
        output = stream.getvalue()
        assert "events=7" in output and "source=ics" in output


class TestRedactUrl:
    @pytest.mark.parametrize(
        "url,forbidden",
        [
            ("https://meet.google.com/abc-defg-hij", "abc-defg-hij"),
            ("https://example.com/a/very/long/secret/path?x=1", "secret"),
        ],
    )
    def test_paths_and_queries_are_dropped_or_shortened(self, url, forbidden):
        result = redact_url(url)
        assert "?" not in result
        assert forbidden not in result or len(result) < len(url)

    def test_a_host_only_url_is_preserved(self):
        assert redact_url("https://example.com") == "https://example.com/"

    def test_nonsense_is_handled(self):
        assert redact_url("") == ""
        assert redact_url("not a url") == "<url>"


class TestFormats:
    def test_text_format_is_greppable(self):
        record = logging.LogRecord(
            "room", logging.INFO, __file__, 1, "calendar.refreshed", (), None
        )
        record.fields = {"events": 7, "stale": False, "note": "with spaces"}
        line = TextFormatter().format(record)
        assert line.startswith("INFO")
        assert "events=7" in line
        assert "stale=false" in line
        assert 'note="with spaces"' in line

    def test_json_format_is_one_object_per_line(self):
        import json

        record = logging.LogRecord(
            "room", logging.WARNING, __file__, 1, "network.unavailable", (), None
        )
        record.fields = {"hosts": "1.1.1.1"}
        payload = json.loads(JsonFormatter().format(record))
        assert payload["level"] == "WARNING"
        assert payload["event"] == "network.unavailable"
        assert payload["hosts"] == "1.1.1.1"

    def test_setup_is_repeatable_without_duplicating_output(self):
        first = setup_logging("INFO", "text")
        count = len(first.handlers)
        second = setup_logging("DEBUG", "json")
        assert len(second.handlers) == count == 1
