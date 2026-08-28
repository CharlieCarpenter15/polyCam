"""Who said what, given several sources that each know part of the answer."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.minutes.attribute import attribute, speaking_spans
from app.minutes.transcript import (
    SOURCE_FACE,
    SOURCE_ROSTER,
    SOURCE_VOICE,
    TRACK_FAR_END,
    TRACK_ROOM,
    Segment,
    SessionMeta,
    Transcript,
)


@dataclass
class FakeSample:
    at: float
    participants: list[str] = field(default_factory=list)
    speaking: list[str] = field(default_factory=list)


def make(segments):
    return Transcript(meta=SessionMeta(session_id="20260901-090000-abcd1234"), segments=segments)


class TestSpeakingSpans:
    def test_consecutive_samples_become_one_span(self):
        samples = [
            FakeSample(0.0, speaking=["Priya"]),
            FakeSample(2.0, speaking=["Priya"]),
            FakeSample(4.0, speaking=["Priya"]),
            FakeSample(6.0, speaking=["Sam"]),
        ]
        spans = speaking_spans(samples)
        by_name = {name: (start, end) for start, end, name in spans}
        assert set(by_name) == {"Priya", "Sam"}
        start, end = by_name["Priya"]
        assert start <= 0.0 and end >= 4.0
        assert len(spans) == 2, "one span each, not one per sample"

    def test_a_speaker_who_returns_gets_two_spans(self):
        samples = [
            FakeSample(0.0, speaking=["Priya"]),
            FakeSample(2.0, speaking=["Sam"]),
            FakeSample(4.0, speaking=["Priya"]),
        ]
        spans = speaking_spans(samples)
        assert sum(1 for _, _, name in spans if name == "Priya") == 2

    def test_silence_produces_nothing(self):
        assert speaking_spans([FakeSample(0.0), FakeSample(2.0)]) == []

    def test_no_samples_is_not_an_error(self):
        assert speaking_spans([]) == []


class TestRemoteAttribution:
    def test_the_meeting_window_names_the_far_end(self):
        written = make([Segment(0.0, 5.0, "Can everyone hear me?", TRACK_FAR_END)])
        attribute(
            written,
            roster_samples=[
                FakeSample(1.0, participants=["Priya", "Sam"], speaking=["Priya"]),
                FakeSample(3.0, participants=["Priya", "Sam"], speaking=["Priya"]),
            ],
        )
        assert written.segments[0].speaker == "Priya"
        assert written.segments[0].source == SOURCE_ROSTER

    def test_a_brief_overlap_does_not_rename_a_long_turn(self):
        """A one-word interjection must not take over somebody else's sentence."""
        written = make([Segment(0.0, 20.0, "A long explanation…", TRACK_FAR_END)])
        attribute(written, roster_samples=[FakeSample(19.5, speaking=["Sam"])])
        assert written.segments[0].speaker == ""

    def test_room_speech_is_never_given_a_remote_name(self):
        written = make([Segment(0.0, 5.0, "Over here", TRACK_ROOM)])
        attribute(written, roster_samples=[FakeSample(2.0, speaking=["Priya"])])
        assert written.segments[0].speaker == ""
        assert written.segments[0].label() == "Room speaker"

    @pytest.mark.parametrize("junk", ["You", "Meeting Room", "iPhone", "Unknown", "Guest"])
    def test_names_that_are_not_people_are_refused(self, junk):
        written = make([Segment(0.0, 5.0, "Hello", TRACK_FAR_END)])
        attribute(written, roster_samples=[FakeSample(2.0, speaking=[junk])])
        assert written.segments[0].speaker == ""

    def test_a_decorated_name_is_tidied(self):
        written = make([Segment(0.0, 5.0, "Hello", TRACK_FAR_END)])
        attribute(written, roster_samples=[FakeSample(2.0, speaking=["Priya Nair (Guest)"])])
        assert written.segments[0].speaker == "Priya Nair"


class TestRoomAttribution:
    def test_a_voice_match_labels_a_room_segment(self):
        written = make([Segment(0.0, 4.0, "Morning", TRACK_ROOM)])
        attribute(written, voice_labels={0: ("Charlie", "abc123", 0.71)})
        assert written.segments[0].speaker == "Charlie"
        assert written.segments[0].source == SOURCE_VOICE
        assert written.segments[0].confidence == pytest.approx(0.71)

    def test_one_person_in_the_room_owns_every_room_line(self):
        written = make(
            [
                Segment(0.0, 4.0, "Morning", TRACK_ROOM),
                Segment(4.0, 8.0, "Shall we start?", TRACK_ROOM),
            ]
        )
        attribute(written, room_people=[{"name": "Charlie", "person_id": "abc", "score": 0.8}])
        assert [s.speaker for s in written.segments] == ["Charlie", "Charlie"]
        assert written.segments[0].source == SOURCE_FACE

    def test_two_people_in_the_room_means_no_guessing(self):
        written = make([Segment(0.0, 4.0, "Morning", TRACK_ROOM)])
        attribute(
            written,
            room_people=[{"name": "Charlie", "person_id": "a"}, {"name": "Sam", "person_id": "b"}],
        )
        assert written.segments[0].speaker == ""

    def test_a_voice_match_beats_the_only_person_in_the_room(self):
        written = make([Segment(0.0, 4.0, "Morning", TRACK_ROOM)])
        attribute(
            written,
            voice_labels={0: ("Sam", "b", 0.8)},
            room_people=[{"name": "Charlie", "person_id": "a"}],
        )
        assert written.segments[0].speaker == "Sam"


class TestParticipants:
    def test_everyone_seen_and_listed_is_collected(self):
        written = make([Segment(0.0, 4.0, "Morning", TRACK_ROOM)])
        attribute(
            written,
            roster_samples=[FakeSample(1.0, participants=["Priya Nair", "Sam Lee"])],
            room_people=[{"name": "Charlie", "person_id": "a", "email": "c@x.com"}],
        )
        by_name = {p.name: p for p in written.participants}
        assert set(by_name) == {"Charlie", "Priya Nair", "Sam Lee"}
        assert by_name["Charlie"].where == "room"
        assert by_name["Priya Nair"].where == "remote"

    def test_nobody_appears_twice(self):
        written = make([])
        attribute(
            written,
            roster_samples=[
                FakeSample(1.0, participants=["Priya"]),
                FakeSample(3.0, participants=["Priya", "priya"]),
            ],
        )
        assert len(written.participants) == 1

    def test_an_invited_address_is_matched_to_a_detected_name(self):
        written = make([])
        attribute(
            written,
            roster_samples=[FakeSample(1.0, participants=["Priya Nair"])],
            invited=["priya.nair@example.com", "someone.else@example.com"],
        )
        assert written.participants[0].email == "priya.nair@example.com"

    def test_a_name_that_does_not_appear_in_an_address_stays_unmatched(self):
        written = make([])
        attribute(
            written,
            roster_samples=[FakeSample(1.0, participants=["Priya Nair"])],
            invited=["pn@example.com"],
        )
        assert written.participants[0].email == ""

    def test_one_address_is_not_given_to_two_people(self):
        written = make([])
        attribute(
            written,
            roster_samples=[FakeSample(1.0, participants=["Sam Lee", "Sam Leeson"])],
            invited=["sam.lee@example.com"],
        )
        emails = [p.email for p in written.participants if p.email]
        assert len(emails) == len(set(emails))

    def test_recipients_merge_detected_people_and_the_invitation(self):
        written = make([])
        written.meta.invited = ["invited@example.com"]
        attribute(
            written,
            room_people=[{"name": "Charlie", "person_id": "a", "email": "c@x.com"}],
            invited=["invited@example.com"],
        )
        assert set(written.recipients()) == {"c@x.com", "invited@example.com"}


class TestRobustness:
    def test_nothing_at_all_is_not_an_error(self):
        written = make([])
        attribute(written)
        assert written.segments == [] and written.participants == []

    def test_a_malformed_sample_is_ignored(self):
        written = make([Segment(0.0, 4.0, "Hi", TRACK_FAR_END)])
        attribute(written, roster_samples=[FakeSample("not a number", speaking=["Priya"])])
        assert written.segments[0].speaker in ("", "Priya")
