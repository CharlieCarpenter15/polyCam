"""The administrator's page for meeting minutes, and the API behind it.

Why this is a blueprint of its own rather than more routes in ``app/main.py``:

the promise the whole package makes is that nothing outside it changes when the
feature is switched off. That is far easier to keep when the page that manages
the feature lives inside the feature. ``main.py`` gains three lines and one
import; it never learns what a voice embedding is, and deleting this package
would leave it with a missing import rather than a hole in the middle of
``register_routes()``.

Everything here is written to read like the rest of ``register_routes()`` on
purpose — the same ``ok()``/``fail()`` envelope, the same ``@require_admin``
and ``@require_csrf`` stack, the same habit of answering with a sentence
somebody can act on instead of a bare status code — so that an engineer
reading the two files back to back is never asked to learn a second style.

Three rules this module exists to enforce, all of them about a feature that
records people:

*Off means off.* Every route checks ``MINUTES_ENABLED`` and, with it off,
answers as though it had never been registered: ``disabled.html`` for the page,
a JSON 404 for the API. Somebody who has not asked for meetings to be recorded
should find no evidence that this room could.

*No embedding ever reaches a browser.* People are serialised with
``Person.to_public_dict()``, which omits the face and voice vectors. They are
biometric data, there is nothing a browser could do with one, and the only way
to be sure they never leak is never to send them.

*Nothing built out of a request string is ever opened.* A reference photo is
located through ``paths.photo_path()``, whose regexes reject anything that is
not a generated id, and an uploaded photo is identified by its magic bytes —
``background_service.detect_image_type`` — rather than by the filename or the
content type the browser claimed.

Responses under ``/api/`` are already sent ``Cache-Control: no-store`` by
``main.py``'s ``after_request`` hook, which is why the transcript and the
summary routes do not set it again: a private meeting's words should not sit
in a phone's disk cache, and one rule doing that for every API route is more
trustworthy than seven routes remembering to.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Callable

from flask import (
    Blueprint,
    current_app,
    jsonify,
    render_template,
    request,
    send_file,
)

from .. import __version__
from ..background_service import detect_image_type
from ..logging_setup import get_logger, log_event
from ..web_security import (
    csrf_token,
    is_admin,
    is_local_request,
    require_admin,
    require_csrf,
)
from . import mailer, paths, summarize
from .people import KINDS

log = get_logger("minutes.web")

#: Largest reference photo accepted. A face embedding is computed from a
#: thumbnail-sized crop, so anything past a few megabytes is a phone camera
#: being generous rather than a better photo. ``MAX_CONTENT_LENGTH`` in
#: ``main.py`` caps the request body long before this, but that cap has to
#: clear a 200 MB slideshow video; this is the cap that fits the job.
MAX_PHOTO_BYTES = 8 * 1024 * 1024

#: Longest voice sample the room will record in one go. ``record_voice_sample``
#: clamps to the same sort of range; repeating the bound here means the page
#: can be told what it may ask for.
MIN_SAMPLE_SECONDS = 5
MAX_SAMPLE_SECONDS = 30
DEFAULT_SAMPLE_SECONDS = 15

#: Most meetings listed in one call. The page shows them all; the cap is there
#: so that a room left recording for a year cannot produce a 40 MB response.
MAX_SESSIONS = 200
DEFAULT_SESSIONS = 60

#: What to say when the master switch is off. Named rather than repeated so
#: the page and every route say exactly the same thing.
FEATURE_OFF = (
    "Meeting minutes is switched off. Turn it on in Settings → "
    "“Meeting minutes (experimental)”."
)

#: What to say when the switch is on but no service was wired in. That is a
#: build or start-up fault rather than a choice, and it is logged as one.
FEATURE_MISSING = (
    "The meeting-minutes feature is not running on this appliance. Restart the "
    "room software; if it comes back, check the logs for a start-up failure."
)

minutes_bp = Blueprint("minutes", __name__)


# ---------------------------------------------------------------------------
# The same small helpers register_routes() uses
# ---------------------------------------------------------------------------


def ok(**payload: Any):
    return jsonify({"ok": True, **payload})


def fail(message: str, status: int = 400, **payload: Any):
    return jsonify({"ok": False, "error": message, **payload}), status


def _config():
    return current_app.config["ROOM_CONFIG"]


def _service():
    """The ``MinutesService`` this request should use, or None.

    Looked up rather than held, for two reasons. The appliance owns the
    service's lifetime and this blueprint should not extend it; and a test (or
    a future caller with a different wiring) can hand one over through
    ``app.config["MINUTES_SERVICE"]`` without constructing the recorder, the
    speech engines and the camera that the real one imports.
    """
    service = current_app.config.get("MINUTES_SERVICE")
    if service is not None:
        return service
    return getattr(current_app.config.get("ROOM_APPLIANCE"), "minutes", None)


def _feature() -> tuple[Any, str]:
    """``(service, reason)`` — the service, or None and why not."""
    if not _config().bool_("MINUTES_ENABLED"):
        return None, FEATURE_OFF
    service = _service()
    if service is None:
        # Being switched on and having nothing behind it is a wiring fault,
        # and silently answering "switched off" would hide it forever.
        log_event(log, logging.ERROR, "minutes.web.no_service", path=request.path)
        return None, FEATURE_MISSING
    return service, ""


def needs_minutes(view: Callable[..., Any]) -> Callable[..., Any]:
    """Answer as though the route did not exist unless the feature is on.

    Applied *inside* ``@require_admin`` (and ``@require_csrf``) so the order of
    checks is: who are you, is this request genuine, and only then does this
    room do that. A stranger on the network is told to sign in either way and
    so learns nothing about what this room has switched on.
    """

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        service, reason = _feature()
        if service is None:
            return fail(reason, 404)
        return view(*args, **kwargs)

    return wrapper


def _template_context() -> dict[str, Any]:
    """Everything ``base.html`` needs.

    A copy of ``main.py``'s helper of the same name: it is a closure inside
    ``register_routes()`` and a blueprint cannot reach it. Kept identical on
    purpose — the app bar, the theme and the CSRF token all come from here, and
    a page missing one of them looks like a different application.
    """
    config = _config()
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


def _text(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def _public_people(service) -> list[dict[str, Any]]:
    """Everybody enrolled, without their vectors. The only serialiser used."""
    return [person.to_public_dict() for person in service.people.all()]


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


@minutes_bp.route("/minutes")
@require_admin
def page():
    """The minutes page: status, people, meetings, and a couple of tests."""
    context = _template_context()
    service, _reason = _feature()
    if service is None:
        # The same answer /panel and /controller give for a switched-off
        # feature: the page that would have been here is simply not here.
        return render_template("disabled.html", **context), 404
    context.update(
        active_page="minutes",
        max_photo_mb=MAX_PHOTO_BYTES // (1024 * 1024),
        default_sample_seconds=DEFAULT_SAMPLE_SECONDS,
        keep_days=_config().int_("MINUTES_KEEP_DAYS"),
        keep_audio_days=_config().int_("MINUTES_KEEP_AUDIO_DAYS"),
    )
    return render_template("minutes.html", **context)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@minutes_bp.route("/api/minutes/status")
@require_admin
@needs_minutes
def api_status():
    """Everything the page needs to explain the feature to somebody.

    ``status()`` already carries the reasons a capability is unavailable, and
    they are written to be acted on — "… is not installed. Install it with:
    pip install …" — so they are passed through untouched. The settings block
    added here is switches and counts only: no API key, no SMTP password, and
    nothing else from ``SECRET_KEYS``.
    """
    service = _service()
    config = _config()
    payload = dict(service.status())
    payload["settings"] = {
        "record_room": config.bool_("MINUTES_RECORD_ROOM"),
        "record_far_end": config.bool_("MINUTES_RECORD_FAR_END"),
        "show_notice": config.bool_("MINUTES_SHOW_RECORDING_NOTICE"),
        "identify_faces": config.bool_("MINUTES_IDENTIFY_FACES"),
        "identify_voices": config.bool_("MINUTES_IDENTIFY_VOICES"),
        "identify_remote": config.bool_("MINUTES_IDENTIFY_REMOTE"),
        "read_captions": config.bool_("MINUTES_READ_CAPTIONS"),
        "summary_enabled": config.bool_("MINUTES_SUMMARY_ENABLED"),
        "email_enabled": config.bool_("MINUTES_EMAIL_ENABLED"),
        "stt_engine": config.str_("MINUTES_STT_ENGINE"),
        "keep_days": config.int_("MINUTES_KEEP_DAYS"),
        "keep_audio_days": config.int_("MINUTES_KEEP_AUDIO_DAYS"),
    }
    payload["limits"] = {
        "photo_mb": MAX_PHOTO_BYTES // (1024 * 1024),
        "min_sample_seconds": MIN_SAMPLE_SECONDS,
        "max_sample_seconds": MAX_SAMPLE_SECONDS,
        "default_sample_seconds": DEFAULT_SAMPLE_SECONDS,
    }
    return jsonify({"ok": True, **payload})


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


@minutes_bp.route("/api/minutes/people")
@require_admin
@needs_minutes
def api_people():
    service = _service()
    return ok(people=_public_people(service), stats=service.people.stats())


@minutes_bp.route("/api/minutes/people", methods=["POST"])
@require_admin
@require_csrf
@needs_minutes
def api_people_create():
    service = _service()
    payload = request.get_json(silent=True) or {}
    person, error = service.people.add(
        name=_text(payload.get("name"), 120),
        email=_text(payload.get("email")),
        notes=_text(payload.get("notes"), 500),
    )
    if person is None:
        return fail(error)
    return ok(
        person=person.to_public_dict(),
        detail=f"“{person.name}” added. Add a photo or record their voice so "
        "the room can recognise them.",
    )


@minutes_bp.route("/api/minutes/people/<person_id>", methods=["POST"])
@require_admin
@require_csrf
@needs_minutes
def api_people_update(person_id: str):
    service = _service()
    payload = request.get_json(silent=True) or {}
    # Only the fields actually sent are changed, so editing a name cannot
    # blank an email address the form did not happen to include.
    changes: dict[str, Any] = {}
    for field, limit in (("name", 120), ("email", 200), ("notes", 500)):
        if field in payload:
            changes[field] = _text(payload.get(field), limit)
    if not changes:
        return fail("There was nothing to change.")
    person, error = service.people.update(person_id, **changes)
    if person is None:
        return fail(error, 404 if error == "No such person." else 400)
    return ok(person=person.to_public_dict(), detail="Saved.")


@minutes_bp.route("/api/minutes/people/<person_id>", methods=["DELETE"])
@require_admin
@require_csrf
@needs_minutes
def api_people_delete(person_id: str):
    """Remove a profile, its vectors and its photos. There is no undo."""
    service = _service()
    person = service.people.get(person_id)
    if person is None:
        return fail("No such person.", 404)
    name = person.name
    if not service.people.delete(person_id):
        return fail("That profile could not be removed.", 409)
    return ok(
        detail=f"“{name}” and every face and voice sample of theirs have been "
        "deleted from this appliance."
    )


@minutes_bp.route("/api/minutes/people/<person_id>/clear", methods=["POST"])
@require_admin
@require_csrf
@needs_minutes
def api_people_clear(person_id: str):
    """Forget one kind of sample without deleting the person."""
    service = _service()
    payload = request.get_json(silent=True) or {}
    kind = _text(payload.get("kind"), 10).lower()
    if kind not in KINDS:
        return fail(f"Unknown kind. Use one of: {', '.join(KINDS)}.")
    person = service.people.get(person_id)
    if person is None:
        return fail("No such person.", 404)
    if not service.people.clear_vectors(person_id, kind):
        return fail("Those samples could not be cleared.", 409)
    noun = "face" if kind == "face" else "voice"
    return ok(
        person=(service.people.get(person_id) or person).to_public_dict(),
        detail=f"Forgot every {noun} sample for “{person.name}”.",
    )


@minutes_bp.route("/api/minutes/people/<person_id>/photo", methods=["POST"])
@require_admin
@require_csrf
@needs_minutes
def api_people_photo(person_id: str):
    """Enrol a face from an uploaded photo.

    The discipline is ``background_service.save()``'s: read the head, decide
    what the file *is* from its magic bytes, and stop reading the moment it
    goes past what we are willing to hold. The name and the content type the
    browser sent are never consulted — a file called ``face.jpg`` full of
    JavaScript is a text file, and it is refused as one.
    """
    service = _service()
    if service.people.get(person_id) is None:
        return fail("No such person.", 404)

    uploaded = request.files.get("photo") or request.files.get("file")
    if uploaded is None:
        return fail("No photo was attached.")

    head = uploaded.stream.read(64)
    if not head:
        return fail("That file was empty.")
    if detect_image_type(head) is None:
        return fail("Only JPEG, PNG, GIF and WebP photos can be used.")

    data, error = _read_capped(uploaded.stream, head, MAX_PHOTO_BYTES)
    if error:
        return fail(error, 413)

    added, detail = service.enrol_photo(person_id, data)
    if not added:
        return fail(detail)
    person = service.people.get(person_id)
    return ok(person=person.to_public_dict() if person else None, detail=detail)


def _read_capped(stream, head: bytes, limit: int) -> tuple[bytes, str]:
    """Read the rest of ``stream``, refusing anything past ``limit``.

    Read in chunks rather than in one call so that an oversized upload is
    abandoned as soon as it is known to be oversized, instead of being held in
    memory in full first and rejected afterwards.
    """
    chunks = [head]
    total = len(head)
    while True:
        chunk = stream.read(256 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            return b"", f"Photos must be smaller than {limit // (1024 * 1024)} MB."
        chunks.append(chunk)
    return b"".join(chunks), ""


@minutes_bp.route("/api/minutes/people/<person_id>/photo/<int:index>")
@require_admin
@needs_minutes
def api_people_photo_get(person_id: str, index: int):
    """Serve one reference photo.

    The path comes from ``paths.photo_path()`` — which returns None unless the
    id matches the generated-id regex and the index is in range — so no part of
    the request is ever joined onto a directory. The type is taken from the
    file's own first bytes rather than its ``.jpg`` name, because that name is
    a slot number and not a claim about the contents.
    """
    service = _service()
    if service.people.get(person_id) is None:
        return fail("No such photo.", 404)
    path = paths.photo_path(person_id, index)
    if path is None or not path.is_file():
        return fail("No such photo.", 404)
    try:
        with path.open("rb") as handle:
            head = handle.read(64)
    except OSError:
        return fail("No such photo.", 404)
    detected = detect_image_type(head)
    if detected is None:
        return fail("No such photo.", 404)
    return send_file(path, mimetype=detected[1], conditional=True)


@minutes_bp.route("/api/minutes/people/<person_id>/voice", methods=["POST"])
@require_admin
@require_csrf
@needs_minutes
def api_people_voice(person_id: str):
    """Record a voice sample through the room's own microphone.

    Deliberately not ``getUserMedia``: a browser will not open a microphone
    over plain HTTP on a LAN address, and even if it would, the phone's
    microphone is not the one that has to recognise this person in six weeks'
    time. The room's far-field microphone is, so the room does the recording.

    This blocks for the length of the sample. That is the honest behaviour —
    the page says “speak now” and the answer arrives when the recording is
    over — and it is why the service refuses while a meeting is being recorded.
    """
    service = _service()
    payload = request.get_json(silent=True) or {}
    try:
        seconds = int(payload.get("seconds", DEFAULT_SAMPLE_SECONDS))
    except (TypeError, ValueError):
        return fail("The length of the sample must be a number of seconds.")
    seconds = max(MIN_SAMPLE_SECONDS, min(MAX_SAMPLE_SECONDS, seconds))

    if service.people.get(person_id) is None:
        return fail("No such person.", 404)

    recorded, detail = service.record_voice_sample(person_id, seconds)
    if not recorded:
        return fail(detail, 409)
    person = service.people.get(person_id)
    return ok(
        person=person.to_public_dict() if person else None,
        seconds=seconds,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------


@minutes_bp.route("/api/minutes/sessions")
@require_admin
@needs_minutes
def api_sessions():
    service = _service()
    try:
        limit = int(request.args.get("limit", DEFAULT_SESSIONS))
    except (TypeError, ValueError):
        limit = DEFAULT_SESSIONS
    limit = max(1, min(MAX_SESSIONS, limit))
    return ok(
        sessions=service.list_sessions(limit),
        keep_days=_config().int_("MINUTES_KEEP_DAYS"),
        keep_audio_days=_config().int_("MINUTES_KEEP_AUDIO_DAYS"),
    )


@minutes_bp.route("/api/minutes/sessions/<session_id>")
@require_admin
@needs_minutes
def api_session(session_id: str):
    """One meeting in full: summary, attributed transcript, recipients.

    The enrolled people come back with it. The page's most valuable action is
    putting a name to “Room speaker”, and it should not have to make a second
    request before it can offer that.
    """
    service = _service()
    data = service.get_session(session_id)
    if data is None:
        return fail("No such recording.", 404)
    return ok(session=data, people=_public_people(service))


@minutes_bp.route("/api/minutes/sessions/<session_id>", methods=["DELETE"])
@require_admin
@require_csrf
@needs_minutes
def api_session_delete(session_id: str):
    service = _service()
    if not service.delete_session(session_id):
        return fail("No such recording.", 404)
    return ok(detail="That meeting — audio, transcript and summary — has been deleted.")


@minutes_bp.route("/api/minutes/sessions/<session_id>/reprocess", methods=["POST"])
@require_admin
@require_csrf
@needs_minutes
def api_session_reprocess(session_id: str):
    """Write the meeting up again, using whatever is configured now."""
    service = _service()
    queued, detail = service.reprocess(session_id)
    if not queued:
        return fail(detail, 404)
    return ok(detail=detail)


@minutes_bp.route("/api/minutes/sessions/<session_id>/relabel", methods=["POST"])
@require_admin
@require_csrf
@needs_minutes
def api_session_relabel(session_id: str):
    """Say who an unidentified speaker actually was.

    The best enrolment path the feature has: somebody reads “Room speaker” in a
    transcript, picks a name, and the appliance both corrects the transcript and
    learns the voice from the very segments it just got wrong. The refreshed
    session is returned so the page can redraw without asking again.
    """
    service = _service()
    payload = request.get_json(silent=True) or {}
    label = _text(payload.get("label"), 120)
    person_id = _text(payload.get("person_id"), 40)
    if not label:
        return fail("Which speaker? No label was given.")
    if not person_id:
        return fail("Choose who that was, or add them under People first.")
    changed, detail = service.relabel(session_id, label, person_id)
    if not changed:
        return fail(detail)
    return ok(detail=detail, session=service.get_session(session_id))


@minutes_bp.route("/api/minutes/sweep", methods=["POST"])
@require_admin
@require_csrf
@needs_minutes
def api_sweep():
    """Apply the retention settings now instead of at the next sweep."""
    service = _service()
    removed = int(service.sweep() or 0)
    days = _config().int_("MINUTES_KEEP_DAYS")
    if not removed:
        return ok(removed=0, detail=f"Nothing was older than {days} days.")
    return ok(
        removed=removed,
        detail=f"Deleted {removed} meeting{'s' if removed != 1 else ''} older "
        f"than {days} days.",
    )


# ---------------------------------------------------------------------------
# Try it
# ---------------------------------------------------------------------------


@minutes_bp.route("/api/minutes/look", methods=["POST"])
@require_admin
@require_csrf
@needs_minutes
def api_look():
    """Take one look through the room camera and say who was recognised."""
    service = _service()
    if not _config().bool_("MINUTES_IDENTIFY_FACES"):
        return fail(
            "Recognising faces in the room is switched off, so the camera was "
            "not opened. Turn it on in Settings first.",
            409,
        )
    look = service.look_at_room_now() or {}
    if look.get("ok") is False:
        return fail(look.get("error") or "The room camera could not be read.", 409)
    people = look.get("people") or []
    names = [str(p.get("name") or "") for p in people if isinstance(p, dict)]
    names = [name for name in names if name]
    if names:
        detail = "Recognised " + ", ".join(names) + "."
    else:
        detail = (
            "Nobody the room recognises was in view. Face recognition across a "
            "boardroom table is hard — try a clearer photo, or somebody closer "
            "to the camera."
        )
    return ok(look=look, people=people, detail=detail)


@minutes_bp.route("/api/minutes/test-email", methods=["POST"])
@require_admin
@require_csrf
@needs_minutes
def api_test_email():
    """Send one test message, to prove the appliance can reach the mail server."""
    config = _config()
    payload = request.get_json(silent=True) or {}
    address = _text(payload.get("to"))
    if "@" not in address:
        return fail("That does not look like an email address.")

    delivery = mailer.send_test(config, address)
    if not delivery.ok:
        return fail(delivery.error or "The test message could not be sent.", 409)

    detail = f"A test message is on its way to {address}."
    summary_ok, summary_why = summarize.available(config)
    if not summary_ok:
        # Mail working but no summary being written is the confusing case: the
        # test arrives, and then no minutes ever do. Better said now.
        detail = f"{detail} {summary_why}"
    log_event(log, logging.INFO, "minutes.web.test_email")
    return ok(detail=detail, sent_to=list(delivery.sent_to))
