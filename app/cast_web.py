"""The listeners a laptop uses to put its screen on the TV.

Two small Flask applications on two ports of their own, and that separation is
the point rather than an accident.

**Why not a few more routes on the dashboard?** The dashboard's port only opens
to the network when an administrator asks for it, and once it is open the QR
code on the TV pairs a phone as the room's administrator. Screen sharing has to
be reachable by anyone who walks in, so putting it there would mean opening the
room's administration to the network as a side effect of turning on sharing.
Instead these listeners are always open and carry the sharing page and nothing
else — no settings, no restarts, no logs, no controller pairing, no calendar.

**Why two ports?**

* ``CAST_PORT`` is plain HTTP, and it is the only address a person ever sees.
  It serves one page: the short explanation of the certificate warning that is
  about to appear, and a button to go on. Plain HTTP so that typing
  ``192.168.1.42:8000`` — with no ``https://`` in front, which nobody types —
  reaches something instead of failing on a TLS handshake.
* ``CAST_SECURE_PORT`` is HTTPS, and serves the page that does the capturing.
  It has to be encrypted: browsers do not offer ``getDisplayMedia`` to a page
  that is not in a secure context, however private the network is.

**No cookies, no CSRF token.** Every signalling endpoint is authorised by
holding a session id handed out by ``POST /api/cast/start`` — a secret the
caller was given, not ambient authority a browser attaches by itself. There is
nothing for a cross-site request to forge: a hostile page can send the request
but cannot know the id, and without it every endpoint refuses.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request

from . import __version__, paths
from .cast_service import MAX_SIGNAL_BYTES
from .logging_setup import get_logger, log_event
from .tls import ensure_certificate
from .web_security import client_address, pin_guard

log = get_logger("cast")

#: Longest a poll may be held open. Kept short: the point is a fast handshake,
#: not a long-lived connection, and a held request is a held thread.
POLL_WAIT_SECONDS = 2.0


def _payload() -> dict[str, Any]:
    return request.get_json(silent=True) or {}


def _too_big(value: Any) -> bool:
    """True when a signalling blob is larger than any real one could be."""
    try:
        return len(json.dumps(value)) > MAX_SIGNAL_BYTES
    except (TypeError, ValueError):
        return True


def _harden(app: Flask) -> None:
    """Response headers for both applications."""

    @app.after_request
    def headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; "
            # Sending the screen is a peer connection, which connect-src does
            # not govern. Browsers that predate the directive ignore it.
            "webrtc 'allow'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response


def _base_app(name: str) -> Flask:
    app = Flask(
        f"{__name__}.{name}",
        template_folder=str(paths.TEMPLATES_DIR),
        static_folder=str(paths.STATIC_DIR),
        static_url_path="/static",
    )
    app.config.update(
        JSON_SORT_KEYS=False,
        # Nothing here accepts an upload; signalling messages are kilobytes.
        MAX_CONTENT_LENGTH=256 * 1024,
    )
    _harden(app)
    return app


# ---------------------------------------------------------------------------
# The plain-HTTP entry point
# ---------------------------------------------------------------------------


def create_entry_app(appliance) -> Flask:
    """One page: what the browser is about to say, and a button to continue."""
    config = appliance.config
    app = _base_app("entry")

    def secure_url() -> str:
        """The HTTPS address, keeping whatever host the visitor typed.

        Reusing their own host matters: somebody who typed ``room.local`` must
        be sent to ``https://room.local``, or the certificate that covers that
        name is checked against an address instead and they get a second,
        different warning.
        """
        host = (request.host or "").rsplit(":", 1)[0]
        if not host:
            return ""
        return f"https://{host}:{config.int_('CAST_SECURE_PORT')}/"

    @app.route("/")
    @app.route("/share")
    @app.route("/cast")
    def entry():
        blocked = ""
        if not appliance.cast.tls_ready():
            blocked = (
                "The room could not create the certificate a browser needs "
                "before it will share a screen. Check the room's logs, or ask "
                "whoever looks after it to run the installer again."
            )
        return render_template(
            "cast_start.html",
            room_name=config.str_("ROOM_NAME"),
            theme=config.str_("THEME"),
            accent=config.str_("ACCENT_COLOR"),
            airplay_name=config.airplay_name(),
            cast_enabled=config.bool_("CAST_ENABLED"),
            secure_url="" if blocked else secure_url(),
            blocked_reason=blocked,
            version=__version__,
        )

    @app.errorhandler(404)
    def not_found(_error):
        # Anything else typed against this port lands on the one page it has.
        return redirect("/")

    return app


# ---------------------------------------------------------------------------
# The HTTPS sender page and its signalling
# ---------------------------------------------------------------------------


def create_cast_app(appliance) -> Flask:
    """The page that captures the screen, and the five endpoints it talks to."""
    config = appliance.config
    cast = appliance.cast
    app = _base_app("sender")

    def ok(**fields: Any):
        return jsonify({"ok": True, **fields})

    def fail(message: str, status: int = 400, **fields: Any):
        return jsonify({"ok": False, "error": message, **fields}), status

    # -- the page --------------------------------------------------------
    @app.route("/")
    def sender_page():
        return render_template(
            "cast.html",
            room_name=config.str_("ROOM_NAME"),
            theme=config.str_("THEME"),
            accent=config.str_("ACCENT_COLOR"),
            pin_required=bool(config.str_("CAST_PIN")),
            enabled=cast.enabled,
            version=__version__,
        )

    @app.route("/cast")
    @app.route("/share")
    def sender_aliases():
        # Whatever somebody typed or bookmarked, they land on the one page.
        return redirect("/")

    # -- signalling ------------------------------------------------------
    @app.route("/api/cast/start", methods=["POST"])
    def api_start():
        if not cast.enabled:
            return fail("Screen sharing from a PC is switched off in this room.", 409)

        address = client_address()
        remaining = pin_guard.locked_out(address)
        if remaining > 0:
            return fail(f"Too many attempts. Try again in {int(remaining)} seconds.", 429)

        body = _payload()
        if not cast.check_pin(str(body.get("pin") or "")):
            pin_guard.record_failure(address)
            log_event(log, logging.WARNING, "cast.pin_rejected", address=address)
            return fail("Wrong sharing code for this room.", 403, needs_pin=True)
        pin_guard.clear(address)

        blocked = appliance.room.sharing_refusal()
        if blocked:
            return fail(blocked, 409)

        # The label is the sender's own description of itself, shown on the TV
        # and in the log. Treated as hostile text: trimmed here, escaped where
        # it is rendered.
        label = str(body.get("label") or "").strip()[:60]
        return ok(**cast.start_session(client=label))

    @app.route("/api/cast/offer", methods=["POST"])
    def api_offer():
        body = _payload()
        offer = body.get("sdp")
        if not isinstance(offer, dict) or _too_big(offer):
            return fail("That is not a usable connection offer.")
        if not cast.submit_offer(str(body.get("session") or ""), offer):
            return _ended(str(body.get("session") or ""))
        return ok()

    @app.route("/api/cast/candidate", methods=["POST"])
    def api_candidate():
        body = _payload()
        candidate = body.get("candidate")
        if not isinstance(candidate, dict) or _too_big(candidate):
            return fail("That is not a usable network candidate.")
        if not cast.submit_candidate(
            str(body.get("session") or ""), candidate, from_sender=True
        ):
            return _ended(str(body.get("session") or ""))
        return ok()

    def _ended(session_id: str):
        """Tell the laptop why it stopped, not merely that it did.

        "The TV is in a meeting, share inside the meeting instead" is worth
        far more to somebody standing in a room than "press Share again".
        """
        reason = cast.why_ended(session_id)
        if reason:
            return fail(f"Sharing stopped: {reason}", 409, ended=True)
        return fail("This sharing session has ended. Press Share again.", 409, ended=True)

    @app.route("/api/cast/poll")
    def api_poll():
        session_id = str(request.args.get("session") or "")
        deadline = time.monotonic() + POLL_WAIT_SECONDS
        while True:
            result = cast.poll_sender(session_id)
            if result is None:
                return _ended(session_id)
            if result["messages"] or time.monotonic() >= deadline:
                return ok(**result)
            # Nothing yet. Holding the request briefly is what makes the
            # handshake feel instant without polling in a tight loop.
            time.sleep(0.1)

    @app.route("/api/cast/stop", methods=["POST"])
    def api_stop():
        cast.stop_session(str(_payload().get("session") or ""))
        # Always a success: the caller wanted the session gone, and it is.
        return ok()

    @app.errorhandler(404)
    def not_found(_error):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "No such endpoint."}), 404
        return redirect("/")

    @app.errorhandler(500)
    def server_error(_error):
        log.exception("cast.unhandled_error", extra={"fields": {"path": request.path}})
        return jsonify({"ok": False, "error": "The room software hit an error."}), 500

    return app


# ---------------------------------------------------------------------------
# The listeners
# ---------------------------------------------------------------------------


class CastServer:
    """Runs both cast applications in background threads.

    Every failure here is reported and swallowed. Sharing from a PC is a
    convenience; the calendar, the join button and AirPlay are the appliance,
    and none of them may be taken down by a busy port or a missing certificate.
    """

    def __init__(self, appliance) -> None:
        self.appliance = appliance
        self._servers: list = []
        self._error = ""
        self._port = 0
        self._secure_port = 0
        # Starting and stopping are serialised against each other. Without
        # this, two settings changes in quick succession overlap: one is still
        # releasing its ports while the next is already trying to bind them,
        # and sharing stays down until somebody changes a setting again.
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return bool(self._servers)

    @property
    def error(self) -> str:
        return self._error

    @property
    def port(self) -> int:
        """The port in the address shown on the TV, or 0 when not listening."""
        return self._port

    @property
    def secure_port(self) -> int:
        return self._secure_port

    def restart(self) -> bool:
        """Close the listeners and open them again on the current settings.

        One locked operation rather than ``stop()`` then ``start()``, so two
        callers cannot interleave the halves.
        """
        with self._lock:
            self._stop_locked()
            return self._start_locked()

    def certificate_names(self) -> list[str]:
        """The addresses the certificate should cover: the room's own."""
        system = self.appliance.system
        names = list(system.local_ip_addresses())
        hostname = system.hostname()
        if hostname:
            names.append(hostname)
            if "." not in hostname:
                # Avahi already publishes this, and it is far easier to type
                # than an address that moves with the DHCP lease.
                names.append(f"{hostname}.local")
        return names

    def start(self) -> bool:
        with self._lock:
            return self._start_locked()

    def _start_locked(self) -> bool:
        config = self.appliance.config
        # A fresh attempt starts with a clean slate, or a stale message from a
        # previous failure would sit on the dashboard forever.
        self._error = ""
        self._port = 0
        self._secure_port = 0

        if not config.bool_("CAST_ENABLED"):
            log_event(log, logging.INFO, "cast.disabled_by_configuration")
            self.appliance.cast.note_listeners(running=False)
            return False

        certificate = ensure_certificate(
            self.certificate_names(), common_name=config.str_("ROOM_NAME")
        )
        if certificate is None:
            self._error = (
                "No certificate could be generated, so the sharing page cannot be "
                "served securely — and browsers will not share a screen without "
                "that. Check that openssl is installed."
            )
            self.appliance.cast.note_listeners(running=False, error=self._error)
            return False

        secure_port = config.int_("CAST_SECURE_PORT")
        entry_port = config.int_("CAST_PORT")
        if secure_port == entry_port:
            self._error = "The two sharing ports must differ."
            log_event(log, logging.ERROR, "cast.ports_identical", port=entry_port)
            self.appliance.cast.note_listeners(running=False, error=self._error)
            return False

        secure = self._listen(
            secure_port, create_cast_app(self.appliance), certificate, "cast-https"
        )
        if secure is None:
            self.appliance.cast.note_listeners(running=False, error=self._error)
            return False

        # The plain-HTTP page is what makes the address on the TV typeable, but
        # sharing still works without it for anyone who has the HTTPS address,
        # so a busy port here is a warning rather than a failure.
        entry = self._listen(
            entry_port, create_entry_app(self.appliance), None, "cast-http"
        )

        self._secure_port = secure_port
        self._port = entry_port if entry is not None else 0
        self.appliance.cast.note_listeners(running=True, error=self._error)
        log_event(
            log, logging.INFO, "cast.listening",
            port=self._port, secure_port=secure_port,
            names=",".join(self.certificate_names()),
        )
        return True

    def _listen(self, port: int, app: Flask, certificate, name: str):
        from werkzeug.serving import make_server

        try:
            # Always every interface: a sharing page reachable only from the Pi
            # itself would have no purpose. What makes that safe is that these
            # applications carry the sharing page and nothing else — see the
            # module docstring.
            server = make_server(
                "0.0.0.0",  # noqa: S104 - deliberate, see above
                port,
                app,
                threaded=True,
                ssl_context=certificate,
            )
        except SystemExit:
            # Werkzeug prints an explanation and then calls sys.exit(1) when it
            # cannot bind. SystemExit is a BaseException, so without naming it
            # here a room where something else already holds this port would
            # not merely lose screen sharing — the whole appliance would exit
            # at boot, taking the calendar, the join button and AirPlay with it.
            message = f"Port {port} could not be opened: it is already in use."
            log_event(log, logging.ERROR, "cast.listen_failed", port=port,
                      error="address already in use",
                      hint=f"run 'ss -tlnp | grep :{port}' to see what holds it, "
                           f"or change the port in Settings")
            self._error = self._error or message
            return None
        except OSError as exc:
            message = f"Port {port} could not be opened: {exc}"
            log_event(log, logging.ERROR, "cast.listen_failed", port=port,
                      error=str(exc), hint=f"run 'ss -tlnp | grep :{port}' to see what holds it")
            self._error = self._error or message
            return None
        except Exception as exc:  # pragma: no cover - an unreadable certificate
            self._error = self._error or f"Secure sharing could not start: {exc}"
            log_event(log, logging.ERROR, "cast.tls_failed", error=str(exc))
            return None

        thread = threading.Thread(
            target=self._serve, args=(server, name), name=name, daemon=True
        )
        thread.start()
        self._servers.append(server)
        return server

    @staticmethod
    def _serve(server, name: str) -> None:
        try:
            server.serve_forever()
        except Exception:  # pragma: no cover - only on shutdown
            log.exception("cast.server_stopped", extra={"fields": {"listener": name}})

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        servers, self._servers = self._servers, []
        self._port = 0
        self._secure_port = 0
        for server in servers:
            try:
                server.shutdown()
            except Exception:  # pragma: no cover - a server already stopping
                log.debug("cast.shutdown_failed", exc_info=True)
            try:
                # Attempted even if shutdown() just failed, and in its own
                # block for that reason: shutdown() ends the serving loop but
                # does NOT release the socket — only this does. Skipping it
                # would leak a listening socket on every settings change, and
                # the next start would find its own port already taken.
                server.server_close()
            except Exception:  # pragma: no cover
                log.debug("cast.close_failed", exc_info=True)
        if servers:
            self.appliance.cast.note_listeners(running=False)
