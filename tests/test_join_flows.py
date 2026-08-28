"""Join automation: the parts that can be tested without a live meeting.

The injected JavaScript itself is exercised against a real DOM by
``tests/js/test_clicker.js`` (optional; needs Node and jsdom). What is tested
here is everything Python decides: which buttons are tried, in what order, and
what happens to the meeting URL.
"""

from __future__ import annotations

import json

import pytest

from app.join_flows import (
    GENERIC_FLOW,
    PROVIDER_FLOWS,
    build_click_script,
    build_in_call_script,
    flow_for,
    ordered_button_texts,
    prepare_url,
)
from app.meeting_links import PROVIDERS


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
