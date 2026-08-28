"""Structured logging that reads well in ``journalctl``.

Two formats are available (``LOG_FORMAT``):

* ``text`` – ``INFO  calendar.refreshed events=7 source=ics``
* ``json`` – one JSON object per line, for log collectors

Every log record may carry structured fields via ``extra={"fields": {...}}``,
or more conveniently through :func:`log_event`.

Secrets never reach the log: :class:`RedactingFilter` scrubs calendar URLs,
meeting URLs, tokens and PINs from both the message and the fields.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any

_LOGGER_NAME = "room"

# Query-string secrets (?token=…, &sig=…) and full meeting URLs are the two
# things that would otherwise leak into logs.
_SECRET_QUERY_RE = re.compile(
    r"(?i)\b(token|sig|signature|key|secret|password|passwd|pwd|pin|apikey|api_key|"
    r"access_token|refresh_token|code|auth)=([^&\s\"']+)"
)
_URL_RE = re.compile(r"(?i)\b((?:https?|webcal)://)([^\s\"'<>]+)")
_SAFE_URL_HOSTS = (
    "teams.microsoft.com",
    "teams.live.com",
    "meet.google.com",
    "zoom.us",
    "webex.com",
    "127.0.0.1",
    "localhost",
)


def redact_url(url: str) -> str:
    """Reduce a URL to something safe to log.

    Only the scheme and host survive for anything off this machine. That is
    deliberate: a Google Meet code (``/abc-defg-hij``) is short enough to look
    harmless but *is* the key to the meeting, exactly like a Zoom passcode or a
    calendar feed token. Localhost paths are kept, because they are the room's
    own pages and are genuinely useful when debugging.
    """
    if not url:
        return ""
    match = re.match(r"(?i)^((?:https?|webcal)://)([^/?#]+)(.*)$", url)
    if not match:
        return "<url>"
    scheme, host, rest = match.groups()
    hostname = host.split(":", 1)[0].lower()

    if hostname in ("127.0.0.1", "localhost", "::1"):
        path = rest.split("?", 1)[0].split("#", 1)[0] or "/"
        return f"{scheme}{host}{path}"

    return f"{scheme}{host}/" if not rest.strip("/") else f"{scheme}{host}/…"


def _redact_text(text: str) -> str:
    text = _SECRET_QUERY_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)

    def _url_sub(match: re.Match[str]) -> str:
        full = match.group(0)
        host = match.group(2).split("/", 1)[0].lower()
        if any(host.endswith(safe) or host == safe for safe in _SAFE_URL_HOSTS):
            # Provider hosts are useful and not secret, but the path can carry
            # a meeting passcode, so it still gets trimmed.
            return redact_url(full)
        return redact_url(full)

    return _URL_RE.sub(_url_sub, text)


class RedactingFilter(logging.Filter):
    """Scrub secrets from messages and structured fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = _redact_text(record.msg)
            fields = getattr(record, "fields", None)
            if isinstance(fields, dict):
                record.fields = {k: self._clean(k, v) for k, v in fields.items()}
        except Exception:  # pragma: no cover - logging must never raise
            pass
        return True

    @staticmethod
    def _clean(key: str, value: Any) -> Any:
        lowered = key.lower()
        if any(word in lowered for word in ("secret", "token", "password", "pin", "ics_url")):
            return "<redacted>" if value not in (None, "", False) else value
        if isinstance(value, str):
            return _redact_text(value)
        return value


def _format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if not text:
        return '""'
    if any(c in text for c in " \t\"'"):
        return '"' + text.replace('"', "'") + '"'
    return text


class TextFormatter(logging.Formatter):
    """``LEVEL  event key=value key=value`` – compact and greppable."""

    def format(self, record: logging.LogRecord) -> str:
        base = record.getMessage()
        fields = getattr(record, "fields", None)
        parts = [f"{record.levelname:<7}", base]
        if isinstance(fields, dict) and fields:
            parts.append(
                " ".join(f"{k}={_format_value(v)}" for k, v in fields.items())
            )
        line = " ".join(p for p in parts if p)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for key, value in fields.items():
                if key not in payload:
                    payload[key] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, default=str)
        except Exception:  # pragma: no cover
            return json.dumps({"level": record.levelname, "event": str(record.msg)})


def setup_logging(level: str = "INFO", fmt: str = "text") -> logging.Logger:
    """Configure the ``room`` logger. Safe to call again after a config change."""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover
            pass

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)

    # Flask/Werkzeug request logs are noise on an appliance; keep warnings only.
    for noisy in ("werkzeug", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if os.environ.get("ROOM_APPLIANCE_QUIET") == "1":
        logger.setLevel(logging.CRITICAL)
    return logger


def get_logger(name: str = "") -> logging.Logger:
    """Return the appliance logger, optionally a child (``room.calendar``)."""
    return logging.getLogger(f"{_LOGGER_NAME}.{name}" if name else _LOGGER_NAME)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    /,
    **fields: Any,
) -> None:
    """Log a structured event: ``log_event(log, logging.INFO, 'calendar.refreshed', events=7)``."""
    logger.log(level, event, extra={"fields": fields})
