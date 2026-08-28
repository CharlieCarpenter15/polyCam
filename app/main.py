"""The room appliance web application and its entry point.

Run it directly for development::

    python3 -m app.main --dev

On the appliance it is started by ``room-dashboard.service``. Everything is one
process with a handful of daemon threads (calendar refresh, room state machine,
health monitor, Poly monitor, optional remote listener), which keeps debugging
on a Raspberry Pi straightforward: one unit, one log, one place to look.
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import signal
import sys
import threading
import time
from datetime import timedelta
from typing import Any

from flask import (
    Flask,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from . import __version__, paths
from .airplay_service import AirPlayService
from .background_service import MAX_VIDEO_BYTES, BackgroundService
from .browser_service import BrowserService
from .calendar_service import CalendarService
from .config import ConfigManager, advisories, get_config
from .config_schema import FIELDS_BY_KEY, SECRET_KEYS, grouped_fields
from .health_service import HealthService
from .join_flows import PROVIDER_FLOWS
from .logging_setup import get_logger, log_event, setup_logging
from .meeting_service import MeetingService
from .minutes import MinutesService
from .minutes.web import minutes_bp
from .models import MODES
from .poly_service import PolyService
from .remote_service import ACTIONS, RemoteService
from .system_service import MANAGED_UNITS, SystemService
from .web_security import (
    check_csrf,
    controller_token,
    csrf_token,
    effective_bind_host,
    internal_token,
    is_admin,
    is_controller,
    is_local_request,
    lan_access_enabled,
    pair_controller,
    require_admin,
    require_controller,
    require_csrf,
    require_internal,
    rotate_controller_token,
    flask_secret_key,
    verify_pin,
)

log = get_logger("web")


class RoomAppliance:
    """Wires the services together and owns their lifecycle."""

    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        self.system = SystemService(config)
        self.calendar = CalendarService(config)
        self.browser = BrowserService(config, self.system)
        self.airplay = AirPlayService(config, self.system)
        self.poly = PolyService(config)
        self.backgrounds = BackgroundService()
        self.room = MeetingService(
            config, self.calendar, self.browser, self.airplay, self.poly, self.system
        )
        # Reads the room's state; never writes to it. Inert unless switched on.
        # Built before the health service so that it can be reported on.
        self.minutes = MinutesService(
            config, self.calendar, self.room, self.poly, self.browser
        )
        self.health = HealthService(
            config,
            self.calendar,
            self.browser,
            self.airplay,
            self.poly,
            self.room,
            self.system,
            minutes=self.minutes,
        )
        self.remote = RemoteService(config, self._on_remote_action)
        self._started = False
        config.on_change(self._on_config_change)

    def _on_remote_action(self, action: str) -> None:
        self.room.dispatch_action(action)

    def _on_config_change(self, values: dict[str, Any], changed: set[str]) -> None:
        if "LOG_LEVEL" in changed or "LOG_FORMAT" in changed:
            setup_logging(self.config.str_("LOG_LEVEL"), self.config.str_("LOG_FORMAT"))
            log_event(log, logging.INFO, "logging.reconfigured",
                      level=self.config.str_("LOG_LEVEL"))
        if "POLY_REMOTE_ENABLED" in changed:
            if self.config.bool_("POLY_REMOTE_ENABLED"):
                self.remote.start()
            else:
                self.remote.stop()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        log_event(
            log,
            logging.INFO,
            "application.started",
            version=__version__,
            room=self.config.str_("ROOM_NAME"),
            dev_mode=self.config.bool_("DEV_MODE"),
            calendar_source=self.config.str_("CALENDAR_SOURCE"),
        )
        self.calendar.start()
        self.poly.start()
        self.room.start()
        self.health.start()
        self.remote.start()
        self.minutes.start()

    def stop(self) -> None:
        log_event(log, logging.INFO, "application.stopping")
        for service in (
            self.minutes, self.remote, self.health, self.room, self.poly, self.calendar
        ):
            try:
                service.stop()
            except Exception:  # pragma: no cover
                log.exception("application.stop_failed")


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------


def create_app(config: ConfigManager | None = None, *, start_services: bool = True) -> Flask:
    config = config or get_config()
    setup_logging(config.str_("LOG_LEVEL"), config.str_("LOG_FORMAT"))
    paths.ensure_dirs()

    app = Flask(
        __name__,
        template_folder=str(paths.TEMPLATES_DIR),
        static_folder=str(paths.STATIC_DIR),
        static_url_path="/static",
    )
    appliance = RoomAppliance(config)

    app.config.update(
        SECRET_KEY=flask_secret_key(),
        ROOM_CONFIG=config,
        ROOM_APPLIANCE=appliance,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        JSON_SORT_KEYS=False,
        # Cap request bodies so an upload cannot exhaust memory or disk. The
        # cap has to clear the largest thing the slideshow accepts — a video —
        # or Flask rejects it before background_service can say why.
        MAX_CONTENT_LENGTH=MAX_VIDEO_BYTES + 4 * 1024 * 1024,
        TEMPLATES_AUTO_RELOAD=config.bool_("DEV_MODE"),
    )

    register_routes(app, appliance)
    # A blueprint rather than more routes here: the feature is self-contained,
    # and when it is switched off every one of its routes answers as though it
    # did not exist.
    app.register_blueprint(minutes_bp)

    if start_services:
        appliance.start()
    return app


def register_routes(app: Flask, appliance: RoomAppliance) -> None:  # noqa: C901
    config = appliance.config

    # -- shared helpers --------------------------------------------------
    def ok(**payload: Any):
        return jsonify({"ok": True, **payload})

    def fail(message: str, status: int = 400, **payload: Any):
        return jsonify({"ok": False, "error": message, **payload}), status

    def template_context() -> dict[str, Any]:
        return {
            "config": config,
            "version": __version__,
            "csrf": csrf_token(),
            "is_admin": is_admin(),
            "is_local": is_local_request(),
            "room_name": config.str_("ROOM_NAME"),
            "theme": config.str_("THEME"),
            "accent": config.str_("ACCENT_COLOR"),
            "dev_mode": config.bool_("DEV_MODE"),
            "setup_required": config.setup_required(),
            "panel_enabled": config.bool_("PANEL_ENABLED"),
            "controller_enabled": config.bool_("CONTROLLER_ENABLED"),
        }

    @app.after_request
    def security_headers(response):
        # A kiosk page needs no third-party anything; lock it down.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; font-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # -- pages -----------------------------------------------------------
    @app.route("/")
    def index():
        """The room dashboard shown on the TV."""
        return render_template("index.html", **template_context())

    @app.route("/panel")
    def panel():
        """Phone-friendly control panel."""
        if not config.bool_("PANEL_ENABLED"):
            return render_template("disabled.html", **template_context()), 404
        if not is_admin():
            return redirect(url_for("login", next="/panel"))
        return render_template("panel.html", **template_context())

    #: What ``disabled.html`` should say for each page that can be switched off.
    DISABLED_TEXT = {
        "controller": (
            "The room controller",
            "Room controller (phone) &rarr; <em>Enable the room controller</em>",
        ),
        "minutes": (
            "Meeting minutes",
            "Meeting minutes &rarr; <em>Record and write up meetings</em>",
        ),
    }

    def disabled_page(which: str = ""):
        context = template_context()
        what, where = DISABLED_TEXT.get(which, ("", ""))
        if what:
            context.update(disabled_what=what, disabled_where=where)
        return render_template("disabled.html", **context), 404

    @app.route("/controller")
    def controller_page():
        """The big-button page a phone gets by scanning the code on the TV."""
        if not config.bool_("CONTROLLER_ENABLED"):
            return disabled_page("controller")
        if not is_controller():
            return redirect(url_for("controller_locked"))
        context = template_context()
        context.update(
            airplay_name=config.airplay_name(),
            remote_enabled=config.bool_("POLY_REMOTE_ENABLED"),
            guest=not is_admin(),
        )
        return render_template("controller.html", **context)

    @app.route("/c/<token>")
    def controller_pair(token: str):
        """Where the QR code on the TV points. Pairs the phone, then gets out of
        the way: from here on the phone can just open /controller."""
        if not config.bool_("CONTROLLER_ENABLED"):
            return disabled_page("controller")
        if not pair_controller(token):
            return redirect(url_for("controller_locked"))
        if config.bool_("CONTROLLER_REQUIRE_PIN") and not is_admin():
            return redirect(url_for("login", next="/controller"))
        return redirect(url_for("controller_page"))

    @app.route("/controller/locked")
    def controller_locked():
        """Shown when a phone reaches the controller without a valid code."""
        context = template_context()
        context.update(pin_required=config.bool_("CONTROLLER_REQUIRE_PIN"))
        return render_template("controller_locked.html", **context), 403

    @app.route("/settings")
    @require_admin
    def settings_page():
        context = template_context()
        context.update(
            groups=grouped_fields(),
            values=config.as_dict(redact=True),
            advisories=advisories(config.as_dict()),
            env_locked=config.env_locked_keys(),
            config_path=str(config.file),
            warnings=list(config.warnings),
        )
        return render_template("settings.html", **context)

    @app.route("/diagnostics")
    @require_admin
    def diagnostics_page():
        context = template_context()
        context.update(units=MANAGED_UNITS)
        return render_template("diagnostics.html", **context)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        target = request.args.get("next") or request.form.get("next") or "/panel"
        if not target.startswith("/") or target.startswith("//"):
            target = "/panel"  # never redirect off-site

        if is_admin():
            return redirect(target)

        message = ""
        if request.method == "POST":
            success, message = verify_pin(request.form.get("pin", ""))
            if success:
                return redirect(target)

        context = template_context()
        context.update(message=message, next=target,
                       pin_set=bool(config.str_("ADMIN_PIN")))
        return render_template("login.html", **context), (
            200 if request.method == "GET" else 401
        )

    @app.route("/logout", methods=["POST"])
    @require_csrf
    def logout():
        session.pop("admin", None)
        return ok(signed_out=True)

    # -- read-only API ---------------------------------------------------
    @app.route("/api/state")
    def api_state():
        """Everything the dashboard renders, in one call."""
        payload = appliance.room.dashboard_payload()
        media = appliance.backgrounds.list_media()
        payload["backgrounds"] = {
            "mode": config.str_("BACKGROUND_MODE"),
            "seconds": config.int_("BACKGROUND_SLIDESHOW_SECONDS"),
            "shuffle": config.bool_("BACKGROUND_SHUFFLE"),
            "dim": config.int_("BACKGROUND_DIM_PERCENT"),
            "blur": config.int_("BACKGROUND_BLUR_PIXELS"),
            "solid": config.str_("BACKGROUND_SOLID_COLOR"),
            # "images" stays what it always was — URLs that can be painted as a
            # CSS background — so a still slideshow never has to know videos
            # exist. "media" is the full list, in order, for the player.
            "images": [item.to_dict()["url"] for item in media if not item.is_video],
            "media": [item.to_dict() for item in media],
            "video_sound": config.bool_("BACKGROUND_VIDEO_SOUND"),
        }
        payload["display"] = {
            "show_instructions": config.bool_("SHOW_SHARING_INSTRUCTIONS"),
            "show_status": config.bool_("SHOW_STATUS_INDICATORS"),
            # How often to come back. A capable machine can afford to ask more
            # often; a Pi 3 has better things to do.
            "poll_ms": config.performance().poll_ms,
            "theme": config.str_("THEME"),
            "accent": config.str_("ACCENT_COLOR"),
            "show_panel_url": config.bool_("PANEL_SHOW_URL_ON_TV"),
        }
        health = appliance.health.report()
        payload["status"] = {
            "overall": health["status"],
            "components": health["components"],
        }
        payload["minutes"] = appliance.minutes.dashboard_payload()
        payload["panel_url"] = _panel_url()
        payload["controller"] = _controller_hint()
        payload["setup"] = _setup_hint()
        payload["version"] = __version__
        return jsonify(payload)

    def _setup_hint() -> dict[str, Any]:
        """What the first-run overlay on the TV should say.

        The admin PIN is included ONLY for a request from the Pi itself. The
        dashboard is deliberately viewable read-only from the LAN, so putting
        the PIN in the general payload would turn /api/state into a way to read
        it without ever signing in. The kiosk browser is on 127.0.0.1, so it
        gets it; nobody else does.

        It is also only useful while setup is unfinished — the overlay stops
        being shown the moment a calendar is configured — which keeps the PIN
        on screen for a short, purposeful window rather than permanently.
        """
        if not config.setup_required():
            return {"required": False}

        lan = config.bool_("ADMIN_LAN_ACCESS")
        hint: dict[str, Any] = {"required": True, "lan": lan}
        if lan and is_local_request():
            hint["pin"] = config.str_("ADMIN_PIN")
        return hint

    @app.route("/api/health")
    def api_health():
        report = appliance.health.report()
        report["units"] = {
            unit: appliance.system.unit_state(unit) for unit in MANAGED_UNITS
        }
        status_code = 200 if report["status"] != "error" else 503
        return jsonify(report), status_code

    # The dashboard polls /api/state every few seconds and asking the kernel for
    # the room's addresses is not free, so the answer is cached briefly.
    _lan_cache: dict[str, Any] = {"at": None, "addresses": []}

    def _lan_addresses() -> list[str]:
        now = time.monotonic()
        if _lan_cache["at"] is None or now - float(_lan_cache["at"]) > 30.0:
            _lan_cache["addresses"] = appliance.system.local_ip_addresses()
            _lan_cache["at"] = now
        return list(_lan_cache["addresses"])

    def _room_host() -> str:
        """``host:port`` a phone in the room can reach, else the loopback one."""
        # request.host reflects the port this request actually arrived on, which
        # is what the reader can reach. The configured port may differ after a
        # settings change that has not been restarted into yet, or when --port
        # was used.
        port = config.int_("DASHBOARD_PORT")
        try:
            if request.host and ":" in request.host:
                port = int(request.host.rsplit(":", 1)[1])
        except (ValueError, RuntimeError):
            pass
        addresses = _lan_addresses()
        if lan_access_enabled(config) and addresses:
            return f"{addresses[0]}:{port}"
        return f"127.0.0.1:{port}"

    def _panel_url() -> str:
        return f"http://{_room_host()}/panel"

    def _controller_url() -> str:
        """The address inside the QR code: pairs the phone and opens the page."""
        return f"http://{_room_host()}/c/{controller_token()}"

    def _controller_hint() -> dict[str, Any]:
        """What the corner of the TV should show.

        The pairing code itself only goes to the kiosk (127.0.0.1). The
        dashboard is deliberately readable from the LAN, and putting the code
        in that payload would hand it to anyone who loaded the page without
        ever looking at the room.
        """
        enabled = config.bool_("CONTROLLER_ENABLED")
        reachable = bool(lan_access_enabled(config) and _lan_addresses())
        hint: dict[str, Any] = {
            "enabled": enabled,
            "show_qr": enabled and config.bool_("CONTROLLER_QR_ON_TV"),
            "reachable": reachable,
        }
        if enabled and is_local_request():
            hint["host"] = _room_host()
            hint["url"] = _controller_url() if reachable else ""
            hint["qr_url"] = "/qr/controller.svg" if reachable else ""
        return hint

    @app.route("/qr/controller.svg")
    def qr_controller():
        """The pairing code as a scannable image, for the TV and the panel."""
        if not config.bool_("CONTROLLER_ENABLED"):
            return "Not found", 404
        # The image *is* the secret, so it is served to the room's own screen
        # and to signed-in administrators only.
        if not (is_local_request() or is_admin()):
            return "Not found", 404
        from .qr import qr_svg

        try:
            scale = max(1, min(16, int(request.args.get("scale", 4))))
        except (TypeError, ValueError):
            scale = 4
        response = make_response(qr_svg(_controller_url(), scale=scale))
        response.headers["Content-Type"] = "image/svg+xml"
        response.headers["Cache-Control"] = "no-store"
        return response

    # -- room actions ----------------------------------------------------
    @app.route("/api/actions/join", methods=["POST"])
    @require_admin
    @require_csrf
    def api_join():
        payload = request.get_json(silent=True) or {}
        meeting_id = str(payload.get("meeting_id") or "").strip()
        if meeting_id:
            success, detail = appliance.room.join_meeting_id(meeting_id)
        else:
            success, detail = appliance.room.join_next()
        return (ok(detail=detail) if success else fail(detail, 409))

    @app.route("/api/actions/leave", methods=["POST"])
    @require_admin
    @require_csrf
    def api_leave():
        appliance.room.leave_meeting(reason="requested from the interface")
        return ok(detail="Returned to the dashboard.")

    @app.route("/api/actions/home", methods=["POST"])
    @require_admin
    @require_csrf
    def api_home():
        appliance.room.go_home(reason="requested from the interface")
        return ok(detail="Showing the dashboard.")

    @app.route("/api/actions/retry-join", methods=["POST"])
    @require_admin
    @require_csrf
    def api_retry_join():
        if appliance.room.retry_join_automation():
            return ok(detail="Trying the join buttons again.")
        return fail("There is no meeting open on the TV.", 409)

    @app.route("/api/actions/refresh-calendar", methods=["POST"])
    @require_admin
    @require_csrf
    def api_refresh_calendar():
        appliance.calendar.refresh_now()
        return ok(detail="Calendar refresh requested.")

    @app.route("/api/actions/volume", methods=["POST"])
    @require_admin
    @require_csrf
    def api_volume():
        payload = request.get_json(silent=True) or {}
        if "level" in payload:
            try:
                level = int(payload["level"])
            except (TypeError, ValueError):
                return fail("Volume must be a number between 0 and 100.")
            success = appliance.poly.set_volume(level)
            return ok(volume=level) if success else fail("No speaker is available.", 409)
        try:
            delta = int(payload.get("delta", config.int_("POLY_VOLUME_STEP")))
        except (TypeError, ValueError):
            return fail("Volume change must be a number.")
        level = appliance.poly.adjust_volume(delta)
        return ok(volume=level) if level is not None else fail("No speaker is available.", 409)

    @app.route("/api/actions/mute", methods=["POST"])
    @require_admin
    @require_csrf
    def api_mute():
        payload = request.get_json(silent=True) or {}
        muted = payload.get("muted")
        result = appliance.poly.set_mute(None if muted is None else bool(muted))
        if result is None:
            return fail("No microphone is available.", 409)
        if appliance.room.state().active is not None:
            appliance.browser.toggle_meeting_mute()
        return ok(muted=result)

    @app.route("/api/actions/remote/<action>", methods=["POST"])
    @require_admin
    @require_csrf
    def api_remote_action(action: str):
        if action not in ACTIONS:
            return fail(f"Unknown action. Use one of: {', '.join(ACTIONS)}")
        result = appliance.room.dispatch_action(action)
        return jsonify({"ok": bool(result.get("ok")), **result})

    # -- the phone controller --------------------------------------------
    #
    # Its own two endpoints rather than the /api/actions/* family: those all
    # require an administrator, and a phone that only scanned the room's code
    # must never be able to reach settings, restarts or logs. The action list
    # below is the whole of what a controller can do.
    CONTROLLER_ACTIONS = frozenset(ACTIONS) | {"leave", "volume_set"}

    @app.route("/api/controller/state")
    @require_controller
    def api_controller_state():
        """Everything the controller renders, in one call."""
        payload = appliance.room.dashboard_payload()
        poly = appliance.poly.status()
        microphone = poly.get("microphone") or {}
        speaker = poly.get("speaker") or {}
        camera = poly.get("camera") or {}
        airplay = payload.get("airplay") or {}
        return jsonify(
            {
                "ok": True,
                "mode": payload["mode"],
                "room": payload["room"],
                "now": payload["now"],
                "time_format_24h": payload["time_format_24h"],
                "current": payload["current"],
                "next": payload["next"],
                "upcoming": payload["upcoming"],
                "active_meeting": payload["active_meeting"],
                "join_available": payload["join_available"],
                "calendar": payload["calendar"],
                "network_ok": payload["network_ok"],
                "setup_required": payload["setup_required"],
                "sharing": {
                    "active": bool(airplay.get("sharing")),
                    "client": airplay.get("client") or "",
                    "name": airplay.get("name") or "",
                },
                "audio": {
                    "muted": microphone.get("muted"),
                    "volume": speaker.get("volume"),
                    "microphone_ok": microphone.get("status") == "ok",
                    "speaker_ok": speaker.get("status") == "ok",
                    "camera_ok": camera.get("status") == "ok",
                },
                "remote": payload.get("remote"),
                "is_admin": is_admin(),
                "version": __version__,
            }
        )

    @app.route("/api/controller/action", methods=["POST"])
    @require_controller
    @require_csrf
    def api_controller_action():
        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action") or "").strip().lower()
        if action not in CONTROLLER_ACTIONS:
            return fail(f"Unknown action: {action}")

        if action == "join":
            meeting_id = str(payload.get("meeting_id") or "").strip()
            if meeting_id:
                success, detail = appliance.room.join_meeting_id(meeting_id)
                return ok(detail=detail) if success else fail(detail, 409)

        if action == "volume_set":
            try:
                level = int(payload.get("level"))
            except (TypeError, ValueError):
                return fail("Volume must be a number between 0 and 100.")
            level = max(0, min(100, level))
            if appliance.poly.set_volume(level):
                return ok(volume=level)
            return fail("No speaker is available.", 409)

        result = appliance.room.dispatch_action(action)
        return jsonify({"ok": bool(result.get("ok")), **result})

    @app.route("/api/actions/controller-code", methods=["POST"])
    @require_admin
    @require_csrf
    def api_controller_code():
        """Issue a new pairing code, e.g. after a visitor kept the old one."""
        rotate_controller_token()
        return ok(
            detail="New room code. Phones will need to scan the code on the TV again."
        )

    @app.route("/api/actions/airplay-simulate", methods=["POST"])
    @require_admin
    @require_csrf
    def api_airplay_simulate():
        if not config.bool_("DEV_MODE"):
            return fail("Only available in development mode.", 409)
        payload = request.get_json(silent=True) or {}
        appliance.airplay.simulate_sharing(bool(payload.get("sharing")))
        return ok(sharing=appliance.airplay.sharing)

    # -- restarts and recovery -------------------------------------------
    @app.route("/api/actions/restart", methods=["POST"])
    @require_admin
    @require_csrf
    def api_restart():
        payload = request.get_json(silent=True) or {}
        target = str(payload.get("target") or "").strip().lower()

        targets = {
            "browser": ("room-kiosk.service", "The TV display is restarting."),
            "airplay": ("room-airplay.service", "AirPlay is restarting."),
            "remote": ("room-remote.service", "The remote handler is restarting."),
            "backend": ("room-dashboard.service", "The room software is restarting."),
            "update": (
                "room-update.service",
                "Checking for a software update. The room restarts itself if "
                "there is one.",
            ),
        }

        if target == "all":
            # Order matters: the browser last, so it reloads a healthy backend.
            done = []
            for unit in ("room-airplay.service", "room-remote.service", "room-kiosk.service"):
                if appliance.system.restart(unit, min_interval=2.0, reason="restart everything"):
                    done.append(unit)
            appliance.calendar.refresh_now()
            appliance.poly.refresh_now()
            return ok(detail="Restarting the room.", restarted=done)

        if target not in targets:
            return fail(f"Unknown target. Use one of: {', '.join(targets)} or 'all'.")

        unit, message = targets[target]
        if target == "backend":
            # Answer first, then exit; systemd brings us straight back.
            _schedule_self_restart(appliance)
            return ok(detail=message)

        if appliance.system.restart(unit, min_interval=2.0, reason="requested from the interface"):
            return ok(detail=message)
        return fail(
            "That could not be restarted. It may have been restarted a moment ago, "
            "or systemd may not be managing it.",
            409,
        )

    @app.route("/api/actions/reboot", methods=["POST"])
    @require_admin
    @require_csrf
    def api_reboot():
        if appliance.system.reboot(reason="requested from the interface", min_interval=60.0):
            return ok(detail="The Raspberry Pi is rebooting. This takes about a minute.")
        return fail("Reboot was refused. Check the sudo rule from install.sh.", 409)

    @app.route("/api/actions/reset-safe", methods=["POST"])
    @require_admin
    @require_csrf
    def api_reset_safe():
        """Return to known-good settings without losing the calendar link."""
        keep = ("CALENDAR_ICS_URL", "CALENDAR_SOURCE", "ADMIN_PIN", "ADMIN_LAN_ACCESS",
                "ROOM_NAME", "TIMEZONE")
        changed = config.reset_to_defaults(keep=keep)
        appliance.calendar.refresh_now()
        appliance.poly.refresh_now()
        appliance.system.restart("room-kiosk.service", min_interval=2.0, reason="safe reset")
        return ok(
            detail="Settings reset to defaults. The calendar link, room name and "
            "admin PIN were kept.",
            changed=len(changed),
        )

    # -- settings --------------------------------------------------------
    @app.route("/api/settings", methods=["GET"])
    @require_admin
    def api_settings_get():
        return jsonify(
            {
                "values": config.as_dict(redact=True),
                "advisories": advisories(config.as_dict()),
                "env_locked": config.env_locked_keys(),
                "secret_keys": sorted(SECRET_KEYS),
                "warnings": list(config.warnings),
                "groups": [
                    {
                        "id": gid,
                        "title": title,
                        "help": help_text,
                        "fields": [
                            {
                                "key": f.key,
                                "type": f.type,
                                "label": f.label,
                                "help": f.help,
                                "choices": list(f.choices),
                                "advanced": f.advanced,
                                "secret": f.secret,
                                "placeholder": f.placeholder,
                                "min": f.minimum,
                                "max": f.maximum,
                            }
                            for f in fields
                        ],
                    }
                    for gid, title, help_text, fields in grouped_fields()
                ],
            }
        )

    @app.route("/api/settings", methods=["POST"])
    @require_admin
    @require_csrf
    def api_settings_post():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return fail("Expected a JSON object of settings.")

        # A redacted secret sent back unchanged must not overwrite the real one.
        pairs = {
            key: value
            for key, value in payload.items()
            if not (key in SECRET_KEYS and str(value) == "********")
        }
        unknown = sorted(set(pairs) - set(FIELDS_BY_KEY))
        if unknown:
            return fail(f"Unknown settings: {', '.join(unknown[:5])}")

        changed, errors = config.update(pairs)
        if errors:
            return jsonify({"ok": False, "errors": errors}), 422

        units = config.restart_units_for_changes(changed)
        restarted = [
            unit
            for unit in units
            if appliance.system.restart(unit, min_interval=2.0, reason="settings changed")
        ]
        needs_backend_restart = "room-dashboard.service" in units

        return ok(
            changed=sorted(changed),
            restarted=restarted,
            needs_backend_restart=needs_backend_restart,
            advisories=advisories(config.as_dict()),
            detail=_describe_save(changed, restarted, needs_backend_restart),
        )

    @app.route("/api/settings/reset", methods=["POST"])
    @require_admin
    @require_csrf
    def api_settings_reset():
        payload = request.get_json(silent=True) or {}
        keep_calendar = bool(payload.get("keep_calendar", True))
        keep = ("CALENDAR_ICS_URL", "CALENDAR_SOURCE", "ADMIN_PIN") if keep_calendar else ()
        changed = config.reset_to_defaults(keep=keep)
        return ok(changed=len(changed), detail="Every setting is back to its default.")

    def _describe_save(changed: set[str], restarted: list[str], backend: bool) -> str:
        if not changed:
            return "Nothing changed."
        parts = [f"Saved {len(changed)} setting{'s' if len(changed) != 1 else ''}."]
        if restarted:
            parts.append("Applied straight away.")
        if backend:
            parts.append("Restart the room software for the new port to take effect.")
        return " ".join(parts)

    # -- backgrounds -----------------------------------------------------
    @app.route("/api/backgrounds", methods=["GET"])
    @require_admin
    def api_backgrounds():
        payload = appliance.backgrounds.payload()
        payload["ok"] = True
        payload["uploads_allowed"] = config.bool_("BACKGROUND_ALLOW_UPLOADS")
        payload["mode"] = config.str_("BACKGROUND_MODE")
        return jsonify(payload)

    @app.route("/api/backgrounds", methods=["POST"])
    @require_admin
    @require_csrf
    def api_backgrounds_upload():
        if not config.bool_("BACKGROUND_ALLOW_UPLOADS"):
            return fail("Background uploads are switched off in Settings.", 409)
        uploaded = request.files.get("image") or request.files.get("file")
        if uploaded is None:
            return fail("No file was attached.")
        item, error = appliance.backgrounds.save(
            uploaded.stream, declared_name=uploaded.filename or ""
        )
        if item is None:
            return fail(error)
        # Uploading the first one is a clear signal the slideshow is wanted.
        if config.str_("BACKGROUND_MODE") == "theme" and appliance.backgrounds.count() == 1:
            config.update({"BACKGROUND_MODE": "slideshow"})
        return ok(image=item.to_dict(), count=appliance.backgrounds.count())

    @app.route("/api/backgrounds/<name>", methods=["DELETE"])
    @require_admin
    @require_csrf
    def api_backgrounds_delete(name: str):
        if appliance.backgrounds.delete(name):
            return ok(count=appliance.backgrounds.count())
        return fail("That file is not in the slideshow.", 404)

    @app.route("/media/backgrounds/<name>")
    def media_background(name: str):
        path = appliance.backgrounds.resolve(name)
        if path is None:
            return "Not found", 404
        response = make_response(send_file(path, conditional=True))
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    # -- diagnostics -----------------------------------------------------
    @app.route("/api/diagnostics")
    @require_admin
    def api_diagnostics():
        return jsonify(
            {
                "ok": True,
                "poly": appliance.poly.inventory(),
                "remote": {
                    "status": appliance.remote.status(),
                    "mappings": appliance.remote.mappings(),
                    "devices": appliance.remote.list_devices(),
                    "actions": list(ACTIONS),
                },
                "calendar": appliance.calendar.status(),
                "browser": appliance.browser.status(),
                "airplay": appliance.airplay.status(),
                "units": {u: appliance.system.unit_state(u) for u in MANAGED_UNITS},
                "join_flows": {
                    pid: {
                        "priority_texts": list(flow.priority_texts),
                        "asks_for_name": flow.asks_for_name,
                        "notes": flow.notes,
                    }
                    for pid, flow in PROVIDER_FLOWS.items()
                },
                "config_file": str(config.file),
                "paths": {
                    "var": str(paths.VAR_DIR),
                    "profile": str(paths.CHROMIUM_PROFILE),
                    "cache": str(paths.CALENDAR_CACHE),
                },
                "modes": list(MODES),
            }
        )

    @app.route("/api/diagnostics/capture-remote", methods=["POST"])
    @require_admin
    @require_csrf
    def api_capture_remote():
        payload = request.get_json(silent=True) or {}
        try:
            seconds = float(payload.get("seconds", 10))
        except (TypeError, ValueError):
            seconds = 10.0
        return jsonify(appliance.remote.capture_keys(seconds))

    @app.route("/api/logs")
    @require_admin
    def api_logs():
        unit = request.args.get("unit", "").strip()
        try:
            lines = int(request.args.get("lines", 200))
        except ValueError:
            lines = 200
        return jsonify({"ok": True, "unit": unit or "all", "text": appliance.system.journal(unit, lines)})

    # -- internal (helper scripts) ---------------------------------------
    @app.route("/api/internal/airplay", methods=["POST"])
    @require_internal
    def api_internal_airplay():
        payload = request.get_json(silent=True) or {}
        event = str(payload.get("event") or "")
        client = str(payload.get("client") or "")
        return jsonify(appliance.airplay.handle_event(event, client=client))

    @app.route("/api/internal/action", methods=["POST"])
    @require_internal
    def api_internal_action():
        """Remote-control button presses from room-remote.service."""
        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action") or "").strip().lower()
        if action not in ACTIONS:
            return fail(f"Unknown action: {action}")
        result = appliance.room.dispatch_action(action)
        return jsonify({"ok": bool(result.get("ok")), **result})

    @app.route("/api/internal/token-check")
    @require_internal
    def api_internal_token_check():
        return ok(detail="Token accepted.")

    # -- errors ----------------------------------------------------------
    @app.errorhandler(404)
    def not_found(_error):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "No such endpoint."}), 404
        if request.path.startswith(("/media/", "/static/")):
            return "Not found", 404
        # A stray URL on the kiosk (a mistyped link, a restored session) should
        # land back on the room screen rather than an error page.
        return redirect(url_for("index"))

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify({"ok": False, "error": "That file is too large."}), 413

    @app.errorhandler(500)
    def server_error(error):
        log.exception("web.unhandled_error", extra={"fields": {"path": request.path}})
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "The room software hit an error."}), 500
        return render_template("error.html", **template_context()), 500


def _schedule_self_restart(appliance: RoomAppliance) -> None:
    """Exit shortly, so systemd restarts the backend cleanly."""

    def _exit() -> None:
        import time

        time.sleep(1.0)
        log_event(log, logging.WARNING, "application.restart_requested")
        appliance.stop()
        os._exit(0)  # noqa: SLF001 - deliberate: systemd will restart us

    threading.Thread(target=_exit, name="self-restart", daemon=True).start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="room-appliance",
        description="Meeting-room appliance backend and dashboard.",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Development mode: mock hardware, LAN access, auto-reload templates.",
    )
    parser.add_argument("--host", default=None, help="Override the listen address.")
    parser.add_argument("--port", type=int, default=None, help="Override the port.")
    parser.add_argument(
        "--print-internal-token",
        action="store_true",
        help="Print the token helper scripts use, then exit.",
    )
    args = parser.parse_args(argv)

    if args.print_internal_token:
        print(internal_token())
        return 0

    paths.ensure_dirs()
    config = get_config()

    if args.dev:
        # Development overrides are applied in memory only, never written to
        # config.yaml, so a developer cannot accidentally ship them to a room.
        overrides: dict[str, object] = {
            "DEV_MODE": True,
            "KIOSK_ENABLED": False,
            "LOG_LEVEL": "DEBUG",
        }
        # Only invent meetings when there is no real feed to read.
        if not config.str_("CALENDAR_ICS_URL"):
            overrides["CALENDAR_SOURCE"] = "mock"
        config.update(overrides, persist=False)

    app = create_app(config)
    appliance: RoomAppliance = app.config["ROOM_APPLIANCE"]

    def shutdown(signum, _frame):  # pragma: no cover - signal path
        log_event(log, logging.INFO, "application.signal", signal=int(signum))
        appliance.stop()
        sys.exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, shutdown)
        except (ValueError, OSError):
            pass

    host = args.host or effective_bind_host(config)
    port = args.port or config.int_("DASHBOARD_PORT")

    log_event(
        log,
        logging.INFO,
        "web.listening",
        host=host,
        port=port,
        lan_admin=config.bool_("ADMIN_LAN_ACCESS"),
    )
    # threaded=True: the dashboard polls while background threads work.
    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            # systemd will restart us, which is right if the port frees up. But
            # without this line the journal shows only a traceback and the room
            # restart-loops with no indication of why.
            log_event(
                log,
                logging.CRITICAL,
                "web.port_already_in_use",
                port=port,
                host=host,
                hint=f"something else holds port {port}: run "
                f"'sudo ss -tlnp | grep :{port}' to find it, or change the port "
                f"with 'roomctl set DASHBOARD_PORT 8090'",
            )
            return 2
        if exc.errno == errno.EADDRNOTAVAIL:
            log_event(
                log, logging.CRITICAL, "web.address_unavailable",
                host=host, hint="DASHBOARD_HOST is not an address on this machine",
            )
            return 2
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
