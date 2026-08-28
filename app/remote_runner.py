"""Standalone runner for the Poly remote / controller handler.

Run by ``room-remote.service`` as its own process, for two reasons:

* Reading ``/dev/input`` needs the ``input`` group. Keeping it separate means
  the web backend does not need that access.
* A misbehaving HID device cannot take the dashboard down with it.

Button presses are forwarded to the backend over localhost using the internal
shared token — the same mechanism the AirPlay supervisor uses. If the backend is
temporarily down, presses are simply dropped with a log line; nothing queues up,
because a button pressed thirty seconds ago should not fire later.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time

import requests

from . import paths
from .config import get_config
from .logging_setup import get_logger, log_event, setup_logging
from .remote_service import RemoteService
from .web_security import internal_token

log = get_logger("remote-runner")

#: Presses older than this are discarded rather than replayed late.
MAX_ACTION_AGE_SECONDS = 5.0


class ActionForwarder:
    """Posts remote actions to the running backend."""

    def __init__(self) -> None:
        self.config = get_config()
        self._session = requests.Session()
        self._token = internal_token()
        self._last_warning = 0.0

    @property
    def url(self) -> str:
        port = self.config.int_("DASHBOARD_PORT")
        return f"http://127.0.0.1:{port}/api/internal/action"

    def __call__(self, action: str) -> None:
        pressed_at = time.monotonic()
        try:
            response = self._session.post(
                self.url,
                json={"action": action},
                headers={"X-Room-Internal-Token": self._token},
                timeout=6,
            )
        except requests.RequestException as exc:
            # Rate-limit the complaint: a backend restart should not flood logs.
            if time.monotonic() - self._last_warning > 30:
                self._last_warning = time.monotonic()
                log_event(
                    log, logging.WARNING, "remote.backend_unreachable",
                    action=action, error=exc.__class__.__name__,
                )
            return

        if time.monotonic() - pressed_at > MAX_ACTION_AGE_SECONDS:
            log_event(log, logging.WARNING, "remote.action_slow", action=action)

        if response.status_code == 403:
            # The token file changed (backend reinstalled); pick up the new one.
            self._token = internal_token()
            log_event(log, logging.WARNING, "remote.token_refreshed")
            return
        if not response.ok:
            log_event(
                log, logging.WARNING, "remote.action_rejected",
                action=action, status=response.status_code,
            )
            return
        log_event(log, logging.DEBUG, "remote.action_forwarded", action=action)


def main(argv: list[str] | None = None) -> int:
    paths.ensure_dirs()
    config = get_config()
    setup_logging(config.str_("LOG_LEVEL"), config.str_("LOG_FORMAT"))

    if not config.bool_("POLY_REMOTE_ENABLED"):
        log_event(log, logging.INFO, "remote.disabled_waiting")
        # Stay alive so the unit is "active" and picks up the setting when the
        # administrator turns it on and restarts this service.
        _sleep_forever()
        return 0

    service = RemoteService(config, ActionForwarder())
    service.start()

    stop = threading.Event()

    def shutdown(signum, _frame):  # pragma: no cover - signal path
        log_event(log, logging.INFO, "remote.stopping", signal=int(signum))
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):
            pass

    log_event(log, logging.INFO, "remote.runner_started",
              mappings=len(service.mappings()))
    while not stop.is_set():
        stop.wait(timeout=5)
    service.stop()
    return 0


def _sleep_forever() -> None:
    event = threading.Event()

    def shutdown(_signum, _frame):  # pragma: no cover
        event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):
            pass
    while not event.is_set():
        event.wait(timeout=30)


if __name__ == "__main__":
    sys.exit(main())
