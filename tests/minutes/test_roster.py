"""Reading the meeting window: the sampler, the files it writes, and the seam.

There is no Chromium here and there never will be on a test machine, so the
browser is a stand-in that answers whatever script it is handed. That is enough
to test everything on the Python side of the glass — the unit conversion, the
back-off, the streaming writes, the caption merge — and the DOM side is covered
separately by ``tests/js/test_roster.js`` under Node and jsdom.

The single most important test in this file is the one about seconds. The page
stamps its samples with ``Date.now()``, in milliseconds since 1970. Everything
downstream counts seconds from the start of the recording. Getting that wrong
produces speaking spans fifty thousand years long which silently overlap every
segment in the meeting, and nothing anywhere would raise.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading

import pytest

from app.minutes import roster
from app.minutes.roster import (
    CaptionLine,
    RosterSample,
    RosterSampler,
    build_captions_script,
    build_drain_script,
    build_install_script,
    build_probe_script,
    build_roster_panel_script,
)
from app.minutes.transcript import SOURCE_ROSTER, TRACK_FAR_END

#: A plausible ``Date.now()``: milliseconds since 1970, some time in 2026.
PAGE_NOW = 1787936454000


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


def script_kind(script: str) -> str:
    """Classify a script the way the page would — by what it asks for.

    The sampler never labels its scripts out of band, so neither does this.
    """
    if "window.__pcRoster = state" in script:
        return "install"
    if "var FLUSH =" in script:
        return "drain"
    if "var PANEL_SELECTORS = " in script:
        return "panel"
    return "click"


class FakeBrowser:
    """Answers ``read_meeting_page`` from a canned list of drain payloads.

    An entry may be a payload, ``None`` (the meeting is not on screen), or a
    callable, which is how a test asserts on what is already on disk *while*
    the sampler is still running.
    """

    def __init__(
        self,
        drains=(),
        *,
        install=True,
        clicked="Turn on live captions",
        panel=None,
        meeting_in_top=True,
        surface=True,
    ):
        self.drains = list(drains)
        self.install = install
        #: False for a page whose meeting stage is a frame of its own, on its
        #: own origin: the top frame can see none of it, and only the walk
        #: through the frames finds anything at all.
        self.meeting_in_top = meeting_in_top
        #: False for a page with no meeting on it anywhere yet — a pre-join
        #: screen. No frame will take an observer that insists on a surface.
        self.surface = surface
        #: Which scripts went through the frame-walking door.
        self.walked: list[str] = []
        #: One entry per frame asked, True for the top frame. A press that
        #: reaches the second frame has pressed something twice.
        self.asked: list[bool] = []
        self.clicked = clicked
        #: What the participant-panel pass finds. The default is a meeting with
        #: the panel shut and a control to open it.
        self.panel = (
            {"open": False, "clicked": "people", "source": "", "candidates": 9}
            if panel is None
            else panel
        )
        self.scripts: list[str] = []
        #: Whether each call claimed a user gesture. Only the two controls the
        #: feature may press should: the page gates them behind a real
        #: interaction, and a reader that claimed one every couple of seconds
        #: would keep the page permanently convinced somebody was using it.
        self.gestures: list[bool] = []
        self.kinds: list[str] = []

    def read_meeting_page(self, script, *, timeout=6.0, user_gesture=False):
        self._record(script, user_gesture)
        return self._answer(script, top_frame=True)

    def read_meeting_frames(
        self, script, *, useful=None, timeout=6.0, user_gesture=False
    ):
        """The door that looks inside the page's frames.

        One pass, whichever frame ends up answering it — so everything the
        tests count stays counted once. The top frame is asked first and its
        answer stands unless ``useful`` rejects it, which is the contract
        ``cdp.evaluate_in_frames`` implements for real.
        """
        self._record(script, user_gesture)
        self.walked.append(script_kind(script))
        top = self._answer(script, top_frame=True)
        # ``None`` is not one frame's answer but the page's: the real door
        # returns it when the evaluate itself failed, and the walk is never
        # reached. A meeting that has left the screen must stay left.
        if top is None:
            return None
        if useful is None or useful(top):
            return top
        other = self._answer(script, top_frame=False)
        if other is None:
            return None
        # Nothing useful anywhere means the top frame's answer stands, which is
        # what keeps "the observer is gone" reaching Python as "the observer is
        # gone" rather than as silence.
        return other if useful(other) else top

    def _record(self, script, user_gesture) -> None:
        self.scripts.append(script)
        self.gestures.append(bool(user_gesture))
        self.kinds.append(script_kind(script))

    def _answer(self, script, *, top_frame: bool):
        """What one frame of the page says. The stage knows; the shell may not."""
        kind = script_kind(script)
        self.asked.append(bool(top_frame))
        # Exactly one frame has the meeting in it. The other one knows nothing,
        # whichever of the two that is.
        blind = top_frame != self.meeting_in_top
        if kind == "install":
            if not self.install:
                return None
            if (blind or not self.surface) and "var REQUIRE_SURFACE = true;" in script:
                # An observer offered to a frame with no meeting in it refuses,
                # leaving nothing behind, so the next frame can be tried.
                return {"ok": False, "state": "no-surface",
                        "reason": "no-provider-surface"}
            return {"ok": True, "state": "installed"}
        if kind == "click":
            if blind:
                return {"clicked": None, "filled_name": False, "waiting": ""}
            return {"clicked": self.clicked, "filled_name": False, "waiting": ""}
        if kind == "panel":
            if blind:
                return {"open": False, "clicked": None, "candidates": 3}
            return self.panel
        if blind:
            return {"ok": False, "installed": False, "reason": "not-installed"}
        if not self.drains:
            return None
        entry = self.drains.pop(0)
        return entry() if callable(entry) else entry

    def script_of(self, kind: str) -> str:
        """The first script of one kind, for asserting on what was sent."""
        for script, seen in zip(self.scripts, self.kinds):
            if seen == kind:
                return script
        return ""


#: The two presses this feature is allowed to make. Everything else only reads.
PRESSES = ("click", "panel")


def panel_fields(events) -> dict:
    """The structured fields of the one participant-panel event, or ``{}``."""
    for name, fields in events.events:
        if name == "minutes.roster_panel_requested":
            return fields
    return {}


class CountingEvent(threading.Event):
    """A stop event that records how long the sampler asked to sleep.

    Pacing is the thing under test and the real intervals run to ten seconds,
    so the wait is recorded and skipped rather than served. ``stop_after``
    ends the loop, because a sampler is meant to run until the meeting does.
    """

    def __init__(self, stop_after: int = 3) -> None:
        super().__init__()
        self.waits: list[float] = []
        self.stop_after = stop_after

    def wait(self, timeout=None):  # type: ignore[override]
        self.waits.append(timeout)
        if len(self.waits) >= self.stop_after:
            self.set()
        return super().wait(0)


class Captured(logging.Handler):
    """Collects structured events from one logger, whatever the root is doing."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.events: list[tuple[str, dict]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.events.append((record.getMessage(), dict(getattr(record, "fields", {}) or {})))

    def names(self) -> list[str]:
        return [event for event, _ in self.events]


@pytest.fixture()
def events():
    handler = Captured()
    roster.log.addHandler(handler)
    previous = roster.log.level
    roster.log.setLevel(logging.DEBUG)
    yield handler
    roster.log.removeHandler(handler)
    roster.log.setLevel(previous)


@pytest.fixture()
def reading(config):
    """A configuration with the meeting-window reader switched on."""
    changed, errors = config.update(
        {"MINUTES_IDENTIFY_REMOTE": True, "MINUTES_READ_CAPTIONS": True, "DEV_MODE": False}
    )
    assert not errors, errors
    return config


def drain_payload(*, samples=(), captions=(), ok=True, **extra):
    """One drain's worth of what the in-page observer hands over."""
    payload = {
        "ok": ok,
        "installed": True,
        "now": PAGE_NOW,
        "seq": 4,
        "participants": ["Priya Nair", "Sam Okafor"],
        "speaking": [],
        "source": 'tiles:[data-stream-type="Video"][data-tid]',
        "health": {"tiles": 2, "roster": 0, "captions": 0},
        "reason": "",
        "surface": True,
        "signal": False,
        "samples": list(samples),
        "captions": list(captions),
    }
    payload.update(extra)
    return payload


def run_sampler(config, browser, directory, *, stop_after=3, provider="teams"):
    """Start a sampler, let its thread run to completion, and stop it."""
    sampler = RosterSampler(config, browser)
    sampler._stop = CountingEvent(stop_after=stop_after)
    sampler.start(provider, directory)
    thread = sampler._thread
    if thread is not None:
        thread.join(timeout=10)
        assert not thread.is_alive(), "the sampler thread did not finish"
    return sampler


def write_lines(path, rows):
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Is it switched on at all
# ---------------------------------------------------------------------------


class TestAvailable:
    def test_switched_off_says_so_plainly(self, config):
        config.update({"MINUTES_IDENTIFY_REMOTE": False})
        ok, why = roster.available(config)
        assert ok is False
        assert "switched off" in why

    def test_development_mode_is_a_no_op_that_explains_itself(self, config):
        config.update({"MINUTES_IDENTIFY_REMOTE": True, "DEV_MODE": True})
        ok, why = roster.available(config)
        assert ok is False
        assert "Development mode" in why

    def test_switched_on_with_a_browser_is_available(self, reading):
        assert roster.available(reading) == (True, "")

    def test_a_broken_configuration_is_unavailable_rather_than_fatal(self):
        class Hostile:
            def bool_(self, key):
                raise RuntimeError("no configuration here")

        ok, why = roster.available(Hostile())
        assert ok is False and why


# ---------------------------------------------------------------------------
# The generated JavaScript
# ---------------------------------------------------------------------------


def embedded(script: str, name: str):
    """Read a ``var NAME = <json>;`` line back out of a rendered script.

    The same trick ``tests/test_join_flows.py`` uses: it asserts on what was
    injected without executing a line of JavaScript.
    """
    fragment = script.split(f"var {name} = ", 1)[1]
    value, _ = json.JSONDecoder().raw_decode(fragment)
    return value


class TestScripts:
    #: The scripts this module renders itself. ``build_captions_script`` is the
    #: join clicker, which has its own tests in ``tests/test_join_flows.py``.
    OURS = [
        build_probe_script("teams"),
        build_probe_script("meet"),
        build_probe_script("zoom"),
        build_probe_script("webex"),
        build_install_script("teams", "run-token", captions=True),
        build_install_script("", "run-token", captions=False),
        build_drain_script("run-token"),
        build_drain_script("run-token", flush=True),
    ]
    #: The one script this module renders that presses something. Held to
    #: every rule below except "never clicks".
    PANELS = [
        build_roster_panel_script("teams"),
        build_roster_panel_script("meet"),
        build_roster_panel_script("zoom"),
        build_roster_panel_script("webex"),
    ]
    #: ...and the join clicker, which is somebody else's prose as well as
    #: somebody else's code, so it is only asked to parse.
    ALL = OURS + PANELS + [build_captions_script()]
    WRITTEN_HERE = OURS + PANELS

    @pytest.mark.parametrize("script", ALL, ids=range(len(ALL)))
    def test_the_generated_script_is_valid_javascript(self, script, tmp_path):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            pytest.skip("Node is not installed (it is not needed on the appliance)")
        path = tmp_path / "probe.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        "script", WRITTEN_HERE, ids=range(len(WRITTEN_HERE))
    )
    def test_the_script_is_es5(self, script):
        """It runs in whatever Chromium the Pi happens to have."""
        assert "=>" not in script
        assert "`" not in script
        assert not any(f" {word} " in script for word in ("let", "const"))

    @pytest.mark.parametrize(
        "script", WRITTEN_HERE, ids=range(len(WRITTEN_HERE))
    )
    def test_no_stray_debugging_is_shipped_into_a_meeting(self, script):
        assert "console.log" not in script
        assert "debugger" not in script

    @pytest.mark.parametrize("script", OURS, ids=range(len(OURS)))
    def test_nothing_is_ever_clicked_by_a_reading_script(self, script):
        """Reading is safe; pressing things changes what the room sees."""
        assert ".click(" not in script
        assert "setAttribute" not in script, "the page is read, never written to"

    def test_a_hostile_run_token_stays_inside_a_string_literal(self, tmp_path):
        """The token is generated, not typed, but json.dumps is free insurance."""
        hostile = '"; window.evil = 1; //'
        script = build_install_script("teams", hostile)
        assert embedded(script, "RUN") == hostile
        assert embedded(build_drain_script(hostile), "RUN") == hostile
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            pytest.skip("Node is not installed (it is not needed on the appliance)")
        path = tmp_path / "install.js"
        path.write_text(script, encoding="utf-8")
        assert subprocess.run([node, "--check", str(path)], timeout=30).returncode == 0

    def test_an_unknown_provider_carries_all_three_families(self):
        script = build_probe_script("webex")
        for name in ("teamsProbe", "meetProbe", "zoomProbe"):
            assert name in script

    def test_a_known_provider_carries_only_its_own(self):
        script = build_probe_script("zoom")
        assert "zoomProbe" in script
        assert "teamsProbe" not in script

    def test_reading_captions_can_be_turned_off_in_the_page(self):
        assert "var WANT_CAPTIONS = false;" in build_install_script("teams", "t", captions=False)
        assert "var WANT_CAPTIONS = true;" in build_install_script("teams", "t", captions=True)

    def test_the_caption_pass_can_only_ever_switch_them_on(self):
        """"Captions" is a substring of "Turn off captions"."""
        avoid = embedded(build_captions_script(), "AVOID")
        assert "turn off" in avoid
        wanted = embedded(build_captions_script(), "WANTED")
        assert "Turn on live captions" in wanted

    @pytest.mark.parametrize(
        "provider,word",
        [("teams", "People"), ("meet", "Show everyone"), ("zoom", "Participants")],
    )
    def test_each_provider_hunts_its_own_control(self, provider, word):
        assert word in embedded(build_roster_panel_script(provider), "PANEL_WANTED")

    def test_an_unknown_provider_tries_all_three(self):
        wanted = embedded(build_roster_panel_script("webex"), "PANEL_WANTED")
        for word in ("People", "Show everyone", "Participants"):
            assert word in wanted
        selectors = embedded(build_roster_panel_script("webex"), "PANEL_SELECTORS")
        assert '[data-tid="roster"]' in selectors
        assert "#participants-ul" in selectors

    def test_the_panel_pass_can_only_ever_open_it(self):
        """A room that shut a panel somebody opened would be taking it away."""
        avoid = embedded(build_roster_panel_script("teams"), "PANEL_AVOID")
        for term in ("hide", "close", "collapse"):
            assert term in avoid

    def test_the_panel_pass_will_not_press_anything_that_leaves(self):
        avoid = embedded(build_roster_panel_script("teams"), "PANEL_AVOID")
        for term in ("leave", "end call", "hang up"):
            assert term in avoid

    def test_the_panel_selectors_cannot_match_the_button_itself(self):
        """Mistaking the control for the panel means never pressing it."""
        for provider in ("teams", "meet", "zoom", "webex"):
            for selector in embedded(
                build_roster_panel_script(provider), "PANEL_SELECTORS"
            ):
                assert selector != '[aria-label*="Participants"]'
                assert "button" not in selector


# ---------------------------------------------------------------------------
# Seconds, not milliseconds
# ---------------------------------------------------------------------------


class TestTheUnitSeam:
    """``sample.at`` is seconds since the recording began. Nothing else."""

    def test_a_page_millisecond_stamp_becomes_small_float_seconds(self):
        # The page took this sample two seconds before the drain, and the
        # recording has been running for ten.
        at = roster._relative_seconds(PAGE_NOW, PAGE_NOW - 2000, 10.0)
        assert at == pytest.approx(8.0)
        assert at < 1_000_000, "a raw Date.now() would be about 1.8e12"

    def test_a_sample_taken_at_the_drain_is_the_elapsed_time(self):
        assert roster._relative_seconds(PAGE_NOW, PAGE_NOW, 42.5) == pytest.approx(42.5)

    def test_time_never_runs_backwards(self):
        assert roster._relative_seconds(PAGE_NOW, PAGE_NOW - 90_000, 3.0) == 0.0

    def test_a_clock_correction_mid_meeting_is_anchored_to_the_drain(self):
        """An NTP step must not produce a span thousands of seconds long."""
        at = roster._relative_seconds(PAGE_NOW, PAGE_NOW - 3_600_000, 12.0)
        assert at == pytest.approx(12.0)

    def test_a_missing_or_unusable_stamp_falls_back_to_the_drain(self):
        assert roster._relative_seconds(None, None, 7.0) == pytest.approx(7.0)
        assert roster._relative_seconds("banana", PAGE_NOW, 7.0) == pytest.approx(7.0)

    def test_the_whole_way_through_the_sampler(self, reading, tmp_path, events):
        """A Date.now() from the page must never reach the file unconverted."""
        browser = FakeBrowser(
            drains=[
                drain_payload(
                    samples=[
                        {"at": PAGE_NOW - 4000, "speaking": [], "ok": True,
                         "participants": ["Priya Nair"]},
                        {"at": PAGE_NOW - 2000, "speaking": ["Priya Nair"], "ok": True},
                        {"at": PAGE_NOW, "speaking": [], "ok": True},
                    ],
                    captions=[{"at": PAGE_NOW - 1000, "speaker": "Priya Nair", "text": "hello"}],
                ),
                None,
            ]
        )
        run_sampler(reading, browser, tmp_path).stop()

        samples = roster.load_samples(tmp_path)
        assert len(samples) == 3
        for sample in samples:
            assert isinstance(sample.at, float)
            assert 0.0 <= sample.at < 60.0, f"{sample.at} looks like a raw Date.now()"
        assert samples == sorted(samples, key=lambda s: s.at)

        lines = roster.load_captions(tmp_path)
        assert len(lines) == 1 and 0.0 <= lines[0].at < 60.0


# ---------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------


class TestBackOff:
    def test_a_healthy_meeting_is_polled_every_two_seconds(self, reading, tmp_path):
        browser = FakeBrowser(drains=[drain_payload() for _ in range(6)])
        sampler = run_sampler(reading, browser, tmp_path, stop_after=4)
        assert sampler._stop.waits[:4] == [2.0, 2.0, 2.0, 2.0]

    def test_it_slows_to_five_seconds_after_three_failures_and_ten_after_ten(
        self, reading, tmp_path
    ):
        browser = FakeBrowser(drains=[drain_payload(ok=False) for _ in range(20)])
        sampler = run_sampler(reading, browser, tmp_path, stop_after=12)
        waits = sampler._stop.waits
        assert waits[:2] == [2.0, 2.0], waits
        assert waits[2:9] == [5.0] * 7, waits
        assert waits[9] == 10.0, waits

    def test_one_good_answer_clears_the_back_off(self, reading, tmp_path):
        browser = FakeBrowser(
            drains=[drain_payload(ok=False), drain_payload(ok=False),
                    drain_payload(ok=False), drain_payload(),
                    drain_payload(), drain_payload()]
        )
        sampler = run_sampler(reading, browser, tmp_path, stop_after=5)
        assert sampler._stop.waits[:5] == [2.0, 2.0, 5.0, 2.0, 2.0]

    def test_a_reloaded_page_is_reinstalled_rather_than_abandoned(self, reading, tmp_path):
        browser = FakeBrowser(
            drains=[
                drain_payload(),
                {"ok": False, "installed": False, "reason": "not-installed", "now": PAGE_NOW},
                drain_payload(),
                None,
            ]
        )
        run_sampler(reading, browser, tmp_path, stop_after=9).stop()
        assert browser.kinds.count("install") == 2, browser.kinds


class TestNoMeetingOnScreen:
    def test_it_stops_once_the_meeting_has_left_the_screen(self, reading, tmp_path, events):
        browser = FakeBrowser(drains=[drain_payload(), None, drain_payload()])
        sampler = run_sampler(reading, browser, tmp_path, stop_after=20)
        assert "minutes.roster_page_gone" in events.names()
        # The third drain was never asked for: the loop had already given up.
        assert browser.kinds.count("drain") == 2, browser.kinds
        assert sampler._stop.waits == [2.0], sampler._stop.waits

    def test_a_page_that_is_not_there_yet_is_given_a_moment(self, reading, tmp_path, events):
        """The room may still be working through a pre-join screen."""
        browser = FakeBrowser(install=False)
        sampler = run_sampler(reading, browser, tmp_path, stop_after=4)
        assert browser.kinds.count("install") >= 3, browser.kinds
        assert "minutes.roster_page_gone" not in events.names()
        assert sampler.stop() == []

    def test_but_not_for_ever(self, reading, tmp_path, events, monkeypatch):
        monkeypatch.setattr(roster, "INSTALL_GRACE_SECONDS", -1.0)
        browser = FakeBrowser(install=False)
        run_sampler(reading, browser, tmp_path, stop_after=20)
        assert browser.kinds.count("install") == 1, browser.kinds
        assert "minutes.roster_page_gone" in events.names()


# ---------------------------------------------------------------------------
# Writing as it goes
# ---------------------------------------------------------------------------


class TestStreamingPersistence:
    def test_both_files_are_written_before_the_meeting_ends(self, reading, tmp_path):
        """A power cut must keep whatever was captured up to that point."""
        seen: dict[str, bool] = {}

        def look():
            seen["roster"] = (tmp_path / roster.ROSTER_FILE).exists()
            seen["captions"] = (tmp_path / roster.CAPTIONS_FILE).exists()

        browser = FakeBrowser(
            drains=[
                drain_payload(
                    samples=[{"at": PAGE_NOW, "speaking": ["Priya Nair"], "ok": True}],
                    captions=[{"at": PAGE_NOW, "speaker": "Priya Nair", "text": "hello"}],
                ),
                look,
            ]
        )
        run_sampler(reading, browser, tmp_path, stop_after=20)
        assert seen == {"roster": True, "captions": True}

    def test_the_files_are_owner_readable_only(self, reading, tmp_path):
        browser = FakeBrowser(
            drains=[
                drain_payload(
                    samples=[{"at": PAGE_NOW, "speaking": [], "ok": True}],
                    captions=[{"at": PAGE_NOW, "speaker": "Priya Nair", "text": "hello"}],
                ),
                None,
            ]
        )
        run_sampler(reading, browser, tmp_path).stop()
        for name in (roster.ROSTER_FILE, roster.CAPTIONS_FILE):
            assert (tmp_path / name).stat().st_mode & 0o777 == 0o600, name

    def test_one_json_object_per_line(self, reading, tmp_path):
        browser = FakeBrowser(
            drains=[
                drain_payload(
                    samples=[
                        {"at": PAGE_NOW - 1000, "speaking": [], "ok": True},
                        {"at": PAGE_NOW, "speaking": ["Sam Okafor"], "ok": True},
                    ]
                ),
                None,
            ]
        )
        run_sampler(reading, browser, tmp_path).stop()
        lines = (tmp_path / roster.ROSTER_FILE).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        for line in lines:
            assert set(json.loads(line)) == {"at", "participants", "speaking", "ok", "reason"}

    def test_a_meeting_with_nothing_to_say_writes_no_files(self, reading, tmp_path):
        browser = FakeBrowser(drains=[drain_payload(), None])
        run_sampler(reading, browser, tmp_path).stop()
        assert not (tmp_path / roster.ROSTER_FILE).exists()
        assert not (tmp_path / roster.CAPTIONS_FILE).exists()


class TestLoadingBack:
    def test_a_truncated_final_line_loses_one_sample_not_the_meeting(self, tmp_path):
        """Exactly what a power cut mid-write leaves behind."""
        path = tmp_path / roster.ROSTER_FILE
        write_lines(path, [
            {"at": 1.0, "speaking": ["Priya Nair"], "participants": ["Priya Nair"]},
            {"at": 3.0, "speaking": [], "participants": ["Priya Nair"]},
        ])
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"at": 5.0, "speaking": ["Pri')

        samples = roster.load_samples(tmp_path)
        assert [s.at for s in samples] == [1.0, 3.0]
        assert samples[0].speaking == ["Priya Nair"]

    def test_a_truncated_caption_file_loses_one_line_not_the_meeting(self, tmp_path):
        path = tmp_path / roster.CAPTIONS_FILE
        write_lines(path, [{"at": 1.0, "speaker": "Priya Nair", "text": "we should ship"}])
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"at": 4.0, "speaker": "Sam Ok')

        lines = roster.load_captions(tmp_path)
        assert len(lines) == 1 and lines[0].text == "we should ship"

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        missing = tmp_path / "never-recorded"
        assert roster.load_samples(missing) == []
        assert roster.load_captions(missing) == []
        assert roster.caption_segments(missing) == []

    def test_rubbish_in_the_file_is_skipped(self, tmp_path):
        (tmp_path / roster.ROSTER_FILE).write_text(
            "not json at all\n[]\n" + json.dumps({"at": 2.0, "speaking": ["Sam"]}) + "\n",
            encoding="utf-8",
        )
        assert [s.at for s in roster.load_samples(tmp_path)] == [2.0]


# ---------------------------------------------------------------------------
# Captions into transcript segments
# ---------------------------------------------------------------------------


def captions_file(tmp_path, rows):
    write_lines(tmp_path / roster.CAPTIONS_FILE, rows)
    return tmp_path


class TestCaptionSegments:
    def test_a_caption_line_arrives_already_attributed(self, tmp_path):
        captions_file(tmp_path, [{"at": 2.0, "speaker": "Priya Nair", "text": "shall we start"}])
        segment = roster.caption_segments(tmp_path)[0]
        assert segment.speaker == "Priya Nair"
        assert segment.track == TRACK_FAR_END
        assert segment.source == SOURCE_ROSTER
        assert segment.text == "shall we start"
        assert segment.start == pytest.approx(2.0)
        assert segment.end > segment.start

    def test_consecutive_lines_from_one_speaker_are_one_turn(self, tmp_path):
        captions_file(tmp_path, [
            {"at": 1.0, "speaker": "Priya Nair", "text": "so the plan is to ship on Friday"},
            {"at": 3.0, "speaker": "Priya Nair", "text": "assuming the tests pass"},
            {"at": 5.0, "speaker": "Priya Nair", "text": "which they should"},
        ])
        segments = roster.caption_segments(tmp_path)
        assert len(segments) == 1
        assert segments[0].text == (
            "so the plan is to ship on Friday assuming the tests pass which they should"
        )
        assert segments[0].start == pytest.approx(1.0)
        assert segments[0].end > 5.0

    def test_a_new_speaker_starts_a_new_turn(self, tmp_path):
        captions_file(tmp_path, [
            {"at": 1.0, "speaker": "Priya Nair", "text": "shall we start"},
            {"at": 3.0, "speaker": "Sam Okafor", "text": "yes lets"},
            {"at": 5.0, "speaker": "Priya Nair", "text": "right then"},
        ])
        segments = roster.caption_segments(tmp_path)
        assert [s.speaker for s in segments] == ["Priya Nair", "Sam Okafor", "Priya Nair"]

    def test_a_long_silence_splits_a_turn(self, tmp_path):
        captions_file(tmp_path, [
            {"at": 1.0, "speaker": "Priya Nair", "text": "one thing before we go"},
            {"at": 300.0, "speaker": "Priya Nair", "text": "sorry I was on mute"},
        ])
        assert len(roster.caption_segments(tmp_path)) == 2

    def test_one_turn_never_swallows_the_whole_meeting(self, tmp_path):
        rows = [
            {"at": float(i * 2), "speaker": "Priya Nair",
             "text": f"and another thing about item number {i}"}
            for i in range(120)
        ]
        captions_file(tmp_path, rows)
        segments = roster.caption_segments(tmp_path)
        assert len(segments) > 1
        assert all(s.end - s.start <= roster.CAPTION_MAX_SEGMENT_SECONDS + 20 for s in segments)

    def test_a_guessed_end_never_runs_over_the_next_turn(self, tmp_path):
        captions_file(tmp_path, [
            {"at": 1.0, "speaker": "Priya Nair",
             "text": "a very long sentence with a great many words in it indeed yes"},
            {"at": 2.0, "speaker": "Sam Okafor", "text": "sorry to interrupt"},
        ])
        first, second = roster.caption_segments(tmp_path)
        assert first.end <= second.start
        assert first.end > first.start

    def test_interim_drafts_of_a_sentence_are_folded_into_the_sentence(self, tmp_path):
        """These interfaces rewrite a line as the words are recognised."""
        captions_file(tmp_path, [
            {"at": 1.0, "speaker": "Priya Nair", "text": "so"},
            {"at": 1.3, "speaker": "Priya Nair", "text": "so the"},
            {"at": 1.6, "speaker": "Priya Nair", "text": "so the plan"},
            {"at": 2.0, "speaker": "Priya Nair", "text": "so the plan is to ship"},
        ])
        segments = roster.caption_segments(tmp_path)
        assert len(segments) == 1
        assert segments[0].text == "so the plan is to ship"
        assert segments[0].start == pytest.approx(1.0), "the turn started when the words did"

    def test_an_exact_repeat_of_a_line_is_not_recorded_twice(self, tmp_path):
        captions_file(tmp_path, [
            {"at": 1.0, "speaker": "Priya Nair", "text": "shall we start"},
            {"at": 1.5, "speaker": "Priya Nair", "text": "shall we start"},
        ])
        assert roster.caption_segments(tmp_path)[0].text == "shall we start"

    def test_a_revision_shorter_than_the_draft_keeps_the_longer_reading(self, tmp_path):
        captions_file(tmp_path, [
            {"at": 1.0, "speaker": "Priya Nair", "text": "so the plan is to ship"},
            {"at": 1.2, "speaker": "Priya Nair", "text": "so the plan"},
        ])
        assert roster.caption_segments(tmp_path)[0].text == "so the plan is to ship"

    def test_two_people_saying_the_same_words_are_two_turns(self, tmp_path):
        captions_file(tmp_path, [
            {"at": 1.0, "speaker": "Priya Nair", "text": "yes"},
            {"at": 2.0, "speaker": "Sam Okafor", "text": "yes"},
        ])
        assert len(roster.caption_segments(tmp_path)) == 2

    def test_a_caption_with_no_author_stays_unattributed(self, tmp_path):
        """An unknown speaker stays unknown; a blank is better than a guess."""
        captions_file(tmp_path, [{"at": 1.0, "speaker": "", "text": "can everyone hear me"}])
        segment = roster.caption_segments(tmp_path)[0]
        assert segment.speaker == ""
        assert segment.label() == "Remote speaker"

    def test_a_diagnostic_string_never_becomes_a_speaker(self, tmp_path):
        captions_file(tmp_path, [
            {"at": 1.0, "speaker": "no-speaking-signal", "text": "hello"},
            {"at": 30.0, "speaker": "exception:TypeError", "text": "a later remark"},
        ])
        assert [s.speaker for s in roster.caption_segments(tmp_path)] == ["", ""]

    def test_an_empty_line_is_dropped(self, tmp_path):
        captions_file(tmp_path, [
            {"at": 1.0, "speaker": "Priya Nair", "text": "   "},
            {"at": 2.0, "speaker": "Priya Nair", "text": "actual words"},
        ])
        segments = roster.caption_segments(tmp_path)
        assert len(segments) == 1 and segments[0].text == "actual words"


class TestMinUsefulCaptions:
    def test_it_is_a_defensible_number(self):
        assert isinstance(roster.MIN_USEFUL_CAPTIONS, int)
        assert 5 <= roster.MIN_USEFUL_CAPTIONS <= 40

    def test_captions_switched_on_for_a_moment_do_not_cover_the_far_end(self, tmp_path):
        """Otherwise the whole remote half of the meeting is thrown away."""
        captions_file(tmp_path, [
            {"at": float(i * 4), "speaker": "Priya Nair" if i % 2 else "Sam Okafor",
             "text": "a short remark"}
            for i in range(4)
        ])
        assert len(roster.caption_segments(tmp_path)) < roster.MIN_USEFUL_CAPTIONS

    def test_a_captioned_conversation_does(self, tmp_path):
        captions_file(tmp_path, [
            {"at": float(i * 6), "speaker": "Priya Nair" if i % 2 else "Sam Okafor",
             "text": "something worth saying about the release"}
            for i in range(30)
        ])
        assert len(roster.caption_segments(tmp_path)) >= roster.MIN_USEFUL_CAPTIONS


# ---------------------------------------------------------------------------
# The sampler's own behaviour
# ---------------------------------------------------------------------------


class TestSampler:
    def test_stop_is_safe_when_start_never_succeeded(self, reading):
        sampler = RosterSampler(reading, FakeBrowser())
        assert sampler.running is False
        assert sampler.snapshot() is None
        assert sampler.stop() == []
        assert sampler.stop() == [], "stopping twice is not an error either"

    def test_development_mode_starts_nothing_and_says_why(self, config, tmp_path, events):
        config.update({"MINUTES_IDENTIFY_REMOTE": True, "DEV_MODE": True})
        session = tmp_path / "session"
        browser = FakeBrowser()
        sampler = RosterSampler(config, browser)
        sampler.start("teams", session)

        assert sampler.running is False
        assert browser.scripts == [], "a no-op must not touch the meeting page"
        assert sampler.stop() == []
        assert not session.exists(), "a no-op must not even make a directory"
        snapshot = sampler.snapshot()
        assert snapshot is not None and snapshot.ok is False
        assert "Development mode" in snapshot.reason
        assert "minutes.roster_unavailable" in events.names()

    def test_switched_off_starts_nothing(self, config, tmp_path):
        config.update({"MINUTES_IDENTIFY_REMOTE": False})
        sampler = RosterSampler(config, FakeBrowser())
        sampler.start("teams", tmp_path)
        assert sampler.running is False
        assert sampler.stop() == []

    def test_stop_returns_what_was_collected(self, reading, tmp_path):
        browser = FakeBrowser(
            drains=[
                drain_payload(samples=[
                    {"at": PAGE_NOW - 2000, "speaking": ["Priya Nair"], "ok": True},
                    {"at": PAGE_NOW, "speaking": [], "ok": True},
                ]),
                None,
            ]
        )
        samples = run_sampler(reading, browser, tmp_path).stop()
        assert [s.speaking for s in samples] == [["Priya Nair"], []]
        assert all(isinstance(s, RosterSample) for s in samples)

    def test_the_snapshot_says_what_is_happening_right_now(self, reading, tmp_path):
        browser = FakeBrowser(
            drains=[drain_payload(speaking=["Sam Okafor"]), None]
        )
        sampler = run_sampler(reading, browser, tmp_path)
        snapshot = sampler.snapshot()
        assert snapshot is not None
        assert snapshot.speaking == ["Sam Okafor"]
        assert snapshot.participants == ["Priya Nair", "Sam Okafor"]
        assert 0.0 <= snapshot.at < 60.0
        sampler.stop()

    def test_a_diagnostic_never_becomes_a_speaker_name(self, reading, tmp_path):
        browser = FakeBrowser(
            drains=[
                drain_payload(
                    samples=[{
                        "at": PAGE_NOW,
                        "speaking": ["no-speaking-signal", "exception:TypeError", "Priya Nair"],
                        "participants": ["unknown", "Priya Nair"],
                        "ok": True,
                    }],
                ),
                None,
            ]
        )
        samples = run_sampler(reading, browser, tmp_path).stop()
        assert samples[0].speaking == ["Priya Nair"]
        assert samples[0].participants == ["Priya Nair"]

    def test_starting_twice_does_not_start_two_samplers(self, reading, tmp_path):
        """Two samplers writing one timeline is the bug the join loop already had."""

        class Blocking:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()

            def read_meeting_page(self, script, *, timeout=6.0, user_gesture=False):
                self.entered.set()
                self.release.wait(timeout=10)

        browser = Blocking()
        sampler = RosterSampler(reading, browser)
        sampler.start("teams", tmp_path)
        assert browser.entered.wait(timeout=5)
        assert sampler.running is True
        first = sampler._thread

        sampler.start("teams", tmp_path)
        assert sampler._thread is first, "the second start must be ignored"

        browser.release.set()
        assert sampler.stop() == []

    def test_a_browser_that_throws_is_survived(self, reading, tmp_path):
        class Exploding:
            def read_meeting_page(self, script, *, timeout=6.0, user_gesture=False):
                raise RuntimeError("the websocket went away")

        sampler = run_sampler(reading, Exploding(), tmp_path, stop_after=3)
        assert sampler.stop() == []

    def test_a_nonsense_payload_is_survived(self, reading, tmp_path):
        browser = FakeBrowser(drains=["not a dict", {"installed": True, "ok": True}, None])
        assert run_sampler(reading, browser, tmp_path, stop_after=8).stop() == []


class TestSignalAbsent:
    def test_a_surface_with_no_speaker_all_meeting_is_logged_once(
        self, reading, tmp_path, events
    ):
        """The line that says a vendor shipped a breaking change."""
        browser = FakeBrowser(
            drains=[drain_payload(surface=True, signal=False) for _ in range(3)] + [None]
        )
        sampler = run_sampler(reading, browser, tmp_path, stop_after=6)
        sampler.stop()
        sampler.stop()  # stopping twice must not log it twice
        assert events.names().count("minutes.speaker_signal_absent") == 1
        fields = dict(events.events[[e for e, _ in events.events].index(
            "minutes.speaker_signal_absent")][1])
        assert fields["provider"] == "teams"
        assert fields["source"]

    def test_a_meeting_where_somebody_spoke_is_not_logged(self, reading, tmp_path, events):
        browser = FakeBrowser(
            drains=[
                drain_payload(
                    surface=True, signal=True,
                    samples=[{"at": PAGE_NOW, "speaking": ["Priya Nair"], "ok": True}],
                ),
                None,
            ]
        )
        run_sampler(reading, browser, tmp_path).stop()
        assert "minutes.speaker_signal_absent" not in events.names()

    def test_never_finding_the_meeting_at_all_is_not_this_alarm(
        self, reading, tmp_path, events
    ):
        """"I could not find the page" is a different fault from "it went quiet"."""
        browser = FakeBrowser(
            drains=[drain_payload(ok=False, surface=False, signal=False), None]
        )
        run_sampler(reading, browser, tmp_path).stop()
        assert "minutes.speaker_signal_absent" not in events.names()

    def test_no_names_are_ever_logged(self, reading, tmp_path, events):
        browser = FakeBrowser(
            drains=[
                drain_payload(
                    samples=[{"at": PAGE_NOW, "speaking": ["Priya Nair"], "ok": True}],
                    captions=[{"at": PAGE_NOW, "speaker": "Priya Nair", "text": "a secret"}],
                ),
                None,
            ]
        )
        run_sampler(reading, browser, tmp_path).stop()
        printed = json.dumps(events.events, default=str)
        assert "Priya Nair" not in printed
        assert "a secret" not in printed


class TestTurningCaptionsOn:
    def test_off_by_default_nothing_is_pressed(self, reading, tmp_path):
        assert reading.bool_("MINUTES_TURN_ON_CAPTIONS") is False
        browser = FakeBrowser(drains=[drain_payload(), None])
        run_sampler(reading, browser, tmp_path).stop()
        assert "click" not in browser.kinds

    def test_on_it_is_tried_exactly_once(self, reading, tmp_path, events):
        reading.update({"MINUTES_TURN_ON_CAPTIONS": True})
        browser = FakeBrowser(drains=[drain_payload() for _ in range(5)])
        run_sampler(reading, browser, tmp_path, stop_after=5).stop()
        assert browser.kinds.count("click") == 1, browser.kinds
        assert browser.kinds[0] == "click", "captions go on before the watching starts"
        assert "minutes.roster_captions_requested" in events.names()

    def test_only_the_presses_claim_a_user_gesture(self, reading, tmp_path):
        """A reader must not keep telling the page that somebody is using it.

        Both controls the feature may touch — live captions, and the
        participant list — are gated behind a real interaction, so pressing
        either has to claim one. Every other pass only reads, and claiming a
        gesture every couple of seconds would leave the page permanently
        convinced there was a person at the keyboard.
        """
        reading.update(
            {"MINUTES_TURN_ON_CAPTIONS": True, "MINUTES_OPEN_ROSTER": True}
        )
        browser = FakeBrowser(drains=[drain_payload() for _ in range(4)])
        run_sampler(reading, browser, tmp_path, stop_after=4).stop()

        by_kind = list(zip(browser.kinds, browser.gestures))
        assert ("click", True) in by_kind, by_kind
        assert ("panel", True) in by_kind, by_kind
        assert not any(
            gesture for kind, gesture in by_kind if kind not in PRESSES
        ), "a reading pass claimed a user gesture"

    def test_a_reading_only_meeting_never_claims_a_gesture(self, reading, tmp_path):
        browser = FakeBrowser(drains=[drain_payload() for _ in range(3)])
        run_sampler(reading, browser, tmp_path, stop_after=3).stop()
        assert not any(browser.gestures)

    def test_a_control_that_is_not_there_is_not_an_error(self, reading, tmp_path):
        reading.update({"MINUTES_TURN_ON_CAPTIONS": True})
        browser = FakeBrowser(drains=[drain_payload(), None], clicked=None)
        run_sampler(reading, browser, tmp_path).stop()
        assert browser.kinds.count("drain") >= 1


class TestOpeningTheParticipantList:
    """``MINUTES_OPEN_ROSTER``: some tenants name nobody until it is open."""

    def test_off_by_default_nothing_is_pressed(self, reading, tmp_path):
        assert reading.bool_("MINUTES_OPEN_ROSTER") is False
        browser = FakeBrowser(drains=[drain_payload(), None])
        run_sampler(reading, browser, tmp_path).stop()
        assert "panel" not in browser.kinds
        assert not any(browser.gestures)

    def test_on_it_is_tried_exactly_once(self, reading, tmp_path, events):
        reading.update({"MINUTES_OPEN_ROSTER": True})
        browser = FakeBrowser(drains=[drain_payload() for _ in range(5)])
        run_sampler(reading, browser, tmp_path, stop_after=5).stop()
        assert browser.kinds.count("panel") == 1, browser.kinds
        assert browser.kinds[0] == "panel", "the panel opens before the watching"
        assert panel_fields(events)["pressed"] is True

    def test_the_press_claims_a_user_gesture(self, reading, tmp_path):
        """The page gates the control behind a real interaction."""
        reading.update({"MINUTES_OPEN_ROSTER": True})
        browser = FakeBrowser(drains=[drain_payload(), None])
        run_sampler(reading, browser, tmp_path).stop()
        assert ("panel", True) in list(zip(browser.kinds, browser.gestures))
        assert not any(
            gesture for kind, gesture in zip(browser.kinds, browser.gestures)
            if kind != "panel"
        )

    def test_a_panel_already_open_is_left_exactly_as_it_is(
        self, reading, tmp_path, events
    ):
        """Pressing the control now would take away a panel somebody opened."""
        reading.update({"MINUTES_OPEN_ROSTER": True})
        browser = FakeBrowser(
            drains=[drain_payload(), None],
            panel={"open": True, "clicked": None, "source": '[data-tid="roster"]'},
        )
        run_sampler(reading, browser, tmp_path).stop()
        assert browser.kinds.count("panel") == 1, "it looked, once"
        fields = panel_fields(events)
        assert fields["already_open"] is True
        assert fields["pressed"] is False

    def test_a_control_that_is_not_there_is_not_an_error(
        self, reading, tmp_path, events
    ):
        reading.update({"MINUTES_OPEN_ROSTER": True})
        browser = FakeBrowser(
            drains=[drain_payload(), None],
            panel={"open": False, "clicked": None, "candidates": 40},
        )
        run_sampler(reading, browser, tmp_path).stop()
        assert browser.kinds.count("drain") >= 1, "the watching carried on"
        fields = panel_fields(events)
        assert fields["pressed"] is False
        assert fields["already_open"] is False

    def test_a_page_that_answers_nothing_at_all_is_not_an_error(
        self, reading, tmp_path, events
    ):
        """``read_meeting_page`` returns None for every failure there is."""
        reading.update({"MINUTES_OPEN_ROSTER": True})
        browser = FakeBrowser(drains=[drain_payload(), None], panel=None)
        browser.panel = None
        run_sampler(reading, browser, tmp_path).stop()
        assert panel_fields(events)["pressed"] is False

    def test_the_hunt_is_built_for_the_meeting_being_recorded(
        self, reading, tmp_path
    ):
        reading.update({"MINUTES_OPEN_ROSTER": True})
        browser = FakeBrowser(drains=[drain_payload(), None])
        run_sampler(reading, browser, tmp_path, provider="meet").stop()
        assert "Show everyone" in browser.script_of("panel")

    def test_no_names_are_logged_by_the_press(self, reading, tmp_path, events):
        reading.update({"MINUTES_OPEN_ROSTER": True})
        browser = FakeBrowser(
            drains=[drain_payload(), None],
            panel={"open": False, "clicked": "people (2) priya nair"},
        )
        run_sampler(reading, browser, tmp_path).stop()
        assert "Priya" not in json.dumps(events.events, default=str)

    def test_both_switches_on_is_two_presses_and_no_more(
        self, reading, tmp_path
    ):
        reading.update(
            {"MINUTES_TURN_ON_CAPTIONS": True, "MINUTES_OPEN_ROSTER": True}
        )
        browser = FakeBrowser(drains=[drain_payload() for _ in range(5)])
        run_sampler(reading, browser, tmp_path, stop_after=5).stop()
        assert browser.kinds.count("click") == 1
        assert browser.kinds.count("panel") == 1
        assert browser.kinds[:2] == ["click", "panel"]


# ---------------------------------------------------------------------------
# A meeting drawn in a frame of its own
# ---------------------------------------------------------------------------


class TestWhatCountsAsAnAnswer:
    """``_answered`` is what stops the frame walk. Each script says it its own way."""

    @pytest.mark.parametrize(
        "payload",
        [
            {"ok": False, "installed": False, "reason": "not-installed"},
            {"ok": True, "installed": False},
            {"ok": False, "state": "no-surface"},
            {"clicked": None, "filled_name": False, "waiting": ""},
            {"open": False, "clicked": None, "candidates": 12},
            {"ok": False, "participants": [], "speaking": []},
            None,
            "not a reply at all",
        ],
    )
    def test_nothing_found_means_try_the_next_frame(self, payload):
        assert roster._answered(payload) is False

    @pytest.mark.parametrize(
        "payload",
        [
            {"ok": True, "installed": True, "participants": []},
            {"ok": True, "state": "installed"},
            {"ok": True, "state": "already"},
            {"clicked": "people"},
            {"open": True, "clicked": None},
            {"ok": False, "participants": [{"name": "Priya Nair"}]},
            {"ok": False, "speaking": ["Priya Nair"]},
        ],
    )
    def test_an_answer_stops_the_walk(self, payload):
        assert roster._answered(payload) is True

    def test_a_press_that_worked_is_never_offered_to_another_frame(self):
        """The expensive mistake: the same control pressed twice over."""
        assert roster._answered({"clicked": "people", "candidates": 3}) is True


class TestAMeetingInAFrameOfItsOwn:
    """The out-of-process stage: nothing in the page around it can see in.

    ``meeting_in_top=False`` is that page — the shell answers every script with
    "there is nothing here", and only the walk through the frames finds the
    meeting at all.
    """

    def test_the_observer_settles_in_the_frame_that_has_the_meeting(
        self, reading, tmp_path, events
    ):
        browser = FakeBrowser(drains=[drain_payload(), None], meeting_in_top=False)
        run_sampler(reading, browser, tmp_path).stop()
        assert "minutes.roster_watching" in events.names()
        assert browser.kinds.count("install") == 1, (
            "the shell refused it, so there was nothing to fall back from"
        )

    def test_the_recording_still_gets_its_samples(self, reading, tmp_path):
        browser = FakeBrowser(
            drains=[
                drain_payload(
                    samples=[{"at": PAGE_NOW, "speaking": ["Priya Nair"], "ok": True}]
                ),
                None,
            ],
            meeting_in_top=False,
        )
        samples = run_sampler(reading, browser, tmp_path).stop()
        assert [s.speaking for s in samples] == [["Priya Nair"]]

    def test_every_pass_goes_through_the_walking_door(
        self, reading, tmp_path
    ):
        browser = FakeBrowser(drains=[drain_payload(), None], meeting_in_top=False)
        run_sampler(reading, browser, tmp_path).stop()
        assert browser.walked.count("install") == 1
        assert browser.walked.count("drain") >= 1

    def test_the_presses_reach_the_frame_too(self, reading, tmp_path, events):
        reading.update({"MINUTES_OPEN_ROSTER": True})
        browser = FakeBrowser(drains=[drain_payload(), None], meeting_in_top=False)
        run_sampler(reading, browser, tmp_path).stop()
        assert panel_fields(events)["pressed"] is True
        assert browser.kinds.count("panel") == 1

    def test_a_page_where_the_meeting_is_where_it_has_always_been_never_walks(
        self, reading, tmp_path
    ):
        """The whole cost argument: a healthy page pays for no second frame."""
        reading.update(
            {"MINUTES_TURN_ON_CAPTIONS": True, "MINUTES_OPEN_ROSTER": True}
        )
        browser = FakeBrowser(drains=[drain_payload() for _ in range(4)])
        run_sampler(reading, browser, tmp_path, stop_after=4).stop()
        assert browser.asked.count(False) == 0, "a second frame was asked for nothing"


class TestWhenNoFrameOwnsUp:
    """A pre-join screen, or a provider whose surface we do not recognise."""

    def test_the_observer_goes_where_it_has_always_gone(
        self, reading, tmp_path, events
    ):
        browser = FakeBrowser(drains=[drain_payload(), None], surface=False)
        run_sampler(reading, browser, tmp_path).stop()
        assert browser.kinds.count("install") == 2, (
            "one offer refused everywhere, then the install this has always done"
        )
        assert "minutes.roster_watching" in events.names()

    def test_the_second_install_asks_for_no_surface_at_all(
        self, reading, tmp_path
    ):
        browser = FakeBrowser(drains=[drain_payload(), None], surface=False)
        run_sampler(reading, browser, tmp_path).stop()
        installs = [s for s, k in zip(browser.scripts, browser.kinds) if k == "install"]
        assert "var REQUIRE_SURFACE = true;" in installs[0]
        assert "var REQUIRE_SURFACE = false;" in installs[1]

    def test_it_still_records_what_it_can_see_from_there(self, reading, tmp_path):
        browser = FakeBrowser(
            drains=[
                drain_payload(
                    samples=[{"at": PAGE_NOW, "speaking": ["Sam Okafor"], "ok": True}]
                ),
                None,
            ],
            surface=False,
        )
        samples = run_sampler(reading, browser, tmp_path).stop()
        assert [s.speaking for s in samples] == [["Sam Okafor"]]

    def test_a_meeting_that_is_not_on_screen_is_still_gone(self, reading, tmp_path):
        """"No answer at all" must not turn into a second install attempt."""
        browser = FakeBrowser(install=False, surface=False)
        run_sampler(reading, browser, tmp_path, stop_after=4)
        assert browser.kinds.count("install") >= 3
        assert all(
            "var REQUIRE_SURFACE = true;" in script
            for script, kind in zip(browser.scripts, browser.kinds)
            if kind == "install"
        )


class TestAnOlderBrowserSeam:
    def test_a_stand_in_without_the_new_door_still_works(self, reading, tmp_path):
        """``read_meeting_frames`` is newer than some of the stand-ins for it."""

        class OnlyTheOldDoor:
            def __init__(self):
                self.kinds = []

            def read_meeting_page(self, script, *, timeout=6.0, user_gesture=False):
                kind = script_kind(script)
                self.kinds.append(kind)
                if kind == "install":
                    return {"ok": True, "state": "installed"}
                return drain_payload() if kind == "drain" else {"clicked": None}

        browser = OnlyTheOldDoor()
        sampler = run_sampler(reading, browser, tmp_path, stop_after=3)
        sampler.stop()
        assert "install" in browser.kinds and "drain" in browser.kinds


# ---------------------------------------------------------------------------
# What the rest of the feature is handed
# ---------------------------------------------------------------------------


class TestTheContractWithAttribute:
    def test_samples_are_what_speaking_spans_expects(self, tmp_path):
        """``attribute.speaking_spans`` reads ``at`` and ``speaking``, nothing else."""
        from app.minutes.attribute import speaking_spans

        write_lines(tmp_path / roster.ROSTER_FILE, [
            {"at": 0.0, "speaking": [], "participants": ["Priya Nair", "Sam Okafor"]},
            {"at": 2.0, "speaking": ["Priya Nair"]},
            {"at": 8.0, "speaking": []},
            {"at": 10.0, "speaking": ["Sam Okafor"]},
            {"at": 14.0, "speaking": []},
        ])
        spans = speaking_spans(roster.load_samples(tmp_path))
        by_name = {name: (start, end) for start, end, name in spans}
        assert set(by_name) == {"Priya Nair", "Sam Okafor"}
        assert by_name["Priya Nair"][0] < by_name["Sam Okafor"][0]
        for start, end in by_name.values():
            assert 0.0 <= start < end < 60.0

    def test_a_dataclass_survives_a_round_trip_through_the_file(self):
        sample = RosterSample(at=1.25, participants=["Priya Nair"], speaking=["Priya Nair"])
        assert RosterSample.from_dict(json.loads(json.dumps(sample.to_dict()))) == sample
        line = CaptionLine(at=2.5, speaker="Priya Nair", text="hello")
        assert CaptionLine.from_dict(json.loads(json.dumps(line.to_dict()))) == line
