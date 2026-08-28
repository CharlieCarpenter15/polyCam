"""``roomctl minutes`` — the terminal's way into the meeting-minutes feature.

The script is driven the way an engineer drives it: as a subprocess, against a
throw-away ``ROOM_APPLIANCE_VAR`` holding hand-written session directories, so
what these tests prove is true of the file that ships rather than of a mock.

Three situations matter and all three are covered here, because the whole point
of the subcommand is that it keeps working when the web page cannot:

*The appliance is running.* A small HTTP server stands in for it, speaking the
shapes ``app/minutes/web.py`` really answers with — including the CSRF check on
everything that changes something, so that a script which forgot to carry the
page token fails here rather than in a meeting room.

*The appliance is not running.* Nothing answers on the port, and the commands
have to fall back to the files and say that they have.

*The feature is switched off.* That is a choice, not a fault, so it is said
plainly and nothing exits non-zero over it.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ROOMCTL = ROOT / "scripts" / "roomctl"

#: The token the fake appliance puts in its pages and demands back.
PAGE_TOKEN = "page-token-for-the-tests"
SESSION_COOKIE = "session=a-fake-flask-session"


# ---------------------------------------------------------------------------
# A room to point the script at
# ---------------------------------------------------------------------------


def write_session(
    sessions,
    session_id,
    *,
    title="A meeting",
    provider="teams",
    stage="sent",
    started="2026-08-28T09:00:00+00:00",
    ended="2026-08-28T09:32:00+00:00",
    error="",
    summary=None,
    sent_to=None,
    segments=None,
):
    """One session directory, written the way MinutesService writes one."""
    directory = sessions / session_id
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "started_at": started,
                "ended_at": ended,
                "meeting_id": "meeting-1",
                "title": title,
                "provider": provider,
                "room": "Boardroom",
                "organizer": "chair@example.com",
                "invited": ["chair@example.com"],
                "stage": stage,
                "error": error,
            }
        ),
        encoding="utf-8",
    )
    if segments is not None:
        (directory / "transcript.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "meta": {"session_id": session_id, "title": title},
                    "segments": segments,
                    "participants": [
                        {
                            "name": "Alice Smith",
                            "email": "alice@example.com",
                            "person_id": "aaaaaaaaaaaa",
                            "where": "room",
                            "source": "face",
                        }
                    ],
                    "notices": [],
                }
            ),
            encoding="utf-8",
        )
    if summary is not None:
        (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    if sent_to is not None:
        (directory / "delivery.json").write_text(
            json.dumps({"ok": True, "sent_to": sent_to, "error": ""}), encoding="utf-8"
        )
    return directory


#: The two meetings that share a prefix are there on purpose: resolving an
#: ambiguous id is a behaviour, not an accident.
MORNING = "20260828-090000-aaaaaaaa"
AFTERNOON = "20260828-140000-bbbbbbbb"
LATER = "20260828-141500-bbbbbbbc"
YESTERDAY = "20260827-101500-cccccccc"

SPEECH = [
    {
        "start": 0.0,
        "end": 4.0,
        "text": "Morning everyone.",
        "track": "room",
        "speaker": "Alice Smith",
        "person_id": "aaaaaaaaaaaa",
        "source": "face",
        "confidence": 0.9,
    },
    {
        "start": 65.0,
        "end": 70.0,
        "text": "Can you hear me at the back?",
        "track": "far-end",
        "speaker": "",
        "person_id": "",
        "source": "",
        "confidence": 0.0,
    },
]

#: A face and a voice are stored as embeddings. They are biometric data, and
#: the numbers below exist so that a test can prove none of them is ever
#: printed — not that the counts are right.
FACE_VECTOR = [0.111111, 0.222222, 0.333333]
VOICE_VECTOR = [0.444444, 0.555555]


class Room:
    """A throw-away appliance directory, and a way to run roomctl against it."""

    def __init__(self, var: Path, config_dir: Path) -> None:
        self.var = var
        self.config_dir = config_dir
        self.config_file = config_dir / "config.yaml"
        self.sessions = var / "minutes" / "sessions"
        self.people_file = var / "minutes" / "people" / "people.json"
        self.port = free_port()
        self.configure()

    def configure(self, *, enabled: bool = True, port: int | None = None) -> None:
        self.config_file.write_text(
            "MINUTES_ENABLED: {}\nDASHBOARD_PORT: {}\n".format(
                "true" if enabled else "false", port or self.port
            ),
            encoding="utf-8",
        )

    def env(self) -> dict[str, str]:
        import os

        env = {k: v for k, v in os.environ.items() if not k.startswith("ROOM_")}
        env["ROOM_APPLIANCE_VAR"] = str(self.var)
        env["ROOM_APPLIANCE_CONFIG_DIR"] = str(self.config_dir)
        # A fixed clock keeps the "when" column the same on every machine.
        env["TZ"] = "UTC"
        return env

    def run(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(ROOMCTL), *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(ROOT),
            env=self.env(),
        )


def free_port() -> int:
    """A port nothing is listening on, so "not running" really means it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture()
def room(tmp_path) -> Room:
    var = tmp_path / "var"
    config_dir = tmp_path / "config"
    sessions = var / "minutes" / "sessions"
    sessions.mkdir(parents=True)
    (var / "minutes" / "people").mkdir(parents=True)
    config_dir.mkdir(parents=True)

    write_session(
        sessions,
        MORNING,
        title="Engineering Daily",
        provider="teams",
        stage="sent",
        summary={"ok": True, "text": "Decisions:\n- Ship on Friday.", "error": ""},
        sent_to=["alice@example.com", "chair@example.com"],
        segments=SPEECH,
    )
    write_session(
        sessions,
        AFTERNOON,
        title="Supplier call about the new packaging line",
        provider="meet",
        stage="transcribed",
        started="2026-08-28T14:00:00+00:00",
        ended="2026-08-28T14:20:00+00:00",
        segments=SPEECH[:1],
    )
    write_session(
        sessions,
        LATER,
        title="Second afternoon meeting",
        provider="teams",
        stage="captured",
        started="2026-08-28T14:15:00+00:00",
        ended="",
    )
    write_session(
        sessions,
        YESTERDAY,
        title="Board",
        provider="zoom",
        stage="failed",
        started="2026-08-27T10:15:00+00:00",
        ended="2026-08-27T11:00:00+00:00",
        error="Transcription ran out of memory.",
    )

    (var / "minutes" / "people" / "people.json").write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-08-28T09:00:00+00:00",
                "people": [
                    {
                        "id": "aaaaaaaaaaaa",
                        "name": "Alice Smith",
                        "email": "alice@example.com",
                        "notes": "",
                        "face": [{"model": "arcface", "values": FACE_VECTOR}],
                        "voice": [{"model": "ecapa", "values": VOICE_VECTOR}],
                        "photos": [0],
                    },
                    {
                        "id": "bbbbbbbbbbbb",
                        "name": "Bob Jones",
                        "email": "",
                        "notes": "",
                        "face": [],
                        "voice": [],
                        "photos": [],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return Room(var, config_dir)


# ---------------------------------------------------------------------------
# A stand-in for the running appliance
# ---------------------------------------------------------------------------


#: What ``/api/minutes/status`` answers with. The capability reasons are the
#: real ones from ``app/minutes/summarize.py`` and friends: they are written to
#: be acted on, and the point of the test is that they reach the terminal whole.
STATUS = {
    "ok": True,
    "enabled": True,
    "recording": {"session_id": LATER, "title": "Second afternoon meeting", "seconds": 252},
    "working_on": "transcribing " + AFTERNOON,
    "queued": 1,
    "people": {"people": 2, "with_face": 1, "with_voice": 1},
    "sessions": 4,
    "capabilities": {
        "audio": {"ok": True, "detail": ""},
        "transcribe": {"ok": True, "detail": ""},
        "roster": {"ok": True, "detail": ""},
        "faces": {"ok": True, "detail": ""},
        "voices": {"ok": True, "detail": ""},
        "summary": {
            "ok": False,
            "detail": "No Claude API key has been set, so there is nothing to "
            "send the transcript to. Add one on the Settings page.",
        },
        "email": {"ok": False, "detail": "Emailing the summary is switched off."},
    },
    "last_error": "",
    # The real route never sends a secret. This one does, so that a renderer
    # which printed whatever it was handed would be caught here.
    "settings": {"stt_engine": "whisper", "api_key": "sk-must-never-be-printed"},
}


class Handler(BaseHTTPRequestHandler):
    """Just enough of app/minutes/web.py to hold the script to its contract."""

    server_version = "FakeRoom/1"

    def log_message(self, *_args):  # noqa: D102 - keep the test output quiet
        return

    # -- helpers ---------------------------------------------------------
    def _json(self, payload, status=200, cookie=False):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}; HttpOnly; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        """The same two things ``require_csrf`` wants: the cookie and the token."""
        if getattr(self.server, "refuse_writes", False):
            return False
        return (
            SESSION_COOKIE in (self.headers.get("Cookie") or "")
            and self.headers.get("X-Room-Token") == PAGE_TOKEN
        )

    def _refuse(self):
        self._json(
            {"ok": False, "error": "This page is out of date. Reload it and try again."},
            403,
        )

    def _record(self, method):
        self.server.calls.append((method, self.path, dict(self.headers)))

    # -- routes ----------------------------------------------------------
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        self._record("GET")
        path = self.path.split("?")[0]
        if path == "/api/health":
            self._json({"status": "ok", "components": {}})
            return
        if path == "/":
            page = (
                '<!doctype html><html><body class="admin" '
                f'data-csrf="{PAGE_TOKEN}">the room</body></html>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}; HttpOnly; Path=/")
            self.end_headers()
            self.wfile.write(page)
            return
        if path == "/api/minutes/status":
            self._json(self.server.status)
            return
        if path == "/api/minutes/sessions":
            self._json({"ok": True, "sessions": self.server.rows, "keep_days": 30})
            return
        if path.startswith("/api/minutes/sessions/"):
            session_id = path.rsplit("/", 1)[-1]
            session = self.server.detail.get(session_id)
            if session is None:
                self._json({"ok": False, "error": "No such recording."}, 404)
                return
            self._json({"ok": True, "session": session, "people": self.server.people})
            return
        if path == "/api/minutes/people":
            self._json(
                {
                    "ok": True,
                    "people": self.server.people,
                    "stats": {"people": 2, "with_face": 1, "with_voice": 1},
                }
            )
            return
        self._json({"ok": False, "error": "No such endpoint."}, 404)

    def do_POST(self):  # noqa: N802
        self._record("POST")
        if not self._authorised():
            self._refuse()
            return
        if self.path.endswith("/reprocess"):
            self._json({"ok": True, "detail": "Queued. It will be written up again in a moment."})
            return
        if self.path == "/api/minutes/sweep":
            self._json({"ok": True, "removed": 2, "detail": "Deleted 2 meetings older than 30 days."})
            return
        self._json({"ok": False, "error": "No such endpoint."}, 404)

    def do_DELETE(self):  # noqa: N802
        self._record("DELETE")
        if not self._authorised():
            self._refuse()
            return
        self._json(
            {"ok": True, "detail": "That meeting — audio, transcript and summary — has been deleted."}
        )


@pytest.fixture()
def appliance(room):
    """The fake appliance, listening on the port the room is configured for."""
    server = ThreadingHTTPServer(("127.0.0.1", room.port), Handler)
    server.calls = []
    server.refuse_writes = False
    server.status = json.loads(json.dumps(STATUS))
    server.rows = [
        {
            "session_id": LATER,
            "title": "Second afternoon meeting",
            "started_at": "2026-08-28T14:15:00+00:00",
            "ended_at": "",
            "provider": "teams",
            "stage": "recording",
            "error": "",
            "has_summary": False,
            "has_audio": True,
            "sent_to": 0,
        },
        {
            "session_id": AFTERNOON,
            "title": "Supplier call about the new packaging line",
            "started_at": "2026-08-28T14:00:00+00:00",
            "ended_at": "2026-08-28T14:20:00+00:00",
            "provider": "meet",
            "stage": "transcribed",
            "error": "",
            "has_summary": False,
            "has_audio": True,
            "sent_to": 0,
        },
        {
            "session_id": MORNING,
            "title": "Engineering Daily",
            "started_at": "2026-08-28T09:00:00+00:00",
            "ended_at": "2026-08-28T09:32:00+00:00",
            "provider": "teams",
            "stage": "sent",
            "error": "",
            "has_summary": True,
            "has_audio": False,
            "sent_to": 2,
        },
    ]
    server.detail = {
        MORNING: {
            "session_id": MORNING,
            "meta": json.loads((room.sessions / MORNING / "meta.json").read_text()),
            "transcript": json.loads((room.sessions / MORNING / "transcript.json").read_text()),
            "text": "[00:00] Alice Smith: Morning everyone.\n"
            "[01:05] Remote speaker: Can you hear me at the back?",
            "speakers": ["Alice Smith", "Remote speaker"],
            "summary": {"ok": True, "text": "Decisions:\n- Ship on Friday.", "error": ""},
            "delivery": {"ok": True, "sent_to": ["alice@example.com"], "error": ""},
            "recipients": ["alice@example.com", "chair@example.com"],
        }
    }
    server.people = [
        {
            "id": "aaaaaaaaaaaa",
            "name": "Alice Smith",
            "email": "alice@example.com",
            "faces": 1,
            "voices": 1,
            "photos": [0],
        },
        {"id": "bbbbbbbbbbbb", "name": "Bob Jones", "email": "", "faces": 0, "voices": 0},
    ]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def wrote(server, method, needle):
    """Did the script make that call, carrying the page token?"""
    for call_method, path, headers in server.calls:
        if call_method == method and needle in path:
            return headers
    return None


# ---------------------------------------------------------------------------
# The script itself
# ---------------------------------------------------------------------------


class TestTheScript:
    def test_the_syntax_is_still_valid(self):
        result = subprocess.run(
            ["bash", "-n", str(ROOMCTL)], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr

    def test_help_needs_no_configuration_and_names_the_new_command(self, tmp_path):
        """`--help` has to work on a Pi with nothing set up yet."""
        import os

        env = {k: v for k, v in os.environ.items() if not k.startswith("ROOM_")}
        env["ROOM_APPLIANCE_VAR"] = str(tmp_path / "nowhere")
        env["ROOM_APPLIANCE_CONFIG_DIR"] = str(tmp_path / "nothing")
        result = subprocess.run(
            [str(ROOMCTL), "--help"], capture_output=True, text=True, timeout=60, env=env
        )
        assert result.returncode == 0, result.stderr
        assert len(result.stdout) > 80
        for line in (
            "roomctl minutes",
            "roomctl minutes list",
            "roomctl minutes show",
            "roomctl minutes process",
            "roomctl minutes delete",
            "roomctl minutes sweep",
            "roomctl minutes people",
        ):
            assert line in result.stdout, line

    def test_minutes_help_prints_the_same_help(self, room):
        result = room.run("minutes", "help")
        assert result.returncode == 0
        assert "roomctl minutes list" in result.stdout

    def test_an_unknown_action_says_what_the_actions_are(self, room):
        result = room.run("minutes", "wibble")
        assert result.returncode == 1
        assert "wibble" in result.stdout
        assert "status | list | show | process | delete | sweep | people" in result.stdout


# ---------------------------------------------------------------------------
# Every action dispatches, with nothing listening on the port
# ---------------------------------------------------------------------------


class TestDispatch:
    @pytest.mark.parametrize(
        "args, expected",
        [
            ([], 0),
            (["status"], 0),
            (["list"], 0),
            (["list", "2"], 0),
            (["show"], 0),
            (["show", MORNING], 0),
            (["show", MORNING, "--transcript"], 0),
            (["show", MORNING, "--summary"], 0),
            (["people"], 0),
            (["process", MORNING], 1),
            (["sweep"], 1),
            (["delete", MORNING, "--yes"], 0),
        ],
    )
    def test_every_action_runs_without_an_appliance(self, room, args, expected):
        result = room.run("minutes", *args)
        assert result.returncode == expected, (args, result.stdout, result.stderr)

    def test_a_bad_count_is_refused_rather_than_guessed_at(self, room):
        result = room.run("minutes", "list", "soon")
        assert result.returncode == 1
        assert "roomctl minutes list" in result.stdout

    def test_an_unknown_flag_on_show_names_the_ones_that_exist(self, room):
        result = room.run("minutes", "show", "--nope")
        assert result.returncode == 1
        assert "--transcript" in result.stdout


# ---------------------------------------------------------------------------
# Switched off
# ---------------------------------------------------------------------------


class TestSwitchedOff:
    def test_status_says_so_and_points_at_the_setting(self, room):
        room.configure(enabled=False)
        result = room.run("minutes")
        assert result.returncode == 0, result.stderr
        assert "switched off" in result.stdout
        assert "MINUTES_ENABLED" in result.stdout

    def test_what_was_recorded_before_is_still_readable(self, room):
        room.configure(enabled=False)
        result = room.run("minutes", "list")
        assert result.returncode == 0
        assert "MINUTES_ENABLED" in result.stdout
        assert "Engineering Daily" in result.stdout

    def test_a_job_only_the_appliance_can_do_says_which_switch_to_flip(self, room):
        room.configure(enabled=False)
        result = room.run("minutes", "process", MORNING)
        assert result.returncode == 1
        assert "MINUTES_ENABLED" in result.stdout


# ---------------------------------------------------------------------------
# Running, but no appliance answering
# ---------------------------------------------------------------------------


class TestWithoutTheAppliance:
    def test_status_says_live_status_is_unavailable_and_reads_the_disk(self, room):
        result = room.run("minutes", "status")
        assert result.returncode == 0, result.stderr
        assert "not answering on port" in result.stdout
        assert "cannot be known from here" in result.stdout
        assert "4 on this appliance" in result.stdout

    def test_list_reads_the_session_directories(self, room):
        result = room.run("minutes", "list")
        assert result.returncode == 0
        for expected in (MORNING, AFTERNOON, LATER, YESTERDAY, "Engineering Daily"):
            assert expected in result.stdout
        # Newest first, the way the ids sort.
        assert result.stdout.index(LATER) < result.stdout.index(YESTERDAY)

    def test_list_takes_a_count(self, room):
        result = room.run("minutes", "list", "2")
        assert LATER in result.stdout
        assert YESTERDAY not in result.stdout

    def test_show_reads_the_meeting_off_the_disk(self, room):
        result = room.run("minutes", "show", MORNING)
        assert result.returncode == 0
        assert "Engineering Daily" in result.stdout
        assert "Alice Smith" in result.stdout
        assert "Ship on Friday" in result.stdout
        assert "Morning everyone." in result.stdout

    def test_a_bare_show_is_the_most_recent_meeting(self, room):
        result = room.run("minutes", "show")
        assert result.returncode == 0
        assert LATER in result.stdout
        assert MORNING not in result.stdout

    def test_a_failed_meeting_shows_why_it_failed(self, room):
        result = room.run("minutes", "show", YESTERDAY)
        assert "Transcription ran out of memory." in result.stdout

    def test_people_are_read_from_the_people_file(self, room):
        result = room.run("minutes", "people")
        assert result.returncode == 0
        assert "Alice Smith" in result.stdout
        assert "Bob Jones" in result.stdout

    def test_sweep_refuses_rather_than_applying_the_policy_itself(self, room):
        result = room.run("minutes", "sweep")
        assert result.returncode == 1
        assert "Nothing was deleted" in result.stdout
        # It still explains what the policy is, which is half the question.
        assert "kept for 30 day(s)" in result.stdout
        assert len(list(room.sessions.iterdir())) == 4


# ---------------------------------------------------------------------------
# Resolving a short id
# ---------------------------------------------------------------------------


class TestPrefixes:
    def test_an_unambiguous_prefix_resolves(self, room):
        result = room.run("minutes", "show", "20260828-09")
        assert result.returncode == 0
        assert MORNING in result.stdout

    def test_an_ambiguous_prefix_is_refused_and_the_candidates_listed(self, room):
        result = room.run("minutes", "show", "20260828-14")
        assert result.returncode == 1
        assert "more than one meeting" in result.stderr
        assert AFTERNOON in result.stderr
        assert LATER in result.stderr
        # Nothing was guessed at and nothing was printed as if it had been.
        assert "Supplier call" not in result.stdout

    def test_a_prefix_that_matches_nothing_says_so(self, room):
        result = room.run("minutes", "show", "19990101")
        assert result.returncode == 1
        assert "19990101" in result.stderr
        assert "roomctl minutes list" in result.stderr

    def test_a_prefix_resolves_for_delete_too(self, room):
        result = room.run("minutes", "delete", "20260827", "--yes")
        assert result.returncode == 0
        assert not (room.sessions / YESTERDAY).exists()


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------


class TestDelete:
    def test_no_at_the_prompt_deletes_nothing(self, room):
        result = room.run("minutes", "delete", MORNING, stdin="n\n")
        assert result.returncode == 0
        assert "Cancelled" in result.stdout
        assert (room.sessions / MORNING / "meta.json").exists()

    def test_an_empty_answer_deletes_nothing(self, room):
        result = room.run("minutes", "delete", MORNING, stdin="\n")
        assert result.returncode == 0
        assert (room.sessions / MORNING / "meta.json").exists()

    def test_the_prompt_shows_which_meeting_is_about_to_go(self, room):
        result = room.run("minutes", "delete", MORNING, stdin="n\n")
        assert "Engineering Daily" in result.stdout
        assert "no undo" in result.stdout

    def test_yes_at_the_prompt_deletes_it(self, room):
        result = room.run("minutes", "delete", MORNING, stdin="y\n")
        assert result.returncode == 0
        assert not (room.sessions / MORNING).exists()
        assert (room.sessions / AFTERNOON).exists()

    def test_the_yes_flag_skips_the_prompt(self, room):
        result = room.run("minutes", "delete", MORNING, "--yes")
        assert result.returncode == 0
        assert "[y/N]" not in result.stdout
        assert not (room.sessions / MORNING).exists()

    def test_delete_needs_an_id(self, room):
        result = room.run("minutes", "delete")
        assert result.returncode == 1
        assert "roomctl minutes delete" in result.stdout
        assert len(list(room.sessions.iterdir())) == 4


# ---------------------------------------------------------------------------
# With the appliance answering
# ---------------------------------------------------------------------------


class TestWithTheAppliance:
    def test_status_comes_from_the_appliance(self, room, appliance):
        result = room.run("minutes", "status")
        assert result.returncode == 0, result.stderr
        assert "not answering" not in result.stdout
        assert "Second afternoon meeting" in result.stdout   # recording now
        assert "04:12" in result.stdout                      # 252 seconds in
        assert "transcribing" in result.stdout               # what the worker is at

    def test_every_unavailable_capability_says_why(self, room, appliance):
        result = room.run("minutes", "status")
        assert "No Claude API key has been set" in result.stdout
        assert "Add one on the Settings page." in result.stdout
        assert "Emailing the summary is switched off." in result.stdout

    def test_no_secret_the_appliance_sends_is_ever_printed(self, room, appliance):
        result = room.run("minutes", "status")
        assert "sk-must-never-be-printed" not in result.stdout
        assert "sk-must-never-be-printed" not in result.stderr

    def test_list_comes_from_the_appliance_not_the_disk(self, room, appliance):
        # Only the appliance knows this one is recording right now.
        result = room.run("minutes", "list")
        assert result.returncode == 0
        assert "recording" in result.stdout
        assert "Engineering Daily" in result.stdout

    def test_show_comes_from_the_appliance(self, room, appliance):
        result = room.run("minutes", "show", MORNING)
        assert result.returncode == 0
        assert "Ship on Friday" in result.stdout
        assert "Can you hear me at the back?" in result.stdout

    def test_the_transcript_flag_pipes_cleanly(self, room, appliance):
        result = room.run("minutes", "show", MORNING, "--transcript")
        assert result.returncode == 0
        assert result.stdout == (
            "[00:00] Alice Smith: Morning everyone.\n"
            "[01:05] Remote speaker: Can you hear me at the back?\n"
        )
        assert "\033" not in result.stdout

    def test_the_summary_flag_pipes_cleanly(self, room, appliance):
        result = room.run("minutes", "show", MORNING, "--summary")
        assert result.returncode == 0
        assert result.stdout == "Decisions:\n- Ship on Friday.\n"

    def test_process_queues_the_meeting_with_the_page_token(self, room, appliance):
        result = room.run("minutes", "process", MORNING)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Queued" in result.stdout
        headers = wrote(appliance, "POST", f"/api/minutes/sessions/{MORNING}/reprocess")
        assert headers is not None
        assert headers.get("X-Room-Token") == PAGE_TOKEN
        assert SESSION_COOKIE in (headers.get("Cookie") or "")

    def test_delete_goes_through_the_appliance_and_not_the_filesystem(self, room, appliance):
        result = room.run("minutes", "delete", MORNING, "--yes")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "has been deleted" in result.stdout
        assert wrote(appliance, "DELETE", f"/api/minutes/sessions/{MORNING}") is not None
        # The appliance owns those files; this script must not have touched them.
        assert (room.sessions / MORNING / "meta.json").exists()

    def test_sweep_asks_the_appliance_to_apply_the_policy(self, room, appliance):
        result = room.run("minutes", "sweep")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Deleted 2 meetings older than 30 days." in result.stdout
        assert wrote(appliance, "POST", "/api/minutes/sweep") is not None

    def test_people_come_from_the_appliance_without_their_vectors(self, room, appliance):
        result = room.run("minutes", "people")
        assert result.returncode == 0
        assert "Alice Smith" in result.stdout
        assert "0.111111" not in result.stdout
        assert "0.444444" not in result.stdout

    def test_a_refusal_from_the_appliance_is_reported_not_swallowed(self, room, appliance):
        """What the failure looks like if the page token ever stops being sent."""
        appliance.refuse_writes = True
        result = room.run("minutes", "process", MORNING)
        assert result.returncode == 1
        assert "out of date" in result.stderr
        assert "Queued" not in result.stdout


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


class TestPrivacy:
    def test_the_transcript_never_lands_in_a_file(self, room, tmp_path):
        """A private meeting's words go to stdout and nowhere else."""
        words = "Can you hear me at the back?"   # said in this meeting and no other
        result = room.run("minutes", "show", MORNING, "--transcript")
        assert words in result.stdout
        holding = {
            path
            for path in tmp_path.rglob("*")
            if path.is_file() and words in path.read_text(errors="ignore")
        }
        # The only copy on this disk is the one the appliance itself wrote.
        assert holding == {room.sessions / MORNING / "transcript.json"}

    def test_an_embedding_is_never_printed(self, room):
        result = room.run("minutes", "people")
        assert "0.111111" not in result.stdout
        assert "0.555555" not in result.stdout
        assert "1" in result.stdout  # the counts are, though
