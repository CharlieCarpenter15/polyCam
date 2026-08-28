"""Access control for the web interface.

The rules, in plain terms:

* **The Pi itself is trusted.** Requests from 127.0.0.1 are the kiosk browser and
  local scripts; they never need a PIN.
* **Anything else needs the admin PIN** — and can only reach the server at all
  when ``ADMIN_LAN_ACCESS`` is on, which the configuration layer refuses to
  enable without a PIN being set.
* **Every state-changing request needs a token** that only a page served by this
  app can know (``X-Room-Token``). A form on someone else's website cannot set a
  custom header, so this closes cross-site request forgery without a framework.
* **Background services** (the AirPlay supervisor, the watchdog) authenticate
  with a shared secret from a root-readable file instead of a cookie, so they are
  unaffected by the above.
* **The room controller** is a deliberately weaker, narrower role. Whoever can
  see the TV can scan the code in its corner and press Join, Leave, Mute,
  Camera and Volume from their phone — the same buttons a physical remote has,
  and nothing more. It never reaches settings, restarts, logs or the
  configuration, so the worst a stranger with a long lens can do is hang up a
  call in a room they are looking at. Turn on ``CONTROLLER_REQUIRE_PIN`` where
  even that is too much.

PIN checks use a constant-time comparison and are rate-limited per client.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from functools import wraps
from typing import Any, Callable

from flask import current_app, jsonify, redirect, request, session, url_for

from . import paths
from .logging_setup import get_logger, log_event

log = get_logger("web")

LOCAL_ADDRESSES = frozenset({"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"})

#: Failed PIN attempts allowed per client before a cooling-off period.
MAX_PIN_ATTEMPTS = 6
PIN_LOCKOUT_SECONDS = 120.0

_SECRET_KEY_FILE = paths.VAR_DIR / "flask-secret-key"
_INTERNAL_TOKEN_FILE = paths.VAR_DIR / "internal-token"

#: Session key marking a phone that scanned the room's QR code.
CONTROLLER_SESSION_KEY = "controller"


# ---------------------------------------------------------------------------
# Secrets on disk
# ---------------------------------------------------------------------------


def _read_or_create_secret(path, length: int = 32) -> str:
    """Load a secret, creating it on first run. Falls back to memory-only."""
    try:
        if path.exists():
            value = path.read_text(encoding="ascii").strip()
            if len(value) >= 16:
                return value
    except OSError:
        pass

    value = secrets.token_urlsafe(length)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="ascii")
        path.chmod(0o600)
    except OSError as exc:
        # A read-only disk means sessions reset on restart; that is acceptable,
        # refusing to start would not be.
        log_event(log, logging.WARNING, "web.secret_not_persisted", error=str(exc))
    return value


def flask_secret_key() -> str:
    return _read_or_create_secret(_SECRET_KEY_FILE)


def internal_token() -> str:
    """Shared secret for ``/api/internal/*`` (helper scripts)."""
    return _read_or_create_secret(_INTERNAL_TOKEN_FILE, 24)


def _controller_token_file():
    """Resolved late so the tests can point ``VAR_DIR`` somewhere temporary."""
    return paths.VAR_DIR / "controller-token"


def controller_token() -> str:
    """The secret inside the QR code shown on the TV.

    It is a room secret rather than a personal one: everybody who scans the
    code gets the same one, and it only ever unlocks the room-control buttons.
    """
    return _read_or_create_secret(_controller_token_file(), 18)


def rotate_controller_token() -> str:
    """Issue a new pairing code, invalidating every phone paired so far."""
    try:
        _controller_token_file().unlink()
    except OSError:
        pass
    session.pop(CONTROLLER_SESSION_KEY, None)
    token = controller_token()
    log_event(log, logging.INFO, "web.controller_token_rotated")
    return token


# ---------------------------------------------------------------------------
# Request classification
# ---------------------------------------------------------------------------


def client_address() -> str:
    """The peer address. Proxy headers are ignored on purpose — trusting
    ``X-Forwarded-For`` here would let a remote client claim to be localhost."""
    return request.remote_addr or ""


def is_local_request() -> bool:
    return client_address() in LOCAL_ADDRESSES


def csrf_token() -> str:
    """Per-session token embedded in every page this app serves."""
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        session["csrf"] = token
        session.permanent = True
    return token


def check_csrf() -> bool:
    expected = session.get("csrf") or ""
    provided = request.headers.get("X-Room-Token", "")
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected, provided)


# ---------------------------------------------------------------------------
# PIN handling
# ---------------------------------------------------------------------------


class PinGuard:
    """Rate-limits PIN attempts per client address."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts: dict[str, list[float]] = {}

    def locked_out(self, address: str) -> float:
        """Seconds remaining in a lockout, or 0."""
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._attempts.get(address, []) if now - t < PIN_LOCKOUT_SECONDS]
            self._attempts[address] = recent
            if len(recent) < MAX_PIN_ATTEMPTS:
                return 0.0
            return round(PIN_LOCKOUT_SECONDS - (now - recent[0]), 1)

    def record_failure(self, address: str) -> None:
        with self._lock:
            self._attempts.setdefault(address, []).append(time.monotonic())

    def clear(self, address: str) -> None:
        with self._lock:
            self._attempts.pop(address, None)


pin_guard = PinGuard()


def verify_pin(submitted: str) -> tuple[bool, str]:
    """Check a submitted PIN. Returns ``(ok, message)``."""
    config = current_app.config["ROOM_CONFIG"]
    expected = config.str_("ADMIN_PIN")
    address = client_address()

    if not expected:
        return False, "No admin PIN has been set on this room."

    remaining = pin_guard.locked_out(address)
    if remaining > 0:
        return False, f"Too many attempts. Try again in {int(remaining)} seconds."

    if hmac.compare_digest(str(submitted or ""), expected):
        pin_guard.clear(address)
        session["admin"] = True
        session.permanent = True
        csrf_token()
        log_event(log, logging.INFO, "web.admin_signed_in", address=address)
        return True, "Signed in."

    pin_guard.record_failure(address)
    log_event(log, logging.WARNING, "web.admin_pin_rejected", address=address)
    return False, "Incorrect PIN."


def pair_controller(submitted: str) -> bool:
    """Accept a scanned pairing code and remember the phone for next time."""
    address = client_address()
    if pin_guard.locked_out(address) > 0:
        log_event(log, logging.WARNING, "web.controller_pairing_throttled",
                  address=address)
        return False

    if not hmac.compare_digest(str(submitted or ""), controller_token()):
        pin_guard.record_failure(address)
        log_event(log, logging.WARNING, "web.controller_code_rejected", address=address)
        return False

    pin_guard.clear(address)
    session[CONTROLLER_SESSION_KEY] = True
    session.permanent = True
    csrf_token()
    log_event(log, logging.INFO, "web.controller_paired", address=address)
    return True


def is_admin() -> bool:
    """True when this request may change things."""
    if is_local_request():
        return True
    return bool(session.get("admin"))


def is_controller() -> bool:
    """True when this request may press the room-control buttons.

    Admins (and the kiosk itself) always can; a paired phone can as well,
    unless the room has been set to demand the PIN from everyone.
    """
    if is_admin():
        return True
    config = current_app.config.get("ROOM_CONFIG")
    if config is not None and config.bool_("CONTROLLER_REQUIRE_PIN"):
        return False
    return bool(session.get(CONTROLLER_SESSION_KEY))


def admin_needed() -> bool:
    """True when the caller must sign in before continuing."""
    return not is_admin()


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------


def wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def require_admin(view: Callable[..., Any]) -> Callable[..., Any]:
    """Refuse the request unless it is local or signed in."""

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if admin_needed():
            if wants_json():
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": "Sign in with the room's admin PIN.",
                            "needs_pin": True,
                        }
                    ),
                    401,
                )
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


def require_controller(view: Callable[..., Any]) -> Callable[..., Any]:
    """Allow the kiosk, an admin, or a phone that scanned the room's code."""

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if is_controller():
            return view(*args, **kwargs)
        if wants_json():
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Scan the code on the TV to control this room.",
                        "needs_pairing": True,
                    }
                ),
                401,
            )
        return redirect(url_for("controller_locked"))

    return wrapper


def require_csrf(view: Callable[..., Any]) -> Callable[..., Any]:
    """Refuse a state-changing request without the page token."""

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return view(*args, **kwargs)
        if not check_csrf():
            log_event(
                log, logging.WARNING, "web.csrf_rejected",
                path=request.path, address=client_address(),
            )
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "This page is out of date. Reload it and try again.",
                        "reload": True,
                    }
                ),
                403,
            )
        return view(*args, **kwargs)

    return wrapper


def require_internal(view: Callable[..., Any]) -> Callable[..., Any]:
    """Localhost plus the shared token — for helper scripts, not browsers."""

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if not is_local_request():
            return jsonify({"ok": False, "error": "Local requests only."}), 403
        provided = request.headers.get("X-Room-Internal-Token", "")
        if not provided or not hmac.compare_digest(provided, internal_token()):
            return jsonify({"ok": False, "error": "Bad internal token."}), 403
        return view(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


def lan_access_enabled(config) -> bool:
    """True when phones on the room's network can reach this server at all."""
    return bool(
        config.bool_("ADMIN_LAN_ACCESS")
        or (config.bool_("CONTROLLER_ENABLED") and config.bool_("CONTROLLER_LAN_ACCESS"))
    )


def effective_bind_host(config) -> str:
    """Where the server should listen.

    Localhost unless the administrator has explicitly opened the room up —
    either for settings from a laptop (which needs a PIN), or for the phone
    controller (which needs the QR code and can only press the room buttons).
    """
    if lan_access_enabled(config):
        configured = config.str_("DASHBOARD_HOST")
        # A specific address stays specific; the default widens to all interfaces.
        return "0.0.0.0" if configured in ("127.0.0.1", "localhost", "") else configured
    return config.str_("DASHBOARD_HOST") or "127.0.0.1"
