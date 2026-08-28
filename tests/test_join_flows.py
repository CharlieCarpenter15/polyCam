"""Join automation: the parts that can be tested without a live meeting.

The injected JavaScript itself is exercised against a real DOM by
``tests/js/test_clicker.js`` (optional; needs Node and jsdom). What is tested
here is everything Python decides: which buttons are tried, in what order, what
happens to the meeting URL, and — in :class:`TestJoinLoop` — that exactly one
join loop is ever pressing at the page.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.browser_service import BrowserService
from app.join_flows import (
    GENERIC_FLOW,
    PROVIDER_FLOWS,
    build_click_script,
    build_in_call_script,
    build_mute_script,
    flow_for,
    ordered_button_texts,
    prepare_url,
)
from app.meeting_links import PROVIDERS
from app.models import Meeting
from app.system_service import SystemService


class TestFlowDefinitions:
    def test_the_three_required_providers_have_flows(self):
        for provider in ("teams", "meet", "zoom"):
            assert provider in PROVIDER_FLOWS

    def test_every_flow_documents_its_limitations(self):
        """The notes end up on the diagnostics page; they must not be blank."""
        for provider, flow in PROVIDER_FLOWS.items():
            assert flow.notes.strip(), f"{provider} has no notes"

    def test_an_unknown_provider_gets_the_generic_flow(self):
        assert flow_for("carrier-pigeon") is GENERIC_FLOW
        assert flow_for("") is GENERIC_FLOW

    def test_every_recognised_provider_resolves_to_some_flow(self):
        for provider in PROVIDERS:
            assert flow_for(provider.id) is not None


class TestButtonOrdering:
    def test_the_browser_step_comes_before_joining(self):
        """Pressing "Join now" before "Continue on this browser" fails on Teams."""
        order = ordered_button_texts("teams", ["Join now"])
        assert order.index("Continue on this browser") < order.index("Join now")

    def test_administrator_additions_are_kept(self):
        order = ordered_button_texts("meet", ["Join now", "Beitreten"])
        assert "Beitreten" in order

    def test_duplicates_are_removed_case_insensitively(self):
        order = ordered_button_texts("meet", ["join now", "JOIN NOW", "Join now"])
        assert sum(1 for text in order if text.lower() == "join now") == 1

    def test_blank_entries_are_ignored(self):
        order = ordered_button_texts("meet", ["", "   ", "Join now"])
        assert "" not in order and "   " not in order

    def test_configured_texts_alone_still_work_for_an_unknown_provider(self):
        order = ordered_button_texts("", ["Enter meeting"])
        assert "Enter meeting" in order


class TestScriptBuilding:
    def test_the_configured_texts_are_embedded_as_json(self):
        script = build_click_script(["Join now", 'Say "hello"'])
        assert '"Join now"' in script
        assert "__PATTERNS__" not in script

    def test_hostile_button_text_cannot_break_the_script(self):
        """Whatever an administrator types must stay inside a string literal.

        The script is evaluated over the DevTools protocol rather than inlined
        into HTML, so the risk is not ``</script>`` but a quote or backslash
        escaping the array literal and turning the rest into code.
        """
        nasty = [
            'O\'Brien\'s "Join"',
            "</script><script>bad()",
            "back\\slash",
            "new\nline",
            "unicode \u2028 separator",
        ]
        script = build_click_script(nasty, display_name='Room "A"\\', fill_name=True)

        embedded = script.split("var WANTED = ")[1].split(";\n")[0]
        assert json.loads(embedded) == nasty, "the texts must survive intact"

        name = script.split("var NAME = ")[1].split(";\n")[0]
        assert json.loads(name) == 'Room "A"\\'

    @pytest.mark.parametrize(
        "texts",
        [
            ["Join now"],
            ['O\'Brien\'s "Join"', "</script><script>bad()", "back\\slash"],
            ["ünïcödé", "日本語で参加", "emoji 🎥"],
            [],
        ],
    )
    def test_the_generated_script_is_valid_javascript(self, texts, tmp_path):
        """Verified with Node where available, so a bad escape cannot slip out."""
        import shutil
        import subprocess

        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            pytest.skip("Node is not installed (it is not needed on the appliance)")

        for script, label in (
            (build_click_script(texts, display_name='A "Room"', fill_name=True), "clicker"),
            (build_in_call_script(), "in-call probe"),
        ):
            path = tmp_path / f"{label.replace(' ', '-')}.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run(
                [node, "--check", str(path)], capture_output=True, text=True, timeout=30
            )
            assert result.returncode == 0, f"{label} is not valid JS:\n{result.stderr}"

    def test_the_display_name_is_embedded_safely(self):
        script = build_click_script(["Join"], display_name='Room "A"', fill_name=True)
        embedded = script.split("var NAME = ")[1].split(";\n")[0]
        assert json.loads(embedded) == 'Room "A"'
        assert "var FILL_NAME = true" in script

    def test_name_filling_is_off_by_default(self):
        assert "var FILL_NAME = false" in build_click_script(["Join"])

    def test_no_placeholders_are_left_behind(self):
        script = build_click_script(["Join"], display_name="Room", fill_name=True)
        for placeholder in ("__PATTERNS__", "__NAME__", "__FILL_NAME__"):
            assert placeholder not in script

    def test_the_in_call_probe_looks_for_a_leave_control(self):
        script = build_in_call_script()
        assert "leave call" in script
        assert "hang up" in script


class TestUrlPreparation:
    def test_zoom_is_asked_for_its_web_client(self):
        """Raspberry Pi OS has no Zoom desktop app, so the web client is the only option."""
        prepared = prepare_url("zoom", "https://us02web.zoom.us/j/123?pwd=x")
        assert "web=1" in prepared
        assert "pwd=x" in prepared, "the passcode must survive"

    def test_other_providers_are_left_untouched(self):
        for provider, url in (
            ("teams", "https://teams.microsoft.com/l/meetup-join/19%3aX/0?context=%7b%7d"),
            ("meet", "https://meet.google.com/abc-defg-hij"),
        ):
            assert prepare_url(provider, url) == url

    def test_an_existing_parameter_is_not_overwritten(self):
        prepared = prepare_url("zoom", "https://us02web.zoom.us/j/123?web=0")
        assert "web=0" in prepared

    def test_an_empty_url_is_safe(self):
        assert prepare_url("zoom", "") == ""


class TestNameFilling:
    """The guest name box: fix the fill and the room stops asking for a name."""

    def test_the_native_value_setter_is_used(self):
        """A plain ``el.value = name`` is a no-op on React (Teams, Meet).

        React tracks the value on the node, sees no change, and re-renders the
        empty box — which is what left rooms sitting on the pre-join screen.
        """
        script = build_click_script(["Join now"], display_name="Room", fill_name=True)
        assert "getOwnPropertyDescriptor" in script
        assert "HTMLInputElement.prototype" in script
        assert 'dispatch(el, "input")' in script and 'dispatch(el, "change")' in script

    def test_the_fill_is_verified_before_it_is_reported(self):
        """``filled_name`` has to be the truth, or the loop waits for nothing."""
        script = build_click_script(["Join now"], display_name="Room", fill_name=True)
        assert "return el.value === text;" in script

    def test_contenteditable_name_boxes_are_handled(self):
        """Zoom and Webex use a contenteditable div in places."""
        script = build_click_script(["Join"], display_name="Room", fill_name=True)
        assert 'contenteditable="true"' in script
        assert "el.textContent = text" in script

    def test_sensitive_fields_are_refused(self):
        """Typing the room name into a passcode box locks the room out."""
        script = build_click_script(["Join"], display_name="Room", fill_name=True)
        blockers = script.split("var NAME_BLOCKERS = ")[1].split(";")[0]
        for word in ("meeting", "code", "passcode", "password", "email"):
            assert f'"{word}"' in blockers
        # "id" and "pin" are matched as whole words: "video" and "hidden" both
        # contain an "id", and neither is a reason to skip a name box.
        assert "(id|pin)" in script

    def test_a_pass_that_fills_a_name_does_not_also_click(self):
        """Clicking Join in the same pass either misses or bounces back.

        The button is disabled until the page has processed the name, so the
        pass returns and the next one — a couple of seconds later — clicks.
        """
        script = build_click_script(["Join now"], display_name="Room", fill_name=True)
        assert "if (filled || waiting) {" in script


class TestRepeatGuardScript:
    def test_no_guard_by_default(self):
        assert "var GUARDED = [];" in build_click_script(["Join now"])

    def test_a_guarded_button_carries_the_page_it_was_pressed_on(self):
        """The URL is what releases the guard: a new page means a new button."""
        script = build_click_script(
            ["Join now"], guarded_clicks=[("join now", "https://meet.google.com/x")]
        )
        embedded = json.loads(script.split("var GUARDED = ")[1].split(";\n")[0])
        assert embedded == [{"text": "join now", "url": "https://meet.google.com/x"}]
        assert "entry.url === location.href" in script

    def test_hostile_button_text_cannot_escape_the_guard_list(self):
        nasty = 'O\'Brien\'s "Join"\\'
        script = build_click_script(["Join"], guarded_clicks=[(nasty, "https://x/?a=b&c")])
        embedded = json.loads(script.split("var GUARDED = ")[1].split(";\n")[0])
        assert embedded[0]["text"] == nasty


class TestMutePass:
    def test_the_mute_pass_can_never_unmute(self):
        """"Mute" is a substring of "Unmute" — without the deny list, a quiet
        room gets unmuted on the way into the call."""
        script = build_mute_script()
        wanted = json.loads(script.split("var WANTED = ")[1].split(";\n")[0])
        avoid = json.loads(script.split("var AVOID = ")[1].split(";\n")[0])
        assert "Mute microphone" in wanted and "Turn off microphone" in wanted
        assert "unmute" in avoid and "turn on microphone" in avoid

    def test_the_mute_pass_never_types_anything(self):
        assert "var FILL_NAME = false" in build_mute_script()


class TestLobbyDetection:
    def test_the_usual_waiting_room_wording_is_recognised(self):
        script = build_click_script(["Join now"])
        for phrase in (
            "asking to be let in",
            "waiting for the host",
            "waiting for the meeting to start",
            "someone lets you in",
            "waiting to be admitted",
        ):
            assert phrase in script

    def test_please_wait_alone_is_not_a_lobby(self):
        """Every pre-join page says "join" and a loading page says "please wait"."""
        script = build_click_script(["Join now"])
        clause = script.split('if (text.indexOf("please wait")')[1].split("return")[0]
        assert 'indexOf("host")' in clause
        assert 'indexOf("join")' not in clause

    def test_the_in_call_probe_knows_more_than_one_provider(self):
        script = build_in_call_script()
        for hint in ("leave call", "hang up", "leave the meeting", "end the call"):
            assert hint in script
        assert 'getAttribute("title")' in script


# ---------------------------------------------------------------------------
# The join loop
# ---------------------------------------------------------------------------

PAGE = "https://meet.google.com/abc-defg-hij"


def a_meeting(uid: str = "daily") -> Meeting:
    now = datetime.now(timezone.utc)
    return Meeting(
        uid=uid,
        title="Engineering Daily",
        start=now,
        end=now + timedelta(minutes=30),
        provider_id="meet",
        join_url=PAGE,
    )


def embedded(script: str, name: str):
    """Read a ``var NAME = <json>;`` line back out of a rendered script."""
    return json.loads(script.split(f"var {name} = ")[1].split(";\n")[0])


def script_kind(script: str) -> str:
    """Classify a script the way the page would: by what it is asking for."""
    if "var WANTED" not in script:
        return "in-call"
    return "mute" if "unmute" in embedded(script, "AVOID") else "click"


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeCDP:
    """Stands in for Chromium: answers the scripts the join loop evaluates.

    ``gate`` reproduces the thing that made two loops possible in the field — a
    DevTools call that takes longer than the three seconds the old code waited
    for a cancelled thread.
    """

    def __init__(self, *, clicks=None, in_call_after=999, gate=None, block_pass=1):
        self.port = 9222
        self.kinds: list[str] = []
        self.click_scripts: list[str] = []
        self.callers: list[str] = []
        self.entered = threading.Event()
        self._lock = threading.Lock()
        self._clicks = list(clicks or [])
        self._in_call_after = in_call_after
        self._probes = 0
        self._gate = gate
        self._block_pass = block_pass

    def evaluate(self, script, *, timeout=10.0):
        kind = script_kind(script)
        with self._lock:
            self.kinds.append(kind)
            if kind == "in-call":
                self._probes += 1
                return "true" if self._probes > self._in_call_after else "false"
            if kind == "mute":
                return json.dumps({"clicked": "turn off microphone", "url": PAGE})
            index = len(self.click_scripts)
            self.click_scripts.append(script)
            self.callers.append(threading.current_thread().name)

        if self._gate is not None and index + 1 == self._block_pass:
            self.entered.set()
            self._gate.wait(timeout=10)

        payload = {
            "clicked": None,
            "filled_name": False,
            "waiting": "",
            "candidates": 4,
            "url": PAGE,
        }
        if index < len(self._clicks):
            payload.update(self._clicks[index])
        return json.dumps(payload)

    # The loop touches nothing else, but the service does on other paths.
    def is_alive(self):
        return True

    def close(self):
        return None


@pytest.fixture()
def joining(mock_config, monkeypatch):
    """A browser service whose join loop runs at test speed rather than room speed."""
    from app import browser_service

    for name in (
        "PASS_INTERVAL_SECONDS",
        "CLICK_INTERVAL_SECONDS",
        "LOBBY_POLL_SECONDS",
        "MAX_INTERVAL_SECONDS",
        "MAX_ERROR_INTERVAL_SECONDS",
    ):
        monkeypatch.setattr(browser_service, name, 0.02)
    _, errors = mock_config.update(
        {"JOIN_SETTLE_SECONDS": 0.02, "AUTO_JOIN_TIMEOUT_SECONDS": 10}
    )
    assert not errors, errors
    service = BrowserService(mock_config, SystemService(mock_config))
    yield service
    service._stop_join_automation()


def run_join(service, cdp, meeting=None, *, timeout=10.0):
    """Run one whole attempt and hand back its record."""
    meeting = meeting or a_meeting()
    service._cdp = cdp
    with service._lock:
        service._meeting_id = meeting.uid
    service._start_join_automation(meeting)
    thread = service._join_thread
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "the join loop never finished"
    return service.last_attempt


class TestJoinLoop:
    def test_a_name_fill_is_recorded_and_the_click_comes_next(self, joining):
        """One pass fills the name, the next presses Join. Never both at once."""
        cdp = FakeCDP(
            clicks=[{"filled_name": True}, {"clicked": "Join now"}], in_call_after=2
        )
        attempt = run_join(joining, cdp)
        assert attempt.filled_name is True
        assert attempt.clicks == ["Join now"]
        assert attempt.in_call is True and attempt.gave_up is False

    def test_only_one_loop_is_ever_pressing_at_the_page(self, joining):
        """The "it joins several times" bug, reproduced.

        A second JOIN arrives while the first attempt is stuck inside a
        DevTools call that outlasts the three-second wait for it to stop. The
        old code cleared the shared stop flag here, and the stale thread woke
        up and carried on clicking beside the new one.
        """
        gate = threading.Event()
        cdp = FakeCDP(gate=gate)
        meeting = a_meeting()
        joining._cdp = cdp
        with joining._lock:
            joining._meeting_id = meeting.uid

        joining._start_join_automation(meeting)
        stale = joining._join_thread
        assert cdp.entered.wait(5), "the first attempt never reached the page"

        generation = joining.join_generation
        starter = threading.Thread(target=joining._start_join_automation, args=(meeting,))
        starter.start()
        assert wait_until(lambda: joining.join_generation > generation)
        gate.set()  # the stalled call returns, exactly as it does in a room
        starter.join(timeout=10)

        assert stale is not joining._join_thread
        assert wait_until(lambda: not stale.is_alive()), "the stale loop never stopped"
        assert cdp.callers.count(stale.name) == 1, "a cancelled attempt pressed again"

    def test_a_cancelled_attempt_is_never_revived(self, joining):
        """Starting a new attempt must not un-cancel the old one."""
        meeting = a_meeting()
        joining._cdp = FakeCDP(in_call_after=0)
        joining._start_join_automation(meeting)
        first_stop = joining._join_stop

        joining._stop_join_automation()
        assert first_stop.is_set()

        joining._start_join_automation(meeting)
        assert first_stop.is_set(), "the old attempt's stop flag was cleared"
        assert joining._join_stop is not first_stop

    def test_a_button_is_not_pressed_again_on_the_same_page(self, joining):
        """The repeat guard: pressed once, then left alone while the page holds."""
        cdp = FakeCDP(clicks=[{"clicked": "Join now"}], in_call_after=2)
        run_join(joining, cdp)

        guards = [embedded(script, "GUARDED") for script in cdp.click_scripts]
        assert guards[0] == [], "nothing has been pressed yet"
        assert guards[1] == [{"text": "join now", "url": PAGE}]
        # The URL is what releases it: tests/js/test_clicker.js checks that the
        # same button is pressed again once the page has moved on.

    def test_the_repeat_guard_can_be_switched_off(self, joining):
        joining.config.update({"JOIN_REPEAT_GUARD_SECONDS": 0})
        cdp = FakeCDP(clicks=[{"clicked": "Join now"}], in_call_after=2)
        run_join(joining, cdp)
        assert all(embedded(s, "GUARDED") == [] for s in cdp.click_scripts)

    def test_the_repeat_guard_lets_go_after_its_window(self, joining):
        joining.config.update({"JOIN_REPEAT_GUARD_SECONDS": 0.1})
        recent = {"join now": (PAGE, time.monotonic())}
        assert joining._repeat_guard(recent) == [("join now", PAGE)]
        time.sleep(0.15)
        assert joining._repeat_guard(recent) == []
        assert recent == {}, "expired entries are dropped, not kept for ever"

    def test_the_lobby_stops_the_clicking(self, joining):
        """Waiting to be admitted is success in progress, not a failure."""
        cdp = FakeCDP(
            clicks=[{"waiting": "asking to be let in"}] * 3, in_call_after=3
        )
        attempt = run_join(joining, cdp)
        assert attempt.waiting is True
        assert attempt.clicks == [], "nothing may be pressed while in the lobby"
        assert attempt.gave_up is False, "a lobby wait is not a failed join"
        assert attempt.in_call is True, "the room joins when the host admits it"

    def test_the_room_can_join_muted(self, joining):
        joining.config.update({"JOIN_MUTE_ON_ENTRY": True})
        cdp = FakeCDP(in_call_after=0)
        attempt = run_join(joining, cdp)
        assert attempt.in_call is True
        assert cdp.kinds == ["in-call", "mute"], "mute once, after the call starts"

    def test_join_muted_is_off_by_default(self, joining):
        cdp = FakeCDP(in_call_after=0)
        run_join(joining, cdp)
        assert "mute" not in cdp.kinds

    def test_a_page_with_nothing_to_press_keeps_being_watched(self, joining):
        """No match is not a failure yet: keep looking until the deadline."""
        cdp = FakeCDP()
        meeting = a_meeting()
        joining._cdp = cdp
        with joining._lock:
            joining._meeting_id = meeting.uid
        joining._start_join_automation(meeting)
        assert wait_until(lambda: len(cdp.click_scripts) >= 3, timeout=5)
        assert joining.last_attempt.clicks == []
        assert joining.last_attempt.gave_up is False, "the deadline has not passed yet"

    def test_the_attempt_gives_up_at_its_deadline(self, joining, monkeypatch):
        """And when it does, the room is left where a person can finish the job."""
        real_int = joining.config.int_
        monkeypatch.setattr(
            joining.config,
            "int_",
            lambda key: 0 if key == "AUTO_JOIN_TIMEOUT_SECONDS" else real_int(key),
        )
        attempt = run_join(joining, FakeCDP())
        assert attempt.gave_up is True
        assert attempt.in_call is False and attempt.clicks == []

    def test_retrying_does_not_navigate(self, joining):
        """Someone pressing "try again" wants another pass, not a page reload."""
        cdp = FakeCDP(in_call_after=0)
        run_join(joining, cdp)
        before = joining.join_generation
        assert joining.retry_join() is True
        assert joining.join_generation > before
        assert joining._join_thread is not None
