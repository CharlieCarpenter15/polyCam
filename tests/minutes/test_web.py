"""The meeting-minutes page and API: access, privacy, uploads and sessions.

Built the way ``tests/test_web.py`` builds things — a real app on a throw-away
configuration directory, signed in as the Pi itself, with the page's own CSRF
token lifted out of the HTML — so that anything these tests prove is true of
the running appliance and not of a mock of it.

The one substitution is ``MinutesService``. The real one imports a recorder,
two speech engines, a face model and the Claude SDK at module scope, none of
which belongs in a web test and none of which the web layer touches. What the
web layer *does* touch is exercised for real: the people store, the session
directory and the transcript records here are the genuine classes, so a change
to their shape fails in this file rather than in somebody's meeting room.

Two of the checks below are not conveniences. A face or voice embedding must
never reach a browser, and a photo must never be located from a request string
— both are asserted directly rather than inferred from a passing round-trip.
"""

from __future__ import annotations

import io
import json
import struct
import zlib
from datetime import datetime, timezone

import pytest

# ---------------------------------------------------------------------------
# Sample files
# ---------------------------------------------------------------------------


def tiny_png() -> bytes:
    """A genuinely valid 4x4 PNG, so the upload check is a real check."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF
        )

    header = struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\xff\x88\x22" * 4 for _ in range(4))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def tiny_jpeg() -> bytes:
    """Enough of a JPEG for the magic-byte check; the rest is padding."""
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 256 + b"\xff\xd9"


# ---------------------------------------------------------------------------
# A stand-in service with the real MinutesService's public surface
# ---------------------------------------------------------------------------


class FakeMinutes:
    """Everything ``app/minutes/web.py`` calls, and nothing else.

    Where a method's answer comes off the disk in the real service it comes off
    the disk here too, through the same ``paths``, ``store`` and ``transcript``
    helpers, so the session tests below run against a directory tree that looks
    exactly like a room's.
    """

    def __init__(self, config, people_store, paths_module):
        self.config = config
        self.people = people_store
        self.paths = paths_module
        self.enabled = True
        #: Every call the web layer made, for the endpoints whose whole job is
        #: to hand work to the service.
        self.calls: list[tuple] = []
        self.look_result = {
            "ok": True,
            "at": "2026-08-28T09:00:00+00:00",
            "people": [{"name": "Ada Lovelace", "score": 0.81}],
        }
        self.voice_result = (True, "Ada Lovelace’s voice has been added.")
        self.photo_error = ""
        self.swept = 3

    # -- status ----------------------------------------------------------
    def status(self):
        return {
            "enabled": True,
            "recording": {"session_id": "20260828-090000-abcdef12",
                          "title": "Engineering Daily", "seconds": 92},
            "working_on": "",
            "queued": 0,
            "people": self.people.stats(),
            "sessions": len(self.paths.list_session_ids()),
            "room_look": {},
            "capabilities": {
                "audio": {"ok": True, "detail": ""},
                "transcribe": {
                    "ok": False,
                    "detail": "“whisper-cli” was not found on PATH, so offline "
                              "speech-to-text is unavailable.",
                },
                "roster": {"ok": True, "detail": ""},
                "faces": {"ok": False, "detail": "“opencv-python-headless” is not "
                                                 "installed. Install it with: pip "
                                                 "install opencv-python-headless"},
                "voices": {"ok": False, "detail": "Recognising voices is switched off."},
                "summary": {"ok": False, "detail": "Writing a summary with Claude is "
                                                   "switched off."},
                "email": {"ok": False, "detail": "Emailing the summary is switched off."},
            },
            "engines": [{"name": "whisper-cpp", "ok": False, "detail": "not installed"}],
            "dependencies": [{"name": "numpy", "ok": False,
                              "detail": "“numpy” is not installed, so comparing voices "
                                        "and faces is unavailable. Install it with: "
                                        "pip install numpy"}],
            "last_error": "",
        }

    # -- sessions --------------------------------------------------------
    def _meta(self, directory):
        from app.minutes.transcript import SessionMeta
        from app.store import read_json

        return SessionMeta.from_dict(read_json(directory / "meta.json", default=None))

    def _transcript(self, directory):
        from app.minutes.transcript import Transcript
        from app.store import read_json

        return Transcript.from_dict(read_json(directory / "transcript.json", default=None))

    def list_sessions(self, limit=50):
        from app.minutes.summarize import Summary
        from app.store import read_json

        out = []
        for session_id in self.paths.list_session_ids()[: max(1, limit)]:
            directory = self.paths.session_dir(session_id)
            meta = self._meta(directory)
            if meta is None:
                continue
            summary = Summary.from_dict(read_json(directory / "summary.json", default=None))
            delivery = read_json(directory / "delivery.json", default=None) or {}
            out.append(
                {
                    "session_id": session_id,
                    "title": meta.title,
                    "started_at": meta.started_at,
                    "ended_at": meta.ended_at,
                    "provider": meta.provider,
                    "stage": meta.stage,
                    "error": meta.error,
                    "has_summary": bool(summary and summary.ok),
                    "has_audio": any(directory.glob("*.wav")),
                    "sent_to": len(delivery.get("sent_to") or []),
                }
            )
        return out

    def get_session(self, session_id):
        from app.minutes.summarize import Summary
        from app.store import read_json

        directory = self.paths.session_dir(session_id)
        if directory is None or not directory.is_dir():
            return None
        meta = self._meta(directory)
        if meta is None:
            return None
        written = self._transcript(directory)
        summary = Summary.from_dict(read_json(directory / "summary.json", default=None))
        return {
            "session_id": session_id,
            "meta": meta.to_dict(),
            "transcript": written.to_dict() if written else None,
            "text": written.render_text() if written else "",
            "speakers": written.speakers() if written else [],
            "summary": summary.to_dict() if summary else None,
            "delivery": read_json(directory / "delivery.json", default=None),
            "recipients": written.recipients() if written else [],
        }

    def delete_session(self, session_id):
        directory = self.paths.session_dir(session_id)
        if directory is None or not directory.is_dir():
            return False
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()
        return True

    def reprocess(self, session_id):
        directory = self.paths.session_dir(session_id)
        if directory is None or not directory.is_dir():
            return False, "No such recording."
        self.calls.append(("reprocess", session_id))
        return True, "Queued. It will be written up again in a moment."

    def relabel(self, session_id, label, person_id):
        from app.store import write_json

        directory = self.paths.session_dir(session_id)
        if directory is None or not directory.is_dir():
            return False, "No such recording."
        written = self._transcript(directory)
        if written is None:
            return False, "That recording has no transcript yet."
        person = self.people.get(person_id)
        if person is None:
            return False, "No such person."
        touched = [s for s in written.segments if s.label() == label]
        if not touched:
            return False, f"Nothing in this transcript is labelled “{label}”."
        for segment in touched:
            segment.speaker = person.name
            segment.person_id = person.id
            segment.source = "manual"
            segment.confidence = 1.0
        write_json(directory / "transcript.json", written.to_dict(), mode=0o600)
        # The real service learns the voice from these very segments; that is
        # voiceprint.py's business and is covered by its own tests.
        self.people.add_vector(person_id, "voice", "test-model", [0.0, 1.0, 0.0])
        self.calls.append(("relabel", session_id, label, person_id))
        return True, f"{len(touched)} lines are now labelled “{person.name}”."

    def sweep(self):
        self.calls.append(("sweep",))
        return self.swept

    # -- enrolment -------------------------------------------------------
    def look_at_room_now(self):
        self.calls.append(("look",))
        return self.look_result

    def enrol_photo(self, person_id, data):
        person = self.people.get(person_id)
        if person is None:
            return False, "No such person."
        if self.photo_error:
            return False, self.photo_error
        self.calls.append(("enrol_photo", person_id, len(data)))
        index = self.people.next_photo_index(person_id)
        path = self.paths.photo_path(person_id, index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.people.record_photo(person_id, index)
        # A real vector, so that "no vector ever reaches a browser" is a claim
        # about something that exists rather than about an empty list.
        self.people.add_vector(person_id, "face", "test-model", [1.0, 0.0, 0.0])
        return True, f"{person.name} can now be recognised by sight."

    def enrol_voice(self, person_id, wav_path):  # pragma: no cover - not routed
        return self.voice_result

    def record_voice_sample(self, person_id, seconds):
        person = self.people.get(person_id)
        if person is None:
            return False, "No such person."
        self.calls.append(("record_voice", person_id, seconds))
        if not self.voice_result[0]:
            return self.voice_result
        self.people.add_vector(person_id, "voice", "test-model", [0.0, 0.0, 1.0])
        return self.voice_result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def minutes_paths(room_dirs):
    """Point ``app/minutes/paths.py`` at the temporary var directory.

    Nothing to do beyond ``room_dirs``: every name in that module is derived
    from ``app.paths.VAR_DIR`` when it is read, so redirecting the appliance's
    writable tree redirects this too.
    """
    from app.minutes import paths as minutes_paths_module

    minutes_paths_module.ensure_dirs()
    return minutes_paths_module


@pytest.fixture()
def service(mock_config, minutes_paths):
    from app.minutes.people import PeopleStore

    store = PeopleStore(minutes_paths.PEOPLE_FILE)
    store.load()
    return FakeMinutes(mock_config, store, minutes_paths)


@pytest.fixture()
def app(mock_config, service):
    """The real application, with the minutes blueprint and a stand-in service.

    Registered here rather than relied upon so the file passes whether or not
    ``main.py`` has been wired up yet; when it has, the guard makes this a
    no-op and the tests run against the real registration.
    """
    from app.main import create_app
    from app.minutes.web import minutes_bp

    mock_config.update({"MINUTES_ENABLED": True})
    application = create_app(mock_config, start_services=False)
    application.config.update(TESTING=True, MINUTES_SERVICE=service)
    if "minutes" not in application.blueprints:
        application.register_blueprint(minutes_bp)
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def token(client):
    """The CSRF token a real page would carry."""
    response = client.get("/panel")
    body = response.get_data(as_text=True)
    marker = 'data-csrf="'
    start = body.index(marker) + len(marker)
    return body[start : body.index('"', start)]


@pytest.fixture()
def person(service):
    created, error = service.people.add("Ada Lovelace", "ada@example.com", "Sits by the window")
    assert created is not None, error
    return created


LAN = {"REMOTE_ADDR": "192.168.1.50"}


def post(client, url, token, payload=None):
    return client.post(url, json=payload or {}, headers={"X-Room-Token": token})


def delete(client, url, token):
    return client.delete(url, headers={"X-Room-Token": token})


def make_session(paths_module, session_id="20260828-090000-abcdef12", *, transcript=True,
                 summary=True, delivered=True, stage="sent"):
    """Write a session directory that looks exactly like a recorded meeting."""
    from app.minutes.transcript import Participant, Segment, SessionMeta, Transcript
    from app.store import write_json

    directory = paths_module.session_dir(session_id)
    directory.mkdir(parents=True, exist_ok=True)

    started = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)
    meta = SessionMeta(
        session_id=session_id,
        started_at=started.isoformat(),
        ended_at=started.replace(minute=30).isoformat(),
        meeting_id="meeting-1",
        title="Engineering Daily",
        provider="teams",
        room="Test Room",
        invited=["dev@example.com"],
        stage=stage,
    )
    write_json(directory / "meta.json", meta.to_dict())

    if transcript:
        written = Transcript(
            meta=meta,
            segments=[
                Segment(start=0.0, end=4.0, text="Morning everyone.", track="room"),
                Segment(start=4.0, end=9.0, text="Shipping on Thursday.", track="room"),
                Segment(
                    start=9.0, end=14.0, text="Noted, thanks.", track="far-end",
                    speaker="Grace Hopper", person_id="", source="roster", confidence=0.9,
                ),
            ],
            participants=[Participant(name="Grace Hopper", email="grace@example.com",
                                      where="remote", source="roster")],
            notices=["The far-end track was quiet for the first minute."],
        )
        write_json(directory / "transcript.json", written.to_dict())

    if summary:
        write_json(
            directory / "summary.json",
            {"text": "The team agreed to ship on Thursday.", "model": "claude-opus-5",
             "ok": True, "error": "", "input_tokens": 900, "output_tokens": 120},
        )

    if delivered:
        write_json(directory / "delivery.json",
                   {"ok": True, "sent_to": ["grace@example.com"], "error": ""})

    return session_id


# ---------------------------------------------------------------------------
# The routes, in one place, so "every route" means every route
# ---------------------------------------------------------------------------

#: ``(method, url, needs_csrf)`` for every route the blueprint owns. Kept as a
#: table because three of the rules below — off is off, admin only, mutations
#: carry a token — have to hold for all of them, and a table is the only way
#: that stays true when a route is added.
ROUTES = [
    ("GET", "/minutes", False),
    ("GET", "/api/minutes/status", False),
    ("GET", "/api/minutes/people", False),
    ("POST", "/api/minutes/people", True),
    ("POST", "/api/minutes/people/abcdef123456", True),
    ("DELETE", "/api/minutes/people/abcdef123456", True),
    ("POST", "/api/minutes/people/abcdef123456/clear", True),
    ("POST", "/api/minutes/people/abcdef123456/photo", True),
    ("GET", "/api/minutes/people/abcdef123456/photo/0", False),
    ("POST", "/api/minutes/people/abcdef123456/voice", True),
    ("GET", "/api/minutes/sessions", False),
    ("GET", "/api/minutes/sessions/20260828-090000-abcdef12", False),
    ("DELETE", "/api/minutes/sessions/20260828-090000-abcdef12", True),
    ("POST", "/api/minutes/sessions/20260828-090000-abcdef12/reprocess", True),
    ("POST", "/api/minutes/sessions/20260828-090000-abcdef12/relabel", True),
    ("POST", "/api/minutes/sweep", True),
    ("POST", "/api/minutes/look", True),
    ("POST", "/api/minutes/test-email", True),
]

API_ROUTES = [row for row in ROUTES if row[1].startswith("/api/")]
MUTATIONS = [row for row in ROUTES if row[2]]


def call(client, method, url, token=None):
    headers = {"X-Room-Token": token} if token else {}
    if method == "GET":
        return client.get(url, headers=headers)
    if method == "DELETE":
        return client.delete(url, headers=headers)
    return client.post(url, json={}, headers=headers)


class TestSwitchedOff:
    """With the feature off, it has to look as though it was never built."""

    @pytest.mark.parametrize("method,url,csrf", API_ROUTES,
                             ids=[f"{m} {u}" for m, u, _ in API_ROUTES])
    def test_the_api_is_not_there(self, client, token, mock_config, method, url, csrf):
        mock_config.update({"MINUTES_ENABLED": False})
        response = call(client, method, url, token)
        assert response.status_code == 404, url
        payload = response.get_json()
        assert payload["ok"] is False
        assert "switched off" in payload["error"]

    def test_the_page_is_not_there(self, client, mock_config):
        mock_config.update({"MINUTES_ENABLED": False})
        response = client.get("/minutes")
        assert response.status_code == 404
        assert "switched off" in response.get_data(as_text=True).lower()

    def test_nothing_reaches_the_service(self, client, token, mock_config, service):
        mock_config.update({"MINUTES_ENABLED": False})
        post(client, "/api/minutes/look", token)
        post(client, "/api/minutes/sweep", token)
        assert service.calls == []

    def test_a_missing_service_is_not_reported_as_a_missing_feature(
        self, app, client, monkeypatch
    ):
        """Switched on with nothing behind it is a fault, and says so.

        Answering “switched off” to a start-up failure would hide it for as long
        as nobody happened to read the journal.
        """
        app.config["MINUTES_SERVICE"] = None
        monkeypatch.delattr(app.config["ROOM_APPLIANCE"], "minutes", raising=False)
        response = client.get("/api/minutes/status")
        assert response.status_code == 404
        assert "not running" in response.get_json()["error"]


class TestAccess:
    @pytest.mark.parametrize("method,url,csrf", ROUTES,
                             ids=[f"{m} {u}" for m, u, _ in ROUTES])
    def test_every_route_needs_an_admin(self, client, token, mock_config, method, url, csrf):
        mock_config.update({"ADMIN_PIN": "4242"})
        headers = {"X-Room-Token": token} if csrf else {}
        response = client.open(url, method=method, headers=headers,
                               environ_overrides=LAN, json={} if method == "POST" else None)
        if url.startswith("/api/"):
            assert response.status_code == 401, url
            assert response.get_json()["needs_pin"] is True
        else:
            assert response.status_code == 302
            assert "/login" in response.headers["Location"]

    @pytest.mark.parametrize("method,url,csrf", MUTATIONS,
                             ids=[f"{m} {u}" for m, u, _ in MUTATIONS])
    def test_every_mutation_needs_the_page_token(self, client, method, url, csrf):
        response = call(client, method, url, token=None)
        assert response.status_code == 403, url
        assert response.get_json()["reload"] is True

    def test_a_wrong_token_is_refused(self, client):
        response = client.post("/api/minutes/look", json={},
                               headers={"X-Room-Token": "not-the-token"})
        assert response.status_code == 403


class TestPage:
    def test_it_renders(self, client):
        response = client.get("/minutes")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert len(body) > 1500
        assert "minutes.js" in body and "minutes.css" in body

    def test_it_is_honest_about_what_this_feature_is(self, client):
        """The settings copy warns about recording people; so must the page."""
        body = client.get("/minutes").get_data(as_text=True).lower()
        assert "records people" in body
        assert "experimental" in body

    def test_every_element_the_script_toggles_exists(self, client):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        script = (root / "app" / "static" / "minutes.js").read_text(encoding="utf-8")
        body = client.get("/minutes").get_data(as_text=True)
        toggled = set(re.findall(r'show\(\$\("([a-z0-9-]+)"\)', script))
        assert toggled, "the page toggles nothing, which cannot be right"
        for element_id in sorted(toggled):
            assert f'id="{element_id}"' in body, element_id


class TestStatus:
    def test_it_carries_the_reasons_things_are_unavailable(self, client):
        payload = client.get("/api/minutes/status").get_json()
        assert payload["ok"] is True
        assert payload["capabilities"]["transcribe"]["ok"] is False
        assert "whisper" in payload["capabilities"]["transcribe"]["detail"]
        assert payload["dependencies"][0]["detail"].startswith("“numpy”")

    def test_it_describes_the_recording_in_progress(self, client):
        recording = client.get("/api/minutes/status").get_json()["recording"]
        assert recording["seconds"] == 92
        assert recording["title"] == "Engineering Daily"

    def test_it_names_the_switches_without_leaking_a_secret(self, client, mock_config):
        mock_config.update({"MINUTES_CLAUDE_API_KEY": "sk-ant-secret-value",
                            "MINUTES_SMTP_PASSWORD": "hunter2"})
        body = client.get("/api/minutes/status").get_data(as_text=True)
        assert "sk-ant-secret-value" not in body
        assert "hunter2" not in body
        settings = json.loads(body)["settings"]
        assert settings["keep_days"] == mock_config.int_("MINUTES_KEEP_DAYS")

    def test_the_answer_is_never_cached(self, client):
        assert client.get("/api/minutes/status").headers["Cache-Control"] == "no-store"


class TestPeopleCrud:
    def test_a_person_can_be_added_read_edited_and_deleted(self, client, token, service):
        created = post(client, "/api/minutes/people", token,
                       {"name": "Ada Lovelace", "email": "ada@example.com"})
        assert created.status_code == 200
        person_id = created.get_json()["person"]["id"]

        listing = client.get("/api/minutes/people").get_json()
        assert [p["name"] for p in listing["people"]] == ["Ada Lovelace"]
        assert listing["stats"]["people"] == 1

        edited = post(client, f"/api/minutes/people/{person_id}", token,
                      {"name": "Ada King"})
        assert edited.get_json()["person"]["name"] == "Ada King"
        # A field that was not sent is left alone rather than blanked.
        assert edited.get_json()["person"]["email"] == "ada@example.com"

        removed = delete(client, f"/api/minutes/people/{person_id}", token)
        assert removed.get_json()["ok"] is True
        assert client.get("/api/minutes/people").get_json()["people"] == []
        assert service.people.get(person_id) is None

    def test_a_person_needs_a_name(self, client, token):
        assert post(client, "/api/minutes/people", token, {"name": "  "}).status_code == 400

    def test_two_people_cannot_share_a_name(self, client, token, person):
        response = post(client, "/api/minutes/people", token, {"name": "Ada Lovelace"})
        assert response.status_code == 400
        assert "already" in response.get_json()["error"]

    def test_an_unknown_person_is_a_404(self, client, token):
        assert client.get("/api/minutes/people").status_code == 200
        assert delete(client, "/api/minutes/people/abcdef123456", token).status_code == 404
        assert post(client, "/api/minutes/people/abcdef123456", token,
                    {"name": "Nobody"}).status_code == 404

    def test_samples_can_be_forgotten_without_deleting_the_person(
        self, client, token, person, service
    ):
        service.people.add_vector(person.id, "face", "test-model", [1.0, 0.0])
        assert client.get("/api/minutes/people").get_json()["people"][0]["faces"] == 1

        cleared = post(client, f"/api/minutes/people/{person.id}/clear", token,
                       {"kind": "face"})
        assert cleared.get_json()["person"]["faces"] == 0
        assert service.people.get(person.id) is not None

    def test_an_unknown_kind_is_refused(self, client, token, person):
        response = post(client, f"/api/minutes/people/{person.id}/clear", token,
                        {"kind": "fingerprint"})
        assert response.status_code == 400


class TestVectorsNeverLeave:
    """A privacy guarantee, asserted rather than assumed.

    An embedding is biometric data and a browser can do nothing with one, so no
    response body may contain a vector, its model name, or the ``face``/``voice``
    keys that carry them in the file on disk.
    """

    def _every_body(self, client, token, person_id, session_id):
        return [
            client.get("/api/minutes/status").get_data(as_text=True),
            client.get("/api/minutes/people").get_data(as_text=True),
            post(client, f"/api/minutes/people/{person_id}", token,
                 {"notes": "still here"}).get_data(as_text=True),
            post(client, f"/api/minutes/people/{person_id}/clear", token,
                 {"kind": "face"}).get_data(as_text=True),
            client.get("/api/minutes/sessions").get_data(as_text=True),
            client.get(f"/api/minutes/sessions/{session_id}").get_data(as_text=True),
        ]

    def test_no_response_carries_an_embedding(self, client, token, person, service,
                                              minutes_paths):
        service.people.add_vector(person.id, "face", "unmistakable-model",
                                  [0.123456, 0.654321, 0.111111])
        service.people.add_vector(person.id, "voice", "unmistakable-model",
                                  [0.999001, 0.002003, 0.004005])
        session_id = make_session(minutes_paths)

        for body in self._every_body(client, token, person.id, session_id):
            assert "unmistakable-model" not in body
            assert "0.654321" not in body
            assert "0.999001" not in body
            assert '"values"' not in body

    def test_the_public_shape_counts_samples_instead(self, client, person, service):
        service.people.add_vector(person.id, "voice", "test-model", [1.0, 0.0])
        row = client.get("/api/minutes/people").get_json()["people"][0]
        assert row["voices"] == 1
        assert "voice" not in row and "face" not in row


class TestPhotoUpload:
    def _upload(self, client, token, person_id, payload, name):
        return client.post(
            f"/api/minutes/people/{person_id}/photo",
            data={"photo": (io.BytesIO(payload), name)},
            content_type="multipart/form-data",
            headers={"X-Room-Token": token},
        )

    def test_a_png_is_accepted(self, client, token, person, service):
        response = self._upload(client, token, person.id, tiny_png(), "ada.png")
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["person"]["photos"] == [0]
        assert service.calls[0][0] == "enrol_photo"

    def test_a_jpeg_is_accepted(self, client, token, person):
        response = self._upload(client, token, person.id, tiny_jpeg(), "ada.jpg")
        assert response.status_code == 200, response.get_json()

    def test_a_text_file_is_refused(self, client, token, person, service):
        response = self._upload(client, token, person.id, b"just some words", "ada.txt")
        assert response.status_code == 400
        assert "JPEG" in response.get_json()["error"]
        assert service.calls == [], "nothing should reach the face model"

    def test_a_file_whose_extension_lies_is_refused(self, client, token, person):
        """The name says PNG; the bytes say shell script. The bytes win."""
        response = self._upload(client, token, person.id,
                                b"#!/bin/sh\nrm -rf /\n", "innocent.png")
        assert response.status_code == 400

    def test_an_svg_is_refused(self, client, token, person):
        """SVG is a script container, and no face model reads one anyway."""
        response = self._upload(client, token, person.id,
                                b'<svg onload="alert(1)"></svg>', "face.svg")
        assert response.status_code == 400

    def test_an_empty_file_is_refused(self, client, token, person):
        response = self._upload(client, token, person.id, b"", "nothing.png")
        assert response.status_code == 400

    def test_an_oversized_photo_is_refused(self, client, token, person, service):
        from app.minutes.web import MAX_PHOTO_BYTES

        payload = tiny_png() + b"\x00" * (MAX_PHOTO_BYTES + 1)
        response = self._upload(client, token, person.id, payload, "huge.png")
        assert response.status_code == 413
        assert "MB" in response.get_json()["error"]
        assert service.calls == []

    def test_an_upload_for_an_unknown_person_is_refused(self, client, token):
        response = self._upload(client, token, "abcdef123456", tiny_png(), "ada.png")
        assert response.status_code == 404

    def test_a_missing_file_is_explained(self, client, token, person):
        response = client.post(f"/api/minutes/people/{person.id}/photo",
                               data={}, content_type="multipart/form-data",
                               headers={"X-Room-Token": token})
        assert response.status_code == 400
        assert "No photo" in response.get_json()["error"]

    def test_a_refusal_from_the_face_model_is_passed_on(self, client, token, person, service):
        service.photo_error = "No face was found in that photo."
        response = self._upload(client, token, person.id, tiny_png(), "wall.png")
        assert response.status_code == 400
        assert response.get_json()["error"] == "No face was found in that photo."


class TestPhotoServing:
    def test_an_uploaded_photo_comes_back(self, client, token, person):
        client.post(
            f"/api/minutes/people/{person.id}/photo",
            data={"photo": (io.BytesIO(tiny_png()), "ada.png")},
            content_type="multipart/form-data",
            headers={"X-Room-Token": token},
        )
        served = client.get(f"/api/minutes/people/{person.id}/photo/0")
        assert served.status_code == 200
        # The type comes from the file's own bytes, not from its .jpg slot name.
        assert served.headers["Content-Type"].startswith("image/png")
        assert served.get_data() == tiny_png()

    def test_an_unknown_index_is_a_404(self, client, person):
        assert client.get(f"/api/minutes/people/{person.id}/photo/7").status_code == 404

    def test_an_unknown_person_is_a_404(self, client):
        assert client.get("/api/minutes/people/abcdef123456/photo/0").status_code == 404

    @pytest.mark.parametrize(
        "person_id",
        [
            "..",
            "..%2f..%2fetc%2fpasswd",
            "%2e%2e%2f%2e%2e%2fconfig.yaml",
            "not-a-real-id",
            "ABCDEF123456",  # the regex is lower-case hex, deliberately
        ],
    )
    def test_a_crafted_id_reaches_nothing(self, client, person_id):
        response = client.get(f"/api/minutes/people/{person_id}/photo/0")
        assert response.status_code in (301, 308, 404), person_id
        if response.status_code == 404 and response.is_json:
            assert response.get_json()["ok"] is False

    def test_a_slot_holding_something_that_is_not_an_image_is_not_served(
        self, client, person, service, minutes_paths
    ):
        """Belt and braces: the bytes are checked on the way out as well as in."""
        path = minutes_paths.photo_path(person.id, 0)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"#!/bin/sh\necho no\n")
        service.people.record_photo(person.id, 0)
        assert client.get(f"/api/minutes/people/{person.id}/photo/0").status_code == 404


class TestVoiceSample:
    def test_the_room_records_the_sample(self, client, token, person, service):
        response = post(client, f"/api/minutes/people/{person.id}/voice", token,
                        {"seconds": 20})
        assert response.status_code == 200
        assert response.get_json()["person"]["voices"] == 1
        assert ("record_voice", person.id, 20) in service.calls

    def test_the_length_is_clamped_to_something_sensible(self, client, token, person, service):
        post(client, f"/api/minutes/people/{person.id}/voice", token, {"seconds": 9999})
        assert service.calls[-1][2] == 30
        post(client, f"/api/minutes/people/{person.id}/voice", token, {"seconds": 0})
        assert service.calls[-1][2] == 5

    def test_a_nonsense_length_is_refused(self, client, token, person, service):
        response = post(client, f"/api/minutes/people/{person.id}/voice", token,
                        {"seconds": "a while"})
        assert response.status_code == 400
        assert service.calls == []

    def test_a_refusal_from_the_recorder_is_passed_on(self, client, token, person, service):
        service.voice_result = (False, "A meeting is being recorded. Try again afterwards.")
        response = post(client, f"/api/minutes/people/{person.id}/voice", token, {})
        assert response.status_code == 409
        assert "meeting is being recorded" in response.get_json()["error"]

    def test_an_unknown_person_is_a_404(self, client, token):
        assert post(client, "/api/minutes/people/abcdef123456/voice", token,
                    {}).status_code == 404


class TestSessions:
    def test_meetings_are_listed_newest_first(self, client, minutes_paths):
        make_session(minutes_paths, "20260826-090000-aaaaaaaa")
        make_session(minutes_paths, "20260828-090000-bbbbbbbb")
        rows = client.get("/api/minutes/sessions").get_json()["sessions"]
        assert [row["session_id"] for row in rows] == [
            "20260828-090000-bbbbbbbb", "20260826-090000-aaaaaaaa"
        ]
        assert rows[0]["has_summary"] is True
        assert rows[0]["sent_to"] == 1
        assert rows[0]["provider"] == "teams"

    def test_a_meeting_reads_back_in_full(self, client, minutes_paths):
        session_id = make_session(minutes_paths)
        payload = client.get(f"/api/minutes/sessions/{session_id}").get_json()
        session = payload["session"]
        assert session["summary"]["text"].startswith("The team agreed")
        assert len(session["transcript"]["segments"]) == 3
        assert "Room speaker" in session["speakers"]
        assert session["recipients"] == ["grace@example.com", "dev@example.com"]
        # The enrolled people ride along so the page can offer a name at once.
        assert "people" in payload

    def test_the_transcript_is_never_cached(self, client, minutes_paths):
        session_id = make_session(minutes_paths)
        response = client.get(f"/api/minutes/sessions/{session_id}")
        assert response.headers["Cache-Control"] == "no-store"

    def test_an_unknown_meeting_is_a_404(self, client, token):
        unknown = "/api/minutes/sessions/20200101-000000-deadbeef"
        assert client.get(unknown).status_code == 404
        assert delete(client, unknown, token).status_code == 404
        assert post(client, unknown + "/reprocess", token).status_code == 404

    @pytest.mark.parametrize("session_id", ["..", "not-an-id", "20260828-090000-ZZZZZZZZ"])
    def test_a_crafted_session_id_reaches_nothing(self, client, session_id):
        response = client.get(f"/api/minutes/sessions/{session_id}")
        assert response.status_code in (301, 308, 404)

    def test_a_meeting_can_be_deleted(self, client, token, minutes_paths):
        session_id = make_session(minutes_paths)
        assert delete(client, f"/api/minutes/sessions/{session_id}", token).get_json()["ok"]
        assert minutes_paths.list_session_ids() == []

    def test_a_meeting_can_be_written_up_again(self, client, token, minutes_paths, service):
        session_id = make_session(minutes_paths)
        response = post(client, f"/api/minutes/sessions/{session_id}/reprocess", token)
        assert response.get_json()["ok"] is True
        assert ("reprocess", session_id) in service.calls

    def test_expired_meetings_can_be_swept_now(self, client, token, service):
        response = post(client, "/api/minutes/sweep", token)
        assert response.get_json()["removed"] == 3
        assert ("sweep",) in service.calls


class TestRelabelling:
    def test_naming_a_speaker_fixes_the_transcript(self, client, token, minutes_paths,
                                                   person, service):
        session_id = make_session(minutes_paths)
        response = post(client, f"/api/minutes/sessions/{session_id}/relabel", token,
                        {"label": "Room speaker", "person_id": person.id})
        assert response.status_code == 200
        payload = response.get_json()
        assert "2 lines" in payload["detail"]

        # The refreshed session comes back with the answer, so the page can
        # redraw without asking again.
        segments = payload["session"]["transcript"]["segments"]
        named = [s for s in segments if s["person_id"] == person.id]
        assert len(named) == 2
        assert all(s["speaker"] == "Ada Lovelace" for s in named)
        assert all(s["source"] == "manual" for s in named)

    def test_the_appliance_learns_the_voice(self, client, token, minutes_paths,
                                            person, service):
        session_id = make_session(minutes_paths)
        post(client, f"/api/minutes/sessions/{session_id}/relabel", token,
             {"label": "Room speaker", "person_id": person.id})
        assert client.get("/api/minutes/people").get_json()["people"][0]["voices"] == 1

    def test_a_label_nobody_used_is_refused(self, client, token, minutes_paths, person):
        session_id = make_session(minutes_paths)
        response = post(client, f"/api/minutes/sessions/{session_id}/relabel", token,
                        {"label": "The cat", "person_id": person.id})
        assert response.status_code == 400
        assert "labelled" in response.get_json()["error"]

    def test_a_missing_choice_is_explained_rather_than_500(self, client, token,
                                                           minutes_paths):
        session_id = make_session(minutes_paths)
        assert post(client, f"/api/minutes/sessions/{session_id}/relabel", token,
                    {"label": "Room speaker"}).status_code == 400
        assert post(client, f"/api/minutes/sessions/{session_id}/relabel", token,
                    {"person_id": "abcdef123456"}).status_code == 400

    def test_a_transcript_that_does_not_exist_yet(self, client, token, minutes_paths,
                                                  person):
        session_id = make_session(minutes_paths, "20260828-100000-cccccccc",
                                  transcript=False)
        response = post(client, f"/api/minutes/sessions/{session_id}/relabel", token,
                        {"label": "Room speaker", "person_id": person.id})
        assert response.status_code == 400
        assert "no transcript" in response.get_json()["error"]


class TestTryIt:
    def test_a_test_email_is_sent(self, client, token, monkeypatch):
        from app.minutes import mailer, web

        sent = {}

        def fake_send(config, to):
            sent["to"] = to
            return mailer.Delivery(ok=True, sent_to=[to])

        monkeypatch.setattr(web.mailer, "send_test", fake_send)
        monkeypatch.setattr(web.summarize, "available", lambda config: (True, ""))

        response = post(client, "/api/minutes/test-email", token,
                        {"to": "charlie@example.com"})
        assert response.status_code == 200
        assert sent["to"] == "charlie@example.com"
        assert "charlie@example.com" in response.get_json()["detail"]

    def test_a_working_mailbox_with_no_summary_says_so(self, client, token, monkeypatch):
        """The confusing case: the test arrives and then no minutes ever do."""
        from app.minutes import mailer, web

        monkeypatch.setattr(web.mailer, "send_test",
                            lambda config, to: mailer.Delivery(ok=True, sent_to=[to]))
        monkeypatch.setattr(
            web.summarize, "available",
            lambda config: (False, "No Claude API key has been set."),
        )
        detail = post(client, "/api/minutes/test-email", token,
                      {"to": "charlie@example.com"}).get_json()["detail"]
        assert "No Claude API key" in detail

    def test_a_refusal_from_the_mailer_is_passed_on(self, client, token, monkeypatch):
        from app.minutes import mailer, web

        monkeypatch.setattr(
            web.mailer, "send_test",
            lambda config, to: mailer.Delivery(error="No outgoing mail server has been set."),
        )
        response = post(client, "/api/minutes/test-email", token, {"to": "c@example.com"})
        assert response.status_code == 409
        assert "mail server" in response.get_json()["error"]

    def test_a_nonsense_address_never_reaches_smtp(self, client, token, monkeypatch):
        from app.minutes import web

        def explode(config, to):  # pragma: no cover - must not be called
            raise AssertionError("SMTP was opened for an address with no @ in it")

        monkeypatch.setattr(web.mailer, "send_test", explode)
        assert post(client, "/api/minutes/test-email", token,
                    {"to": "not-an-address"}).status_code == 400

    def test_looking_at_the_room_reports_who_was_seen(self, client, token, mock_config,
                                                      service):
        mock_config.update({"MINUTES_IDENTIFY_FACES": True})
        response = post(client, "/api/minutes/look", token)
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["people"][0]["name"] == "Ada Lovelace"
        assert "Ada Lovelace" in payload["detail"]
        assert ("look",) in service.calls

    def test_an_empty_room_is_said_plainly(self, client, token, mock_config, service):
        mock_config.update({"MINUTES_IDENTIFY_FACES": True})
        service.look_result = {"ok": True, "people": []}
        detail = post(client, "/api/minutes/look", token).get_json()["detail"]
        assert "Nobody" in detail

    def test_the_camera_is_not_opened_when_faces_are_switched_off(self, client, token,
                                                                  mock_config, service):
        mock_config.update({"MINUTES_IDENTIFY_FACES": False})
        response = post(client, "/api/minutes/look", token)
        assert response.status_code == 409
        assert service.calls == []

    def test_a_camera_fault_is_reported(self, client, token, mock_config, service):
        mock_config.update({"MINUTES_IDENTIFY_FACES": True})
        service.look_result = {"ok": False, "error": "No camera was found.", "people": []}
        response = post(client, "/api/minutes/look", token)
        assert response.status_code == 409
        assert response.get_json()["error"] == "No camera was found."
