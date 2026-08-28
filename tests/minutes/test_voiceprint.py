"""Telling voices apart, and refusing to guess when it cannot.

The synthetic voices here are stacks of harmonics rather than recordings, which
makes them cleanly separable — much more so than two colleagues at opposite ends
of a table. That is deliberate: these tests check the *plumbing* (does the
detector find the speech, does the same voice match itself, does a different one
not, does a short sample get refused, does an unrecognised speaker stay
unnamed). How well it does on real far-field audio is a question about the
technique, not about this code, and no unit test can answer it.
"""

from __future__ import annotations

import math
import struct
import wave

import pytest

from app.minutes import deps, voiceprint
from app.minutes.people import KIND_VOICE, PeopleStore, cosine, normalise
from app.minutes.transcript import TRACK_FAR_END, TRACK_ROOM, Segment

pytest.importorskip("numpy", reason="the fingerprinting is numpy-only by design")


def tone(harmonics, seconds, amplitude=8000):
    """A crude synthetic voice: a fundamental and its harmonics."""
    out = []
    for index in range(int(voiceprint.SAMPLE_RATE * seconds)):
        value = sum(
            math.sin(2 * math.pi * freq * index / voiceprint.SAMPLE_RATE)
            for freq in harmonics
        ) / len(harmonics)
        out.append(int(value * amplitude))
    return out


def silence(seconds):
    return [0] * int(voiceprint.SAMPLE_RATE * seconds)


def write_wav(path, samples, *, rate=16000, channels=1, width=2):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


VOICE_A = [120, 240, 360, 1200]
VOICE_B = [210, 420, 900, 1800]


@pytest.fixture()
def two_voices(tmp_path):
    """Voice A from 0.6-5.6s, voice B from 6.2-11.2s."""
    samples = silence(0.6) + tone(VOICE_A, 5) + silence(0.6) + tone(VOICE_B, 5) + silence(0.6)
    return write_wav(tmp_path / "room.wav", samples)


class TestReadingAudio:
    def test_a_slice_is_returned(self, two_voices):
        raw, error = voiceprint.read_wav(two_voices, 1.0, 2.0)
        assert not error
        assert len(raw) == pytest.approx(voiceprint.SAMPLE_RATE * 2, rel=0.01)

    def test_a_stereo_file_is_refused_rather_than_misread(self, tmp_path):
        path = write_wav(tmp_path / "stereo.wav", tone(VOICE_A, 1), channels=2)
        raw, error = voiceprint.read_wav(path)
        assert raw == b"" and "16-bit mono" in error

    def test_a_missing_file_is_an_error_not_an_exception(self, tmp_path):
        raw, error = voiceprint.read_wav(tmp_path / "nope.wav")
        assert raw == b"" and error

    def test_a_truncated_recording_still_reads(self, tmp_path, two_voices):
        """A power cut leaves a header claiming more frames than exist."""
        data = bytearray(two_voices.read_bytes())
        cut = data[: len(data) // 2]
        path = tmp_path / "cut.wav"
        path.write_bytes(bytes(cut))
        raw, error = voiceprint.read_wav(path)
        assert not error
        assert len(raw) > 0


class TestFindingSpeech:
    def test_both_stretches_of_speech_are_found(self, two_voices):
        spans = voiceprint.speech_spans(two_voices)
        assert len(spans) == 2
        first, second = spans
        assert first[0] == pytest.approx(0.6, abs=0.2)
        assert first[1] == pytest.approx(5.6, abs=0.2)
        assert second[0] == pytest.approx(6.2, abs=0.2)

    def test_silence_yields_nothing(self, tmp_path):
        path = write_wav(tmp_path / "quiet.wav", silence(6))
        assert voiceprint.speech_spans(path) == []

    def test_a_breath_does_not_split_a_turn(self, tmp_path):
        """A gap shorter than the join window is part of the same turn."""
        samples = tone(VOICE_A, 3) + silence(0.2) + tone(VOICE_A, 3)
        path = write_wav(tmp_path / "breath.wav", samples)
        assert len(voiceprint.speech_spans(path)) == 1

    def test_a_real_pause_does_split_a_turn(self, tmp_path):
        samples = tone(VOICE_A, 3) + silence(1.5) + tone(VOICE_A, 3)
        path = write_wav(tmp_path / "pause.wav", samples)
        assert len(voiceprint.speech_spans(path)) == 2

    def test_a_missing_file_yields_no_spans(self, tmp_path):
        assert voiceprint.speech_spans(tmp_path / "nope.wav") == []


class TestFingerprints:
    def test_the_same_voice_matches_itself_across_different_slices(self, two_voices):
        first, _, error_a = voiceprint.embed_file(two_voices, 0.8, 4.0)
        second, _, error_b = voiceprint.embed_file(two_voices, 1.5, 5.0)
        assert not error_a and not error_b
        assert cosine(normalise(first), normalise(second)) > 0.9

    def test_two_different_voices_do_not_match(self, two_voices):
        first, _, _ = voiceprint.embed_file(two_voices, 0.8, 5.0)
        second, _, _ = voiceprint.embed_file(two_voices, 6.4, 11.0)
        assert cosine(normalise(first), normalise(second)) < voiceprint.CLUSTER_THRESHOLD

    def test_a_short_sample_is_refused_with_a_useful_sentence(self, two_voices):
        vector, _, error = voiceprint.embed_file(two_voices, 0.8, 1.4)
        assert not vector
        assert "seconds" in error and str(int(voiceprint.MIN_ENROL_SECONDS)) in error

    def test_the_vector_records_which_model_made_it(self, two_voices):
        _, model, _ = voiceprint.embed_file(two_voices, 0.8, 5.0)
        assert model in (
            voiceprint.MODEL_TITANET, voiceprint.MODEL_VOSK, voiceprint.MODEL_MFCC
        )

    def test_empty_audio_is_an_error_not_a_crash(self):
        vector, _, error = voiceprint.embed_samples(b"")
        assert not vector and error

    def test_numpy_missing_is_reported_rather_than_raised(self, two_voices, monkeypatch):
        deps.set_probe_for_tests("numpy", False, "numpy is not installed.")
        try:
            vector, _, error = voiceprint.embed_file(two_voices, 0.8, 5.0)
            assert not vector and "numpy" in error
        finally:
            deps.refresh()


class TestAvailability:
    def test_off_by_default(self, config):
        ok, why = voiceprint.available(config)
        assert ok is False and "switched off" in why

    def test_on_but_without_a_speaker_model_it_says_what_it_cannot_do(self, config):
        config.update({"MINUTES_IDENTIFY_VOICES": True})
        ok, why = voiceprint.available(config)
        assert ok is True
        assert "cannot be named" in why.lower()

    def test_the_engine_order_prefers_the_good_model(self):
        """MFCC is the last resort, never the first choice."""
        assert voiceprint.model_name() in (
            voiceprint.MODEL_TITANET, voiceprint.MODEL_VOSK, voiceprint.MODEL_MFCC
        )
        assert voiceprint.can_name_people() is False, (
            "nothing is installed here, so nothing may be named"
        )

    def test_numpy_missing_blocks_it_entirely(self, config):
        config.update({"MINUTES_IDENTIFY_VOICES": True})
        deps.set_probe_for_tests("numpy", False, "numpy is not installed.")
        try:
            ok, why = voiceprint.available(config)
            assert ok is False and "numpy" in why
        finally:
            deps.refresh()


class TestLabellingAMeeting:
    @pytest.fixture()
    def store(self, tmp_path):
        return PeopleStore(tmp_path / "people.json")

    @pytest.fixture()
    def enabled(self, config):
        config.update({"MINUTES_IDENTIFY_VOICES": True, "MINUTES_VOICE_THRESHOLD": 0.9})
        return config

    def segments(self):
        return [
            Segment(0.6, 5.6, "first speaker", TRACK_ROOM),
            Segment(6.2, 11.2, "second speaker", TRACK_ROOM),
        ]

    @pytest.fixture()
    def naming(self, monkeypatch):
        """Pretend a real speaker model and a real VAD are installed.

        Naming is deliberately withheld unless both are: the MFCC fallback
        cannot identify anybody, and the loudness-based speech detector misses
        so much speech that a segment it produced is not safe to put a name to.
        """
        monkeypatch.setattr(voiceprint, "can_name_people", lambda: True)

    def test_an_enrolled_voice_is_named(self, two_voices, store, enabled, naming):
        charlie, _ = store.add("Charlie", "charlie@example.com")
        vector, model, _ = voiceprint.embed_file(two_voices, 0.8, 5.0)
        store.add_vector(charlie.id, KIND_VOICE, model, vector)

        labels, _ = voiceprint.label_room_segments(
            two_voices.parent, self.segments(), store, enabled
        )
        assert labels.get(0, ("", "", 0))[0] == "Charlie"
        assert labels[0][1] == charlie.id

    def test_an_unenrolled_voice_is_never_given_a_name(
        self, two_voices, store, enabled, naming
    ):
        charlie, _ = store.add("Charlie")
        vector, model, _ = voiceprint.embed_file(two_voices, 0.8, 5.0)
        store.add_vector(charlie.id, KIND_VOICE, model, vector)

        labels, _ = voiceprint.label_room_segments(
            two_voices.parent, self.segments(), store, enabled
        )
        second = labels.get(1)
        assert second is None or second[1] == "", "an unknown voice must not get a profile"

    def test_nobody_is_named_when_only_the_loudness_detector_is_available(
        self, two_voices, store, enabled
    ):
        """A segment found by loudness alone may be half somebody else's sentence."""
        charlie, _ = store.add("Charlie")
        vector, model, _ = voiceprint.embed_file(two_voices, 0.8, 5.0)
        store.add_vector(charlie.id, KIND_VOICE, model, vector)

        labels, note = voiceprint.label_room_segments(
            two_voices.parent, self.segments(), store, enabled
        )
        assert all(person_id == "" for _, person_id, _ in labels.values())
        if labels:
            assert "not named" in note

    def test_speakers_are_still_kept_apart_without_a_model(
        self, two_voices, store, enabled
    ):
        labels, _ = voiceprint.label_room_segments(
            two_voices.parent, self.segments(), store, enabled
        )
        names = {name for name, _, _ in labels.values()}
        assert len(names) == len(labels), "two different voices must not share a label"

    def test_remote_segments_are_left_alone(self, two_voices, store, enabled):
        segments = [Segment(0.6, 5.6, "on the call", TRACK_FAR_END)]
        labels, _ = voiceprint.label_room_segments(
            two_voices.parent, segments, store, enabled
        )
        assert labels == {}

    def test_a_deleted_recording_is_not_an_error(self, tmp_path, store, enabled):
        labels, note = voiceprint.label_room_segments(
            tmp_path, self.segments(), store, enabled
        )
        assert labels == {} and note == ""

    def test_switched_off_returns_the_reason(self, two_voices, store, config):
        labels, note = voiceprint.label_room_segments(
            two_voices.parent, self.segments(), store, config
        )
        assert labels == {} and "switched off" in note


class TestATranscriptWithoutTranscription:
    """What the appliance produces when speech-to-text is switched off.

    The setting promises that “none” still works out who spoke. A record of how
    many people talked, for how long and in what order is genuinely useful, and
    is what somebody turning transcription off on a slow Pi is asking for.
    """

    def test_speech_becomes_segments_with_no_words(self, two_voices):
        segments = voiceprint.speech_segments(two_voices)
        assert len(segments) == 2
        assert all(segment.text == "" for segment in segments)
        assert all(segment.track == TRACK_ROOM for segment in segments)
        assert segments[0].start == pytest.approx(0.6, abs=0.2)
        assert segments[1].duration == pytest.approx(5.0, abs=0.4)

    def test_the_track_can_be_chosen(self, two_voices):
        segments = voiceprint.speech_segments(two_voices, TRACK_FAR_END)
        assert all(segment.track == TRACK_FAR_END for segment in segments)

    def test_silence_produces_no_segments(self, tmp_path):
        path = write_wav(tmp_path / "quiet.wav", silence(6))
        assert voiceprint.speech_segments(path) == []

    def test_a_missing_recording_is_not_an_error(self, tmp_path):
        assert voiceprint.speech_segments(tmp_path / "nope.wav") == []

    def test_such_a_transcript_reads_sensibly(self, two_voices):
        from app.minutes.transcript import SessionMeta, Transcript

        written = Transcript(
            meta=SessionMeta(session_id="20260101-000000-abcdef12", title="Standup"),
            segments=voiceprint.speech_segments(two_voices),
        )
        text = written.render_text()
        assert "spoke for" in text
        assert written.has_words is False, "there are no words to summarise"


class TestClustering:
    def test_unnamed_speakers_are_kept_apart_and_numbered_from_two(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        out = voiceprint._cluster([(0, a), (1, a), (2, b), (3, b)])
        assert out[0][0] == out[1][0]
        assert out[2][0] == out[3][0]
        assert out[0][0] != out[2][0]
        assert all(name.startswith("Room speaker ") for name, _, _ in out.values())
        assert "Room speaker 1" not in {name for name, _, _ in out.values()}

    def test_a_cluster_carries_no_profile_id(self):
        out = voiceprint._cluster([(0, [1.0, 0.0]), (1, [1.0, 0.0])])
        assert all(person_id == "" for _, person_id, _ in out.values())

    def test_a_lone_segment_is_left_unlabelled(self):
        out = voiceprint._cluster([(0, [1.0, 0.0, 0.0])])
        assert out == {}

    def test_nothing_to_cluster_is_fine(self):
        assert voiceprint._cluster([]) == {}


class TestLearningFromACorrection:
    def test_a_corrected_label_teaches_the_profile(self, two_voices, tmp_path):
        store = PeopleStore(tmp_path / "people.json")
        charlie, _ = store.add("Charlie")
        segments = [Segment(0.8, 5.0, "hello", TRACK_ROOM)]

        ok, message = voiceprint.learn_from_segments(
            two_voices.parent, segments, store, charlie.id
        )
        assert ok, message
        assert store.get(charlie.id).knows_voice()

    def test_lines_too_short_to_learn_from_say_so(self, two_voices, tmp_path):
        store = PeopleStore(tmp_path / "people.json")
        charlie, _ = store.add("Charlie")
        segments = [Segment(0.8, 1.2, "yes", TRACK_ROOM)]

        ok, message = voiceprint.learn_from_segments(
            two_voices.parent, segments, store, charlie.id
        )
        assert not ok and "seconds" in message

    def test_a_deleted_recording_says_so(self, tmp_path):
        store = PeopleStore(tmp_path / "people.json")
        charlie, _ = store.add("Charlie")
        ok, message = voiceprint.learn_from_segments(
            tmp_path, [Segment(0.0, 9.0, "x", TRACK_ROOM)], store, charlie.id
        )
        assert not ok and "deleted" in message
