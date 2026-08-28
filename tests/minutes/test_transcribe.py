"""Choosing an engine, running it, and reading the result back.

Nothing here has whisper.cpp, faster-whisper or vosk installed — the appliance
often will not either — so the engines are exercised through the capability
probes and through fixtures of the output the real binaries produce. The parts
that are ours rather than theirs — which engine gets picked, how the two tracks
are merged, and which lines are the room hearing its own speaker — are tested
directly.
"""

from __future__ import annotations

import importlib
import json
import types

import pytest

from app.minutes import audio, transcribe
from app.minutes.transcript import TRACK_FAR_END, TRACK_ROOM, Segment
from app.system_service import CommandResult

#: whisper.cpp prints this to stdout for every segment, in every version.
WHISPER_STDOUT = """
[00:00:00.000 --> 00:00:03.480]   Right, shall we make a start.
[00:00:03.480 --> 00:00:07.960]   I have put last month's figures in the shared folder.
[00:00:07.960 --> 00:00:11.200]   Anything else before we finish?
"""

#: ``-oj`` writes this beside the WAV. Offsets are milliseconds.
WHISPER_JSON = {
    "systeminfo": "whisper.cpp",
    "model": {"type": "base.en"},
    "transcription": [
        {
            "timestamps": {"from": "00:00:00,000", "to": "00:00:03,480"},
            "offsets": {"from": 0, "to": 3480},
            "text": " Right, shall we make a start.",
        },
        {
            "timestamps": {"from": "00:00:03,480", "to": "00:00:07,960"},
            "offsets": {"from": 3480, "to": 7960},
            "text": " I have put last month's figures in the shared folder.",
        },
    ],
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def minutes_paths(room_dirs):
    from app.minutes import deps
    from app.minutes import paths as minutes_paths_module

    importlib.reload(minutes_paths_module)
    minutes_paths_module.ensure_dirs()
    deps.refresh()
    yield minutes_paths_module
    deps.refresh()
    importlib.reload(minutes_paths_module)


@pytest.fixture()
def nothing_installed(minutes_paths):
    """The honest state of a fresh appliance, and of this test machine."""
    from app.minutes import deps

    for name, detail in (
        ("whisper-cpp", "“whisper-cli” was not found on PATH."),
        ("faster_whisper", "“faster-whisper” is not installed."),
        ("vosk", "“vosk” is not installed."),
    ):
        deps.set_probe_for_tests(name, False, detail)
    return deps


@pytest.fixture()
def whisper_installed(nothing_installed, minutes_paths):
    """whisper.cpp on PATH with a model beside it."""
    nothing_installed.set_probe_for_tests(
        "whisper-cpp", True, path="/usr/bin/whisper-cli"
    )
    model = minutes_paths.MODELS_DIR / "ggml-base.en.bin"
    model.write_bytes(b"not really a model")
    return model


@pytest.fixture()
def session_dir(minutes_paths, tmp_path):
    directory = tmp_path / "session"
    directory.mkdir()
    return directory


def recorded(session_dir, room=12.0, far_end=12.0):
    if room:
        audio.write_silence(session_dir / "room.wav", room)
    if far_end:
        audio.write_silence(session_dir / "farend.wav", far_end)
    return session_dir


def room(start, end, text):
    return Segment(start=start, end=end, text=text, track=TRACK_ROOM)


def far(start, end, text):
    return Segment(start=start, end=end, text=text, track=TRACK_FAR_END)


# ---------------------------------------------------------------------------
# Choosing an engine
# ---------------------------------------------------------------------------


class TestChoosingAnEngine:
    @pytest.mark.parametrize(
        "setting, expected",
        [
            ("whisper-cpp", transcribe.WHISPER_CPP),
            ("faster-whisper", transcribe.FASTER_WHISPER),
            ("vosk", transcribe.VOSK),
            ("none", transcribe.NONE),
        ],
    )
    def test_an_explicit_choice_is_honoured_even_when_it_is_not_installed(
        self, config, nothing_installed, setting, expected
    ):
        """So that ``available()`` can explain what to install, rather than lie."""
        config.update({"MINUTES_STT_ENGINE": setting})
        engine = transcribe.choose_engine(config)
        assert engine.name == expected
        assert transcribe.chosen_engine_name(config) == expected

    def test_switching_it_off_is_a_valid_answer(self, config, nothing_installed):
        config.update({"MINUTES_STT_ENGINE": "none"})
        ok, why = transcribe.available(config)
        assert not ok
        assert "switched off" in why

    def test_auto_falls_back_to_the_placeholder_in_development(
        self, mock_config, nothing_installed
    ):
        assert transcribe.chosen_engine_name(mock_config) == transcribe.MOCK
        assert transcribe.available(mock_config) == (True, "")

    def test_auto_with_nothing_installed_explains_what_to_install(
        self, config, nothing_installed
    ):
        engine = transcribe.choose_engine(config)
        assert engine.name == transcribe.WHISPER_CPP
        ok, why = transcribe.available(config)
        assert not ok
        assert "whisper" in why.lower()

    def test_auto_prefers_whisper_cpp_when_it_is_there(self, config, whisper_installed):
        assert transcribe.chosen_engine_name(config) == transcribe.WHISPER_CPP
        assert transcribe.available(config) == (True, "")

    def test_auto_falls_through_to_faster_whisper(self, config, nothing_installed):
        nothing_installed.set_probe_for_tests("faster_whisper", True)
        assert transcribe.chosen_engine_name(config) == transcribe.FASTER_WHISPER

    def test_auto_falls_through_to_vosk(self, config, nothing_installed, minutes_paths):
        nothing_installed.set_probe_for_tests("vosk", True)
        model = minutes_paths.MODELS_DIR / "vosk-model-small-en-us-0.15"
        (model / "conf").mkdir(parents=True)
        assert transcribe.chosen_engine_name(config) == transcribe.VOSK

    def test_vosk_without_a_model_is_not_chosen(self, config, nothing_installed):
        nothing_installed.set_probe_for_tests("vosk", True)
        ok, why = transcribe.VoskEngine(config).available(config)
        assert not ok
        assert "vosk model" in why
        assert transcribe.chosen_engine_name(config) == transcribe.WHISPER_CPP

    def test_an_unknown_engine_name_falls_back_to_automatic(self, whisper_installed):
        """A config file edited by hand, or one written by a newer version."""

        class Stubborn:
            def str_(self, key):
                return "wishful-thinking" if key == "MINUTES_STT_ENGINE" else ""

            def bool_(self, key):
                return False

            def int_(self, key):
                return 0

        assert transcribe.chosen_engine_name(Stubborn()) == transcribe.WHISPER_CPP


class TestHardwareTiers:
    """Trap T1: a Pi 3 must not be asked to transcribe an hour of audio."""

    @pytest.mark.parametrize("tier", ["balanced", "high"])
    def test_capable_hardware_may_transcribe_locally(
        self, config, whisper_installed, tier
    ):
        config.update({"PERFORMANCE_PROFILE": tier})
        assert transcribe.WhisperCppEngine(config).available(config) == (True, "")

    def test_the_low_tier_refuses_whisper_cpp_and_says_why(
        self, config, whisper_installed
    ):
        config.update({"PERFORMANCE_PROFILE": "low"})
        ok, why = transcribe.WhisperCppEngine(config).available(config)
        assert not ok
        assert "too slow" in why
        assert "Settings" in why

    def test_the_low_tier_refuses_faster_whisper_too(self, config, nothing_installed):
        nothing_installed.set_probe_for_tests("faster_whisper", True)
        config.update({"PERFORMANCE_PROFILE": "low"})
        ok, why = transcribe.FasterWhisperEngine(config).available(config)
        assert not ok
        assert "too slow" in why

    def test_the_low_tier_still_allows_vosk(self, config, nothing_installed, minutes_paths):
        """Kaldi runs faster than real time even on the slow hardware."""
        nothing_installed.set_probe_for_tests("vosk", True)
        (minutes_paths.MODELS_DIR / "vosk-model" / "conf").mkdir(parents=True)
        config.update({"PERFORMANCE_PROFILE": "low"})
        assert transcribe.VoskEngine(config).available(config) == (True, "")

    def test_faster_whisper_picks_a_bigger_model_on_a_real_computer(self, config):
        config.update({"PERFORMANCE_PROFILE": "high"})
        assert transcribe.faster_whisper_model(config) == "small.en"
        config.update({"PERFORMANCE_PROFILE": "balanced"})
        assert transcribe.faster_whisper_model(config) == "base.en"


class TestEngineReport:
    def test_every_engine_is_listed_with_a_reason(self, config, nothing_installed):
        rows = transcribe.engine_report(config)
        names = [row["name"] for row in rows]
        assert names == ["whisper-cpp", "faster-whisper", "vosk", "mock", "none"]
        for row in rows:
            if not row["ok"]:
                assert row["detail"], f"{row['name']} is unavailable without saying why"

    def test_exactly_one_engine_is_marked_as_chosen(self, config, whisper_installed):
        rows = transcribe.engine_report(config)
        chosen = [row["name"] for row in rows if row["chosen"]]
        assert chosen == [transcribe.WHISPER_CPP]

    def test_the_placeholder_is_always_usable(self, config, nothing_installed):
        rows = {row["name"]: row for row in transcribe.engine_report(config)}
        assert rows["mock"]["ok"] is True


# ---------------------------------------------------------------------------
# whisper.cpp: the command line and both output shapes
# ---------------------------------------------------------------------------


class TestWhisperCpp:
    def test_the_command_line_asks_for_timestamps_and_runs_politely(
        self, config, whisper_installed, session_dir, monkeypatch
    ):
        recorded(session_dir, far_end=0)
        seen: list[list[str]] = []

        def fake_run(argv, **kwargs):
            seen.append(list(argv))
            return CommandResult(True, 0, WHISPER_STDOUT, "")

        monkeypatch.setattr(transcribe, "run", fake_run)
        monkeypatch.setattr(transcribe, "which", lambda name: "/usr/bin/nice")

        engine = transcribe.WhisperCppEngine(config)
        utterances, error = engine.transcribe(
            session_dir / "room.wav", language="en", timeout=600
        )
        assert error == ""
        assert len(utterances) == 3

        argv = seen[0]
        assert argv[:3] == ["nice", "-n", "15"], "transcription must not fight the kiosk"
        assert argv[3] == "/usr/bin/whisper-cli"
        assert "-oj" in argv, "the JSON output carries exact offsets"
        assert "-m" in argv and str(whisper_installed) in argv
        assert "-f" in argv and str(session_dir / "room.wav") in argv
        assert argv[argv.index("-l") + 1] == "en"

    def test_the_json_output_is_preferred_and_tidied_away(
        self, config, whisper_installed, session_dir, monkeypatch
    ):
        recorded(session_dir, far_end=0)
        written = session_dir / "room.json"

        def fake_run(argv, **kwargs):
            written.write_text(json.dumps(WHISPER_JSON), encoding="utf-8")
            return CommandResult(True, 0, "", "")

        monkeypatch.setattr(transcribe, "run", fake_run)
        utterances, error = transcribe.WhisperCppEngine(config).transcribe(
            session_dir / "room.wav", language="en", timeout=600
        )
        assert error == ""
        assert [u.start for u in utterances] == [0.0, 3.48]
        assert utterances[0].text == "Right, shall we make a start."
        assert not written.exists(), "the intermediate file should not be left behind"

    def test_a_failing_binary_reports_rather_than_raises(
        self, config, whisper_installed, session_dir, monkeypatch
    ):
        recorded(session_dir, far_end=0)
        monkeypatch.setattr(
            transcribe,
            "run",
            lambda argv, **kw: CommandResult(False, 1, "", "error: failed to load model"),
        )
        utterances, error = transcribe.WhisperCppEngine(config).transcribe(
            session_dir / "room.wav", language="en", timeout=600
        )
        assert utterances == []
        assert "failed to load model" in error

    def test_without_a_model_it_says_so(self, config, nothing_installed, session_dir):
        nothing_installed.set_probe_for_tests("whisper-cpp", True, path="/usr/bin/whisper-cli")
        recorded(session_dir, far_end=0)
        utterances, error = transcribe.WhisperCppEngine(config).transcribe(
            session_dir / "room.wav", language="en", timeout=600
        )
        assert utterances == []
        assert "model" in error

    def test_the_configured_model_wins_over_the_downloaded_one(
        self, config, whisper_installed, tmp_path
    ):
        chosen = tmp_path / "ggml-small.en.bin"
        chosen.write_bytes(b"model")
        config.update({"MINUTES_STT_MODEL": str(chosen)})
        assert transcribe.whisper_model(config) == chosen

    def test_a_configured_model_that_is_missing_is_not_silently_replaced(
        self, config, whisper_installed
    ):
        config.update({"MINUTES_STT_MODEL": "/no/such/model.bin"})
        assert transcribe.whisper_model(config) is None
        ok, why = transcribe.WhisperCppEngine(config).available(config)
        assert not ok and "model" in why


class TestParsingWhisperOutput:
    def test_the_stdout_form(self):
        utterances = transcribe.parse_whisper_output(WHISPER_STDOUT)
        assert len(utterances) == 3
        assert utterances[0].start == 0.0
        assert utterances[0].end == 3.48
        assert utterances[0].text == "Right, shall we make a start."
        assert utterances[2].text == "Anything else before we finish?"

    def test_the_stdout_form_with_comma_decimals_and_short_clocks(self):
        text = "[00:02,500 --> 00:04,000]  Later in the meeting."
        utterances = transcribe.parse_whisper_output(text)
        assert utterances[0].start == 2.5
        assert utterances[0].end == 4.0

    def test_stray_lines_are_ignored(self):
        text = "whisper_init_from_file: loading model\n" + WHISPER_STDOUT
        assert len(transcribe.parse_whisper_output(text)) == 3

    def test_the_json_form(self):
        utterances = transcribe.parse_whisper_json(WHISPER_JSON)
        assert [u.start for u in utterances] == [0.0, 3.48]
        assert [u.end for u in utterances] == [3.48, 7.96]

    def test_the_json_form_without_offsets_falls_back_to_the_clocks(self):
        payload = {
            "transcription": [
                {
                    "timestamps": {"from": "00:00:01,000", "to": "00:00:02,500"},
                    "text": " Only clocks here.",
                }
            ]
        }
        utterances = transcribe.parse_whisper_json(payload)
        assert utterances[0].start == 1.0
        assert utterances[0].end == 2.5

    def test_rubbish_is_no_segments_rather_than_an_exception(self):
        assert transcribe.parse_whisper_json(None) == []
        assert transcribe.parse_whisper_json({"transcription": "nonsense"}) == []
        assert transcribe.parse_whisper_output("") == []


class TestParsingVoskOutput:
    def test_word_times_become_the_span(self):
        raw = json.dumps(
            {
                "text": "hello everyone",
                "result": [
                    {"word": "hello", "start": 1.2, "end": 1.6, "conf": 0.9},
                    {"word": "everyone", "start": 1.6, "end": 2.4, "conf": 0.7},
                ],
            }
        )
        utterance = transcribe.parse_vosk_result(raw)[0]
        assert utterance.start == 1.2
        assert utterance.end == 2.4
        assert utterance.text == "hello everyone"
        assert utterance.confidence == 0.8

    def test_an_empty_result_is_skipped(self):
        assert transcribe.parse_vosk_result(json.dumps({"text": ""})) == []
        assert transcribe.parse_vosk_result("not json") == []


# ---------------------------------------------------------------------------
# Transcribing a whole session
# ---------------------------------------------------------------------------


class TestTranscribeSession:
    def test_both_tracks_are_transcribed_and_merged_in_order(
        self, mock_config, session_dir, nothing_installed
    ):
        recorded(session_dir)
        engine = transcribe.choose_engine(mock_config)
        segments, notices = transcribe.transcribe_session(
            engine, session_dir, mock_config
        )
        assert segments
        assert {s.track for s in segments} == {TRACK_ROOM, TRACK_FAR_END}
        assert [s.start for s in segments] == sorted(s.start for s in segments)
        assert not [n for n in notices if "could not" in n]

    def test_a_missing_track_is_skipped_rather_than_failed(
        self, mock_config, session_dir, nothing_installed
    ):
        recorded(session_dir, far_end=0)
        segments, _notices = transcribe.transcribe_session(
            transcribe.MockEngine(mock_config), session_dir, mock_config
        )
        assert segments
        assert {s.track for s in segments} == {TRACK_ROOM}

    def test_no_audio_at_all_is_a_notice_not_an_exception(
        self, mock_config, session_dir
    ):
        segments, notices = transcribe.transcribe_session(
            transcribe.MockEngine(mock_config), session_dir, mock_config
        )
        assert segments == []
        assert any("no audio" in n.lower() for n in notices)

    def test_an_unavailable_engine_returns_its_reason(
        self, config, nothing_installed, session_dir
    ):
        recorded(session_dir)
        segments, notices = transcribe.transcribe_session(
            transcribe.choose_engine(config), session_dir, config
        )
        assert segments == []
        assert notices and "whisper" in notices[0].lower()

    def test_skipping_the_far_end_leaves_the_recording_alone(
        self, mock_config, session_dir, nothing_installed
    ):
        """The captions already have the remote speakers, with their names."""
        recorded(session_dir)
        segments, notices = transcribe.transcribe_session(
            transcribe.MockEngine(mock_config),
            session_dir,
            mock_config,
            skip_far_end=True,
        )
        assert {s.track for s in segments} == {TRACK_ROOM}
        assert any("captions" in n for n in notices)
        assert (session_dir / "farend.wav").exists(), "retention owns that decision"

    def test_an_engine_that_raises_is_survived(
        self, mock_config, session_dir, nothing_installed
    ):
        class Exploding(transcribe.MockEngine):
            def transcribe(self, wav, *, language, timeout):
                raise RuntimeError("the model segfaulted")

        recorded(session_dir, far_end=0)
        segments, notices = transcribe.transcribe_session(
            Exploding(mock_config), session_dir, mock_config
        )
        assert segments == []
        assert any("segfaulted" in n for n in notices)

    def test_an_empty_recording_is_reported(self, mock_config, session_dir):
        audio.write_silence(session_dir / "room.wav", 0.05)
        segments, notices = transcribe.transcribe_session(
            transcribe.MockEngine(mock_config), session_dir, mock_config
        )
        assert segments == []
        assert any("empty" in n for n in notices)

    def test_the_placeholder_engine_does_not_echo_itself(
        self, mock_config, session_dir, nothing_installed
    ):
        """Or development mode would look like the echo suppression is broken."""
        recorded(session_dir)
        segments, _notices = transcribe.transcribe_session(
            transcribe.MockEngine(mock_config), session_dir, mock_config
        )
        rooms = {s.text for s in segments if s.track == TRACK_ROOM}
        far_ends = {s.text for s in segments if s.track == TRACK_FAR_END}
        assert rooms and far_ends
        assert not rooms & far_ends


class TestTheWholePipelineWithoutHardware:
    """Record and transcribe a meeting on a machine with neither."""

    def test_a_mock_recording_transcribes_end_to_end(
        self, mock_config, session_dir, nothing_installed, monkeypatch
    ):
        # The recorder's disk guard is real, and the machine running the suite
        # may genuinely be short of space; this test is not about that.
        monkeypatch.setattr(
            audio.shutil,
            "disk_usage",
            lambda _path: types.SimpleNamespace(total=100, used=60, free=40),
        )
        recorder = audio.Recorder(mock_config, session_dir)
        started, why = recorder.start(room=True, far_end=True)
        assert started, why
        # Stand the clock back rather than sleep: the placeholder is written to
        # the length of the recording, and a meeting of nought seconds has
        # nothing in it to write down.
        recorder._started_at -= 20.0
        capture = recorder.stop()
        assert capture.room_wav and capture.far_end_wav
        assert capture.seconds >= 20.0

        engine = transcribe.choose_engine(mock_config)
        assert engine.name == transcribe.MOCK
        segments, notices = transcribe.transcribe_session(
            engine, session_dir, mock_config
        )
        assert len(segments) >= 4
        assert {s.track for s in segments} == {TRACK_ROOM, TRACK_FAR_END}
        assert all(s.text for s in segments)
        assert all(s.end >= s.start for s in segments)
        assert not [n for n in notices if "could not" in n or "no audio" in n.lower()]


class TestTimeouts:
    """Trap T2: slow is normal, and killing a slow run loses the meeting."""

    def test_short_audio_still_gets_five_minutes(self):
        assert transcribe.timeout_for(3.0) == 300.0

    def test_a_long_meeting_gets_thirty_times_real_time(self):
        assert transcribe.timeout_for(3600.0) == 108000.0

    def test_nonsense_lengths_do_not_produce_a_negative_timeout(self):
        assert transcribe.timeout_for(-5.0) == 300.0


# ---------------------------------------------------------------------------
# Echo suppression — trap T8
# ---------------------------------------------------------------------------


class TestEchoSuppression:
    def test_the_room_hearing_the_speaker_is_dropped(self):
        segments = [
            far(10.0, 13.0, "We have had the same question from two other sites."),
            room(10.2, 13.4, "We have had the same question from two other sites"),
        ]
        kept, dropped = transcribe.drop_echoes(segments)
        assert dropped == 1
        assert [s.track for s in kept] == [TRACK_FAR_END]

    def test_small_recognition_differences_still_count_as_an_echo(self):
        segments = [
            far(4.0, 8.0, "Let me share my screen for a moment."),
            room(4.3, 8.2, "let me share my screen for a moment"),
        ]
        _kept, dropped = transcribe.drop_echoes(segments)
        assert dropped == 1

    def test_a_genuinely_different_room_line_at_the_same_time_is_kept(self):
        segments = [
            far(10.0, 13.0, "We have had the same question from two other sites."),
            room(10.1, 12.9, "Can you put that in the chat for us, please?"),
        ]
        kept, dropped = transcribe.drop_echoes(segments)
        assert dropped == 0
        assert len(kept) == 2

    def test_the_same_sentence_at_a_different_time_is_kept(self):
        """People do repeat each other; that is a conversation, not an echo."""
        segments = [
            far(10.0, 13.0, "We have had the same question from two other sites."),
            room(90.0, 93.0, "We have had the same question from two other sites."),
        ]
        _kept, dropped = transcribe.drop_echoes(segments)
        assert dropped == 0

    def test_short_agreements_are_never_dropped(self):
        """Two people saying “yes” at once is normal and means something."""
        segments = [
            far(5.0, 5.4, "Yes."),
            room(5.1, 5.5, "Yes"),
        ]
        _kept, dropped = transcribe.drop_echoes(segments)
        assert dropped == 0

    def test_a_far_end_line_is_never_dropped(self):
        """The call's own recording is the authoritative copy of remote speech."""
        segments = [
            far(1.0, 4.0, "That matches what we are seeing on our side."),
            far(1.1, 4.1, "That matches what we are seeing on our side."),
        ]
        _kept, dropped = transcribe.drop_echoes(segments)
        assert dropped == 0

    def test_one_far_end_line_can_only_account_for_the_room_lines_it_overlaps(self):
        segments = [
            far(0.0, 3.0, "I will send the summary round after this."),
            room(0.2, 3.1, "I will send the summary round after this."),
            room(40.0, 43.0, "I will send the summary round after this."),
        ]
        kept, dropped = transcribe.drop_echoes(segments)
        assert dropped == 1
        assert [s.start for s in kept] == [0.0, 40.0]

    def test_it_works_on_a_transcript_merged_with_the_meeting_captions(self):
        """What the caller must do when it skipped the far-end track.

        The captions arrive already attributed and on the far-end track, so the
        same comparison run over the merged list removes the room microphone's
        copy of what the speaker played.
        """
        from app.minutes.transcript import SOURCE_ROSTER

        captions = [
            Segment(
                start=12.0,
                end=15.0,
                text="We have had the same question from two other sites.",
                track=TRACK_FAR_END,
                speaker="Priya Nair",
                source=SOURCE_ROSTER,
            )
        ]
        from_the_room = [
            room(12.3, 15.2, "We have had the same question from two other sites."),
            room(16.0, 18.0, "Shall I put that on next week's agenda?"),
        ]
        merged = sorted(captions + from_the_room, key=lambda s: s.start)
        kept, dropped = transcribe.drop_echoes(merged)
        assert dropped == 1
        assert [s.speaker for s in kept] == ["Priya Nair", ""]
        assert kept[1].text == "Shall I put that on next week's agenda?"

    def test_nothing_to_compare_against_changes_nothing(self):
        segments = [room(0.0, 2.0, "Only the room was recorded today.")]
        kept, dropped = transcribe.drop_echoes(segments)
        assert dropped == 0
        assert kept == segments

    def test_an_empty_transcript_is_handled(self):
        assert transcribe.drop_echoes([]) == ([], 0)

    def test_the_session_reports_what_it_dropped(
        self, mock_config, session_dir, monkeypatch, nothing_installed
    ):
        """End to end: an engine that hears the same words on both tracks."""

        class Echoing(transcribe.MockEngine):
            def transcribe(self, wav, *, language, timeout):
                return [
                    transcribe.Utterance(
                        1.0, 5.0, "We have had the same question from two other sites."
                    )
                ], ""

        recorded(session_dir)
        segments, notices = transcribe.transcribe_session(
            Echoing(mock_config), session_dir, mock_config
        )
        assert [s.track for s in segments] == [TRACK_FAR_END]
        assert any("picked up from the speaker" in n for n in notices)


class TestNormalising:
    def test_case_and_punctuation_do_not_matter(self):
        assert transcribe._normalise("Hello, World!") == transcribe._normalise("hello world")

    def test_spacing_does_not_matter(self):
        assert transcribe._normalise("  two   words  ") == "two words"
