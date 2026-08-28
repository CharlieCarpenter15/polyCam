"""Reaching a frame the page will not let JavaScript reach by itself.

There is no Chromium here, so DevTools is a stand-in: a fake websocket that
records every command sent to it and answers from a small table. That is enough
to test the things that actually matter about ``app/cdp.py``'s frame support,
all of which are about *what goes on the wire* — which command, how often, and
with which parameters.

The most important test in this file is the dullest one:
``evaluate`` without a ``context_id`` must send no ``contextId`` key at all.
Every existing caller — the join clicker, the mute pass, the roster reader —
goes through that method, and a ``contextId`` appearing where there was none
would quietly move all of them into a different frame.
"""

from __future__ import annotations

import json
import sys

import pytest

from app.cdp import CDPError, ChromeDevTools, PageFrame, looks_useful

WS_URL = "ws://127.0.0.1:9222/devtools/page/kiosk"
MEETING_URL = "https://teams.microsoft.com/v2/"
STAGE_URL = "https://teams.cloud.microsoft/stage"


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


class Refusal(Exception):
    """What Chromium says when it will not do something: an error reply.

    A command it has never heard of, a frame that has gone, an execution
    context that died with the document — they all come back this way, and
    ``send()`` turns every one of them into a :class:`CDPError`.
    """


class FakeSocket:
    """One websocket, answering whatever the fake browser decides."""

    def __init__(self, chrome: "FakeChromium") -> None:
        self.chrome = chrome
        self.connected = True
        self._outbox: list[str] = []

    def settimeout(self, timeout: float) -> None:
        self.chrome.timeouts.append(timeout)

    def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.chrome.calls.append((message["method"], message.get("params") or {}))
        if self.chrome.chatter:
            # A protocol event arriving ahead of the reply. ``send()`` throws
            # it away, which is exactly why the frame cache watches the page
            # URL instead of subscribing to Page.frameNavigated.
            self._outbox.append(
                json.dumps({"method": "Page.frameNavigated", "params": {}})
            )
        self._outbox.append(json.dumps(self.chrome.reply_to(message)))

    def recv(self) -> str:
        if not self._outbox:
            raise OSError("the socket said nothing")
        return self._outbox.pop(0)

    def close(self) -> None:
        self.connected = False


class FakeChromium:
    """A DevTools endpoint with a scripted answer for every command.

    ``answers`` maps an execution context id to what ``Runtime.evaluate``
    returns there; ``None`` is the page's own top frame, which is where an
    evaluate with no ``contextId`` lands. An answer that is a :class:`Refusal`
    is raised instead, which is what a dead context does.
    """

    def __init__(
        self,
        *,
        url: str = MEETING_URL,
        children: tuple[str, ...] = ("stage", "chat"),
        answers: dict | None = None,
    ) -> None:
        self.url = url
        self.children = children
        self.answers: dict = answers or {}
        self.calls: list[tuple[str, dict]] = []
        self.timeouts: list[float] = []
        self.sockets: list[FakeSocket] = []
        self.chatter = False
        #: Frames whose isolated world Chromium will not make.
        self.world_refused: set[str] = set()
        #: Execution context ids handed out, per frame.
        self.worlds: dict[str, int] = {}
        self._next_world = 100

    # -- what the client sees --------------------------------------------
    def page_target(self) -> dict:
        return {"type": "page", "url": self.url, "webSocketDebuggerUrl": WS_URL}

    def module(self) -> object:
        """Stands in for the ``websocket`` package ``_connect`` imports."""
        chrome = self

        class Module:
            @staticmethod
            def create_connection(url, timeout=None, max_size=None):
                socket = FakeSocket(chrome)
                chrome.sockets.append(socket)
                return socket

        return Module()

    def navigate(self, url: str) -> None:
        """The page moves on without the tab, or the socket, changing."""
        self.url = url

    # -- the command table -----------------------------------------------
    def reply_to(self, message: dict) -> dict:
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        handler = getattr(self, "on_" + method.replace(".", "_"), None)
        if handler is None:
            return {
                "id": message["id"],
                "error": {"message": f"'{method}' wasn't found"},
            }
        try:
            result = handler(params)
        except Refusal as exc:
            return {"id": message["id"], "error": {"message": str(exc)}}
        return {"id": message["id"], "result": result}

    def on_Page_enable(self, params: dict) -> dict:  # noqa: N802 - a CDP name
        return {}

    def on_Page_getFrameTree(self, params: dict) -> dict:  # noqa: N802
        return {
            "frameTree": {
                "frame": {"id": "top", "url": self.url},
                "childFrames": [
                    # No parentId on purpose: the flattening has to work out
                    # the parent from the nesting, which is all some builds give.
                    {"frame": {"id": name, "url": f"{STAGE_URL}/{name}"}}
                    for name in self.children
                ],
            }
        }

    def on_Page_createIsolatedWorld(self, params: dict) -> dict:  # noqa: N802
        frame_id = str(params.get("frameId") or "")
        if frame_id in self.world_refused:
            raise Refusal("No frame for given id found")
        self._next_world += 1
        self.worlds[frame_id] = self._next_world
        return {"executionContextId": self._next_world}

    def on_Runtime_evaluate(self, params: dict) -> dict:
        answer = self.answers.get(params.get("contextId"))
        if isinstance(answer, Refusal):
            raise answer
        return {"result": {"value": answer}}

    # -- what the tests ask ----------------------------------------------
    def sent(self, method: str) -> list[dict]:
        return [params for name, params in self.calls if name == method]

    def contexts_evaluated(self) -> list:
        return [params.get("contextId") for params in self.sent("Runtime.evaluate")]

    def world_for(self, frame_id: str) -> int:
        return self.worlds[frame_id]


@pytest.fixture()
def chrome(monkeypatch):
    """A fake browser with a two-frame meeting page, and the client for it."""
    fake = FakeChromium()
    monkeypatch.setitem(sys.modules, "websocket", fake.module())
    return fake


@pytest.fixture()
def devtools(chrome, monkeypatch):
    client = ChromeDevTools(port=9222)
    monkeypatch.setattr(client, "page_target", chrome.page_target)
    return client


def answers(**by_frame):
    """Map ``top=…, stage=…`` onto the context ids the fake hands out.

    Written as a callable so a test can say what each frame replies without
    knowing which execution context id it will be given.
    """

    def resolve(chrome: FakeChromium) -> None:
        chrome.answers = {None: by_frame.get("top")}
        for name in chrome.children:
            if name in by_frame:
                chrome.answers[chrome.world_for(name)] = by_frame[name]

    return resolve


# ---------------------------------------------------------------------------
# The regression guard for every existing caller
# ---------------------------------------------------------------------------


class TestEvaluateIsUnchanged:
    def test_no_context_id_sends_no_context_id_key(self, devtools, chrome):
        """Not ``None``, not ``0`` — absent. The top frame is the default."""
        chrome.answers = {None: "true"}
        assert devtools.evaluate("1 + 1") == "true"
        (params,) = chrome.sent("Runtime.evaluate")
        assert "contextId" not in params

    def test_the_rest_of_the_parameters_are_exactly_as_they_were(
        self, devtools, chrome
    ):
        devtools.evaluate("probe()", timeout=6.0, user_gesture=False)
        (params,) = chrome.sent("Runtime.evaluate")
        assert params == {
            "expression": "probe()",
            "returnByValue": True,
            "awaitPromise": True,
            "userGesture": False,
            "timeout": 6000,
        }

    def test_a_user_gesture_is_still_claimed_by_default(self, devtools, chrome):
        devtools.evaluate("press()")
        assert chrome.sent("Runtime.evaluate")[0]["userGesture"] is True

    def test_a_context_id_is_passed_through_when_one_is_given(
        self, devtools, chrome
    ):
        devtools.evaluate("probe()", context_id=101)
        assert chrome.sent("Runtime.evaluate")[0]["contextId"] == 101

    def test_a_protocol_error_is_still_a_cdp_error(self, devtools, chrome):
        chrome.answers = {None: Refusal("Cannot find context with specified id")}
        with pytest.raises(CDPError):
            devtools.evaluate("probe()")

    def test_events_arriving_first_are_still_discarded(self, devtools, chrome):
        """The reason the frame cache cannot be driven by events."""
        chrome.chatter = True
        chrome.answers = {None: "true"}
        assert devtools.evaluate("probe()") == "true"


# ---------------------------------------------------------------------------
# Enumerating the frames
# ---------------------------------------------------------------------------


class TestTheFrameTree:
    def test_the_top_frame_comes_first_and_knows_it_is_the_top(self, devtools):
        found = devtools.frames()
        assert [f.frame_id for f in found] == ["top", "stage", "chat"]
        assert found[0].is_top
        assert not any(f.is_top for f in found[1:])

    def test_a_child_frame_learns_its_parent_from_the_nesting(self, devtools):
        stage = devtools.frames()[1]
        assert stage.parent_id == "top"
        assert stage.url.startswith(STAGE_URL)

    def test_the_tree_is_read_once_and_then_remembered(self, devtools, chrome):
        for _ in range(4):
            devtools.frames()
        assert len(chrome.sent("Page.getFrameTree")) == 1

    def test_a_browser_that_refuses_gives_an_empty_list_rather_than_an_error(
        self, devtools, chrome, monkeypatch
    ):
        monkeypatch.delattr(FakeChromium, "on_Page_getFrameTree")
        assert devtools.frames() == []

    def test_a_refusal_is_remembered_too(self, devtools, chrome, monkeypatch):
        """A Raspberry Pi must not ask a question it has already been refused."""
        monkeypatch.delattr(FakeChromium, "on_Page_getFrameTree")
        for _ in range(5):
            devtools.frames()
        assert len(chrome.sent("Page.getFrameTree")) == 1

    def test_the_walk_is_bounded(self, devtools, chrome):
        chrome.children = tuple(f"frame{n}" for n in range(40))
        from app.cdp import MAX_FRAMES

        assert len(devtools.frames()) == MAX_FRAMES


class TestTheCacheIsDroppedWhenThePageMovesOn:
    def test_a_navigation_means_a_fresh_tree(self, devtools, chrome):
        devtools.frames()
        chrome.navigate("https://teams.cloud.microsoft/v2/")
        devtools.frames()
        assert len(chrome.sent("Page.getFrameTree")) == 2

    def test_the_isolated_worlds_go_with_it(self, devtools, chrome):
        first = devtools.isolated_world("stage")
        chrome.navigate("https://teams.cloud.microsoft/v2/")
        devtools.frames()
        second = devtools.isolated_world("stage")
        assert first != second
        assert len(chrome.sent("Page.createIsolatedWorld")) == 2

    def test_staying_on_the_same_page_keeps_the_tree(self, devtools, chrome):
        devtools.frames()
        devtools.evaluate("probe()")
        devtools.frames()
        assert len(chrome.sent("Page.getFrameTree")) == 1

    def test_forgetting_by_hand_works_too(self, devtools, chrome):
        devtools.frames()
        devtools.forget_frames()
        devtools.frames()
        assert len(chrome.sent("Page.getFrameTree")) == 2

    def test_a_closed_socket_takes_the_cache_with_it(self, devtools, chrome):
        devtools.frames()
        devtools.close()
        devtools.frames()
        assert len(chrome.sent("Page.getFrameTree")) == 2


# ---------------------------------------------------------------------------
# Isolated worlds
# ---------------------------------------------------------------------------


class TestIsolatedWorlds:
    def test_a_world_is_made_once_per_frame(self, devtools, chrome):
        first = devtools.isolated_world("stage")
        for _ in range(4):
            assert devtools.isolated_world("stage") == first
        assert len(chrome.sent("Page.createIsolatedWorld")) == 1

    def test_the_world_is_named_after_the_appliance(self, devtools, chrome):
        devtools.isolated_world("stage")
        (params,) = chrome.sent("Page.createIsolatedWorld")
        assert params == {"frameId": "stage", "worldName": "room-appliance"}

    def test_a_frame_that_has_gone_answers_none_rather_than_raising(self, devtools):
        devtools_world = devtools.isolated_world
        assert devtools_world("") is None

    def test_a_refusal_is_remembered_so_it_is_not_asked_again(
        self, devtools, chrome
    ):
        chrome.world_refused = {"stage"}
        for _ in range(5):
            assert devtools.isolated_world("stage") is None
        assert len(chrome.sent("Page.createIsolatedWorld")) == 1


# ---------------------------------------------------------------------------
# Trying each frame in turn
# ---------------------------------------------------------------------------


def has_names(reply) -> bool:
    """A caller's predicate: a reply that names somebody is worth having."""
    return bool(isinstance(reply, dict) and reply.get("participants"))


class TestEvaluateInFrames:
    def test_a_useful_top_frame_answer_costs_nothing_extra(self, devtools, chrome):
        """The ordinary case must not pay for a frame walk it does not need."""
        chrome.answers = {None: {"participants": ["Priya Nair"]}}
        reply = devtools.evaluate_in_frames("probe()", useful=has_names)
        assert reply == {"participants": ["Priya Nair"]}
        assert chrome.sent("Page.getFrameTree") == []
        assert chrome.contexts_evaluated() == [None]

    def test_an_empty_top_frame_answer_sends_it_into_the_children(
        self, devtools, chrome
    ):
        devtools.frames()
        answers(top={"participants": []}, stage={"participants": ["Sam Okafor"]})(
            _worlds(devtools, chrome)
        )
        reply = devtools.evaluate_in_frames("probe()", useful=has_names)
        assert reply == {"participants": ["Sam Okafor"]}

    def test_the_first_useful_answer_wins_and_the_walk_stops(
        self, devtools, chrome
    ):
        devtools.frames()
        answers(
            top={"participants": []},
            stage={"participants": ["Sam Okafor"]},
            chat={"participants": ["Nobody At All"]},
        )(_worlds(devtools, chrome))
        reply = devtools.evaluate_in_frames("probe()", useful=has_names)
        assert reply == {"participants": ["Sam Okafor"]}
        assert chrome.world_for("chat") not in chrome.contexts_evaluated()

    def test_the_top_frame_is_never_asked_twice(self, devtools, chrome):
        devtools.frames()
        answers(top={"participants": []})(_worlds(devtools, chrome))
        devtools.evaluate_in_frames("probe()", useful=has_names)
        assert chrome.contexts_evaluated().count(None) == 1

    def test_nothing_useful_anywhere_returns_the_top_frame_answer(
        self, devtools, chrome
    ):
        devtools.frames()
        answers(top={"participants": [], "reason": "no-provider-surface"})(
            _worlds(devtools, chrome)
        )
        reply = devtools.evaluate_in_frames("probe()", useful=has_names)
        assert reply == {"participants": [], "reason": "no-provider-surface"}

    def test_one_frame_refusing_a_world_does_not_stop_the_others(
        self, devtools, chrome
    ):
        chrome.world_refused = {"stage"}
        devtools.frames()
        answers(top={"participants": []}, chat={"participants": ["Sam Okafor"]})(
            _worlds(devtools, chrome)
        )
        reply = devtools.evaluate_in_frames("probe()", useful=has_names)
        assert reply == {"participants": ["Sam Okafor"]}

    def test_a_frame_that_dies_mid_walk_is_stepped_over(self, devtools, chrome):
        devtools.frames()
        answers(
            top={"participants": []},
            stage=Refusal("Cannot find context with specified id"),
            chat={"participants": ["Sam Okafor"]},
        )(_worlds(devtools, chrome))
        reply = devtools.evaluate_in_frames("probe()", useful=has_names)
        assert reply == {"participants": ["Sam Okafor"]}

    def test_a_dead_frames_world_is_made_again_on_the_next_pass(
        self, devtools, chrome
    ):
        """Its context died; the frame may not have. Ask once more, not for ever."""
        devtools.frames()
        answers(top={"participants": []}, stage=Refusal("no such context"))(
            _worlds(devtools, chrome)
        )
        devtools.evaluate_in_frames("probe()", useful=has_names)
        devtools.evaluate_in_frames("probe()", useful=has_names)
        worlds = [
            params["frameId"] for params in chrome.sent("Page.createIsolatedWorld")
        ]
        assert worlds.count("stage") == 2

    def test_a_browser_with_no_frame_support_degrades_to_the_top_frame(
        self, devtools, chrome, monkeypatch
    ):
        monkeypatch.delattr(FakeChromium, "on_Page_getFrameTree")
        chrome.answers = {None: {"participants": []}}
        reply = devtools.evaluate_in_frames("probe()", useful=has_names)
        assert reply == {"participants": []}
        assert chrome.sent("Page.createIsolatedWorld") == []

    def test_with_no_frames_it_is_evaluate_exception_and_all(
        self, devtools, chrome, monkeypatch
    ):
        """A caller must not be able to tell the two apart."""
        monkeypatch.delattr(FakeChromium, "on_Page_getFrameTree")
        chrome.answers = {None: Refusal("Cannot access the page")}
        with pytest.raises(CDPError):
            devtools.evaluate_in_frames("probe()", useful=has_names)

    def test_a_frame_that_answers_rescues_a_top_frame_that_threw(
        self, devtools, chrome
    ):
        devtools.frames()
        answers(top=Refusal("Cannot access the page"), stage={"participants": ["S"]})(
            _worlds(devtools, chrome)
        )
        assert devtools.evaluate_in_frames("probe()", useful=has_names) == {
            "participants": ["S"]
        }

    def test_the_gesture_and_the_expression_travel_into_the_frame(
        self, devtools, chrome
    ):
        devtools.frames()
        answers(top={"participants": []})(_worlds(devtools, chrome))
        devtools.evaluate_in_frames("probe()", useful=has_names, user_gesture=False)
        for params in chrome.sent("Runtime.evaluate"):
            assert params["expression"] == "probe()"
            assert params["userGesture"] is False

    def test_a_predicate_that_throws_is_a_no_rather_than_a_crash(
        self, devtools, chrome
    ):
        """It is the caller's predicate, and this runs during a meeting."""

        def hostile(reply):
            raise RuntimeError("this predicate is broken")

        chrome.answers = {None: {"participants": ["Priya Nair"]}}
        assert devtools.evaluate_in_frames("probe()", useful=hostile) == {
            "participants": ["Priya Nair"]
        }

    def test_without_a_predicate_an_empty_answer_still_sends_it_looking(
        self, devtools, chrome
    ):
        devtools.frames()
        answers(top="", stage="found something")(_worlds(devtools, chrome))
        assert devtools.evaluate_in_frames("probe()") == "found something"


def _worlds(devtools: ChromeDevTools, chrome: FakeChromium) -> FakeChromium:
    """Make every frame's isolated world up front, so a test can script them.

    The client caches what comes back, so this changes nothing about what the
    walk under test then does — it only means the test knows the context ids.
    """
    for frame in devtools.frames():
        if not frame.is_top:
            devtools.isolated_world(frame.frame_id)
    return chrome


# ---------------------------------------------------------------------------
# The default "did that answer anything?" test
# ---------------------------------------------------------------------------


class TestLooksUseful:
    @pytest.mark.parametrize(
        "value", [None, False, "", "   ", "false", "null", [], {}]
    )
    def test_nothing_is_nothing(self, value):
        assert looks_useful(value) is False

    @pytest.mark.parametrize(
        "value", ["true", "a name", ["Priya Nair"], {"ok": True}, 0, 1]
    )
    def test_an_answer_is_an_answer(self, value):
        assert looks_useful(value) is True

    def test_the_in_call_probe_reads_the_way_it_is_written(self):
        """It answers in strings, and "false" is an answer meaning "no"."""
        assert looks_useful("false") is False
        assert looks_useful("true") is True


class TestPageFrame:
    def test_a_frame_with_no_parent_is_the_top_one(self):
        assert PageFrame("top").is_top
        assert not PageFrame("stage", parent_id="top").is_top


# ---------------------------------------------------------------------------
# The door the appliance opens onto a meeting
# ---------------------------------------------------------------------------


@pytest.fixture()
def meeting_browser(mock_config, devtools):
    """A browser service with a meeting on screen and the fake DevTools in it."""
    from app.browser_service import TARGET_MEETING, BrowserService
    from app.system_service import SystemService

    service = BrowserService(mock_config, SystemService(mock_config))
    service._cdp = devtools
    service._target = TARGET_MEETING
    return service


class TestReadMeetingFrames:
    def test_a_reply_from_a_child_frame_comes_back_decoded(
        self, meeting_browser, chrome, devtools
    ):
        """Teams' stage on its own origin: the top frame knows nothing."""
        _worlds(devtools, chrome)
        chrome.answers = {
            None: json.dumps({"participants": []}),
            chrome.world_for("stage"): json.dumps({"participants": ["Sam Okafor"]}),
        }
        reply = meeting_browser.read_meeting_frames("probe()", useful=has_names)
        assert reply == {"participants": ["Sam Okafor"]}

    def test_the_predicate_is_asked_about_the_decoded_reply(
        self, meeting_browser, chrome, devtools
    ):
        """A caller writes "does this name anybody?", not JSON archaeology."""
        seen = []
        _worlds(devtools, chrome)
        chrome.answers = {
            None: json.dumps({"participants": []}),
            chrome.world_for("stage"): json.dumps({"participants": ["Sam Okafor"]}),
        }

        def watching(reply):
            seen.append(reply)
            return has_names(reply)

        meeting_browser.read_meeting_frames("probe()", useful=watching)
        assert all(isinstance(reply, dict) for reply in seen), seen

    def test_no_meeting_on_screen_means_no_answer_and_no_commands(
        self, meeting_browser, chrome
    ):
        from app.browser_service import TARGET_DASHBOARD

        meeting_browser._target = TARGET_DASHBOARD
        assert meeting_browser.read_meeting_frames("probe()") is None
        assert chrome.calls == []

    def test_a_failure_is_no_answer_rather_than_an_exception(
        self, meeting_browser, chrome, monkeypatch
    ):
        monkeypatch.delattr(FakeChromium, "on_Page_getFrameTree")
        chrome.answers = {None: Refusal("Cannot access the page")}
        assert meeting_browser.read_meeting_frames("probe()") is None

    def test_it_reads_without_claiming_a_user_gesture(
        self, meeting_browser, chrome
    ):
        """Same rule as the door beside it: reading is not interacting."""
        chrome.answers = {None: json.dumps({"participants": ["Priya Nair"]})}
        meeting_browser.read_meeting_frames("probe()")
        assert chrome.sent("Runtime.evaluate")[0]["userGesture"] is False
