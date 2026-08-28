"""Deciding who said each line, from several sources that each know a bit.

Nothing here listens to audio or looks at a camera. It takes what the other
modules found out and reconciles it, which is a separate job with its own rules
and is much easier to test on its own.

Four sources, in descending order of how much they deserve to be believed:

1. **Which track the speech arrived on.** The room microphone and the speaker's
   own output are recorded separately, so "was this person in the room or on the
   call" is not a guess at all — it is a fact about which file the audio is in.
   Every other source can be wrong; this one cannot, so it is decided first and
   never overridden.
2. **The meeting window.** Teams, Meet and Zoom all know exactly who is on the
   call and which of them is talking, and they draw it on screen. Reading it
   back is far more reliable than inferring it from audio, so a name from the
   roster beats a name from a voice fingerprint.
3. **The camera.** Who was seen in the room shortly before the meeting started.
   It cannot tell you which of them is speaking, but when it saw exactly one
   person it has effectively told you.
4. **Voice fingerprints.** The weakest source, because a far-field microphone in
   a hard-surfaced room is the worst case for the technique. Used to label a
   speaker only when nothing better is available.

When two sources disagree, the higher-ranked one wins — that is what
``SOURCE_RANK`` in ``transcript.py`` is for — and the transcript records which
source won, so a person reading it can see the difference between "the meeting
app said this was Priya" and "a voice sounded 0.64 like Priya".
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from .transcript import (
    SOURCE_FACE,
    SOURCE_RANK,
    SOURCE_ROSTER,
    SOURCE_VOICE,
    Participant,
    Segment,
    Transcript,
)

#: A roster sample taken this long before or after a segment still counts as
#: describing it. The sampler runs every couple of seconds and a speaker
#: highlight lags the voice slightly, so a little slack recovers turns that
#: would otherwise fall between two samples.
SAMPLE_SLACK_SECONDS = 1.5

#: A remote segment must overlap a speaker's span by at least this fraction of
#: its own length before the name is applied. Without it, a one-word
#: interjection during somebody else's sentence renames the whole sentence.
MIN_OVERLAP_FRACTION = 0.34

#: Names the meeting apps produce that are not people. Attributing a line to
#: "Meeting Room" or an unnamed phone caller is worse than leaving it unlabelled,
#: because it reads like a fact.
_NOT_A_PERSON = re.compile(
    r"^(you|unknown|guest|participant|caller|meeting room|conference room|"
    r"room system|iphone|ipad|android|unknown user|anonymous)$",
    re.IGNORECASE,
)


def attribute(
    written: Transcript,
    *,
    roster_samples: Sequence[Any] = (),
    voice_labels: Mapping[int, tuple[str, str, float]] | None = None,
    room_people: Sequence[Mapping[str, Any]] = (),
    invited: Sequence[str] = (),
) -> None:
    """Label ``written``'s segments and fill in its participant list, in place."""
    voice_labels = voice_labels or {}
    spans = speaking_spans(roster_samples)

    _label_remote(written, spans)
    _label_room(written, voice_labels, room_people)
    written.participants = _participants(written, roster_samples, room_people, invited)
    _fill_participant_emails(written, invited)


# ---------------------------------------------------------------------------
# The meeting window's active speaker
# ---------------------------------------------------------------------------


def speaking_spans(samples: Sequence[Any]) -> list[tuple[float, float, str]]:
    """Turn "who was speaking at time T" samples into ``(start, end, name)`` spans.

    The sampler polls; it does not get told when somebody starts or stops. Two
    consecutive samples naming the same person are therefore one span, and a
    gap between samples is closed rather than left as a hole, because a poll
    that happened to land between two words is not the speaker stopping.
    """
    open_spans: dict[str, float] = {}
    last_seen: dict[str, float] = {}
    out: list[tuple[float, float, str]] = []

    for sample in sorted(samples, key=lambda s: _at(s)):
        at = _at(s=sample)
        speaking = {
            _clean(name) for name in getattr(sample, "speaking", []) or [] if _clean(name)
        }
        # Anybody who has stopped appearing closes their span.
        for name in list(open_spans):
            if name not in speaking:
                out.append((open_spans.pop(name), last_seen.get(name, at) + SAMPLE_SLACK_SECONDS, name))
        for name in speaking:
            if name not in open_spans:
                open_spans[name] = max(0.0, at - SAMPLE_SLACK_SECONDS)
            last_seen[name] = at

    final = max((_at(s) for s in samples), default=0.0)
    for name, start in open_spans.items():
        out.append((start, last_seen.get(name, final) + SAMPLE_SLACK_SECONDS, name))
    return sorted(out)


def _at(s: Any) -> float:
    try:
        return float(getattr(s, "at", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _label_remote(written: Transcript, spans: Sequence[tuple[float, float, str]]) -> None:
    """Name each far-end segment after whoever the meeting window said was talking."""
    if not spans:
        return
    for segment in written.segments:
        if not segment.is_remote:
            continue
        name, overlap = _best_span(segment, spans)
        if not name:
            continue
        length = max(0.25, segment.duration)
        if overlap / length < MIN_OVERLAP_FRACTION:
            continue
        _apply(segment, name, "", SOURCE_ROSTER, min(1.0, overlap / length))


def _best_span(
    segment: Segment, spans: Sequence[tuple[float, float, str]]
) -> tuple[str, float]:
    best_name = ""
    best_overlap = 0.0
    for start, end, name in spans:
        overlap = min(segment.end, end) - max(segment.start, start)
        if overlap > best_overlap:
            best_overlap = overlap
            best_name = name
    if _is_not_a_person(best_name):
        return "", 0.0
    return best_name, max(0.0, best_overlap)


# ---------------------------------------------------------------------------
# The room
# ---------------------------------------------------------------------------


def _label_room(
    written: Transcript,
    voice_labels: Mapping[int, tuple[str, str, float]],
    room_people: Sequence[Mapping[str, Any]],
) -> None:
    """Name the in-room segments from voice profiles, or from an empty room.

    The second half of that is the useful trick. When the camera saw exactly one
    person in the room and nothing else has claimed the segment, every in-room
    voice is that person's — not because we recognised the voice, but because
    there was nobody else there to be. It is recorded as a face-derived label so
    that a reader can see it was reasoned rather than heard.
    """
    for index, segment in enumerate(written.segments):
        if segment.is_remote:
            continue
        label = voice_labels.get(index)
        if label:
            name, person_id, score = label
            if name and not _is_not_a_person(name):
                _apply(segment, name, person_id, SOURCE_VOICE, score)

    named = [p for p in room_people if _clean(p.get("name"))]
    if len(named) != 1:
        return
    only = named[0]
    for segment in written.segments:
        if segment.is_remote or segment.speaker:
            continue
        _apply(
            segment,
            _clean(only.get("name")),
            _clean(only.get("person_id")),
            SOURCE_FACE,
            _score(only.get("score")),
        )


def _apply(segment: Segment, name: str, person_id: str, source: str, confidence: float) -> None:
    """Set a speaker, unless something more trustworthy already did."""
    if SOURCE_RANK.get(source, 0) < SOURCE_RANK.get(segment.source, 0):
        return
    segment.speaker = name
    segment.person_id = person_id or segment.person_id
    segment.source = source
    segment.confidence = round(max(0.0, min(1.0, confidence)), 3)


# ---------------------------------------------------------------------------
# Who was there
# ---------------------------------------------------------------------------


def _participants(
    written: Transcript,
    roster_samples: Sequence[Any],
    room_people: Sequence[Mapping[str, Any]],
    invited: Sequence[str],
) -> list[Participant]:
    """Everybody the appliance believes was in the meeting, best evidence first."""
    out: list[Participant] = []
    seen: set[str] = set()

    def add(name: str, *, where: str, source: str, person_id: str = "", email: str = "") -> None:
        clean = _clean(name)
        if not clean or _is_not_a_person(clean):
            return
        key = clean.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(
            Participant(
                name=clean, email=_clean(email), person_id=_clean(person_id),
                where=where, source=source,
            )
        )

    # Seen by the camera: the strongest evidence that somebody was in the room.
    for person in room_people:
        add(
            _clean(person.get("name")),
            where="room",
            source="face",
            person_id=_clean(person.get("person_id")),
            email=_clean(person.get("email")),
        )

    # Everybody the meeting window ever listed.
    for sample in roster_samples:
        for name in getattr(sample, "participants", []) or []:
            add(name, where="remote", source="roster")

    # Anybody a voice matched who is not already accounted for.
    for segment in written.segments:
        if segment.speaker and segment.source == SOURCE_VOICE:
            add(segment.speaker, where="room", source="voice", person_id=segment.person_id)

    return out


def _fill_participant_emails(written: Transcript, invited: Sequence[str]) -> None:
    """Give a detected participant an address when the invitation implies one.

    Matching a display name against an invited address is guesswork, so it is
    deliberately narrow: the whole name has to appear in the local part, in
    order, and the address must not already belong to somebody else on the
    list. "Priya Nair" therefore finds ``priya.nair@`` and ``pnair@`` finds
    nobody, which is the right way round to be wrong.
    """
    addresses = [a for a in (_clean(x) for x in invited) if "@" in a]
    if not addresses:
        return
    taken = {p.email.lower() for p in written.participants if p.email}
    for person in written.participants:
        if person.email:
            continue
        parts = [p for p in re.split(r"[^a-z0-9]+", person.name.lower()) if p]
        if not parts:
            continue
        for address in addresses:
            if address.lower() in taken:
                continue
            local = address.split("@", 1)[0].lower()
            simple = re.sub(r"[^a-z0-9]", "", local)
            if _ordered_contains(simple, parts):
                person.email = address
                taken.add(address.lower())
                break


def _ordered_contains(haystack: str, needles: Iterable[str]) -> bool:
    position = 0
    for needle in needles:
        found = haystack.find(needle, position)
        if found == -1:
            return False
        position = found + len(needle)
    return True


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    # Meeting apps decorate names: "Priya Nair (Guest)", "Charlie — presenting".
    text = re.sub(r"\s*\((guest|host|organiser|organizer|presenter|you)\)\s*$", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text)[:120]


def _is_not_a_person(name: str) -> bool:
    return not name or bool(_NOT_A_PERSON.match(name.strip()))


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
