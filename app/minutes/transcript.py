"""What a recorded meeting looks like once it has been written down.

Four small records, and the rules for turning them into text:

``SessionMeta``
    Written the moment recording starts, before anything has been captured.
    That order matters — a power cut halfway through a meeting leaves a
    directory with a meta file in it, which is how the appliance knows on the
    next boot that there is an unfinished recording to pick up.

``Segment``
    One stretch of speech: when it happened, which track it came from, what was
    said, and who the appliance believes said it. ``source`` records *how* that
    belief was reached, because "the meeting app said so" and "a voice profile
    matched at 0.71" deserve different amounts of trust, and a person reading
    the transcript should be able to tell them apart.

``Participant``
    Somebody the appliance thinks was in the meeting, where they were, and how
    it found out. This is the list the summary email is addressed to.

``Transcript``
    The three of the above, together, plus the rendering used for the Claude
    prompt and the email.

Times are seconds from the start of the recording, not wall clocks: the two
audio tracks and the roster samples all have to line up with each other, and a
single origin is the only way that survives a clock adjustment mid-meeting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

#: Where a voice came from. The room microphone hears the people physically
#: present; the speaker's monitor hears everyone dialled in. Keeping the two
#: apart is the single most reliable fact this feature has.
TRACK_ROOM = "room"
TRACK_FAR_END = "far-end"
TRACKS = (TRACK_ROOM, TRACK_FAR_END)

#: How a speaker was identified, worst to best.
SOURCE_UNKNOWN = ""
SOURCE_VOICE = "voice"
SOURCE_FACE = "face"
SOURCE_ROSTER = "roster"
SOURCE_MANUAL = "manual"

#: Trust order used when two methods disagree. A person who corrected the
#: transcript by hand always wins; the meeting app naming its active speaker
#: beats a voice fingerprint, which is a guess.
SOURCE_RANK: dict[str, int] = {
    SOURCE_UNKNOWN: 0,
    SOURCE_VOICE: 1,
    SOURCE_FACE: 2,
    SOURCE_ROSTER: 3,
    SOURCE_MANUAL: 4,
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _s(value: Any) -> str:
    return str(value or "").strip()


@dataclass
class Segment:
    """One stretch of speech and who the appliance believes said it."""

    start: float
    end: float
    text: str
    track: str = TRACK_ROOM
    speaker: str = ""
    person_id: str = ""
    source: str = SOURCE_UNKNOWN
    confidence: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def is_remote(self) -> bool:
        return self.track == TRACK_FAR_END

    def label(self) -> str:
        """What to print in front of the line."""
        if self.speaker:
            return self.speaker
        return "Remote speaker" if self.is_remote else "Room speaker"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: Any) -> "Segment | None":
        if not isinstance(row, dict):
            return None
        text = _s(row.get("text"))
        if not text:
            return None
        track = _s(row.get("track")) or TRACK_ROOM
        return cls(
            start=_f(row.get("start")),
            end=_f(row.get("end")),
            text=text,
            track=track if track in TRACKS else TRACK_ROOM,
            speaker=_s(row.get("speaker")),
            person_id=_s(row.get("person_id")),
            source=_s(row.get("source")),
            confidence=_f(row.get("confidence")),
        )


@dataclass
class Participant:
    """Somebody believed to have been in the meeting."""

    name: str
    email: str = ""
    person_id: str = ""
    #: "room" for physically present, "remote" for dialled in, "" for unknown.
    where: str = ""
    #: "calendar", "roster", "face", "voice" or "manual".
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: Any) -> "Participant | None":
        if not isinstance(row, dict):
            return None
        name = _s(row.get("name"))
        email = _s(row.get("email"))
        if not name and not email:
            return None
        return cls(
            name=name or email,
            email=email,
            person_id=_s(row.get("person_id")),
            where=_s(row.get("where")),
            source=_s(row.get("source")),
        )


@dataclass
class SessionMeta:
    """Written when recording starts, so an interrupted meeting is recoverable."""

    session_id: str
    started_at: str = ""
    ended_at: str = ""
    meeting_id: str = ""
    title: str = ""
    provider: str = ""
    room: str = ""
    organizer: str = ""
    invited: list[str] = field(default_factory=list)
    #: "recording", "captured", "transcribed", "summarised", "sent", "failed".
    stage: str = "recording"
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: Any) -> "SessionMeta | None":
        if not isinstance(row, dict):
            return None
        session_id = _s(row.get("session_id"))
        if not session_id:
            return None
        invited = row.get("invited")
        return cls(
            session_id=session_id,
            started_at=_s(row.get("started_at")),
            ended_at=_s(row.get("ended_at")),
            meeting_id=_s(row.get("meeting_id")),
            title=_s(row.get("title")),
            provider=_s(row.get("provider")),
            room=_s(row.get("room")),
            organizer=_s(row.get("organizer")),
            invited=[_s(x) for x in invited if _s(x)] if isinstance(invited, list) else [],
            stage=_s(row.get("stage")) or "recording",
            error=_s(row.get("error")),
        )


@dataclass
class Transcript:
    """A meeting, written down and attributed."""

    meta: SessionMeta
    segments: list[Segment] = field(default_factory=list)
    participants: list[Participant] = field(default_factory=list)
    #: Free-text notes about how well the capture went, shown in the web page.
    notices: list[str] = field(default_factory=list)

    # -- derived ---------------------------------------------------------
    @property
    def session_id(self) -> str:
        return self.meta.session_id

    @property
    def duration_seconds(self) -> float:
        return max((s.end for s in self.segments), default=0.0)

    @property
    def word_count(self) -> int:
        return sum(len(s.text.split()) for s in self.segments)

    def speakers(self) -> list[str]:
        """Every distinct label that appears, in order of first appearance."""
        seen: list[str] = []
        for segment in sorted(self.segments, key=lambda s: s.start):
            label = segment.label()
            if label not in seen:
                seen.append(label)
        return seen

    def recipients(self) -> list[str]:
        """Addresses the summary should go to: everyone we have one for.

        Deduplicated case-insensitively but returned in their original form,
        because an address is displayed back to the person who configured it.
        """
        out: list[str] = []
        seen: set[str] = set()
        for address in [p.email for p in self.participants] + list(self.meta.invited):
            clean = _s(address)
            if "@" not in clean:
                continue
            key = clean.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(clean)
        return out

    # -- rendering -------------------------------------------------------
    def render_text(self, *, max_chars: int = 0) -> str:
        """The transcript as ``[mm:ss] Name: text`` lines.

        This is what the summary prompt and the "full transcript" download both
        use. ``max_chars`` truncates from the *start* rather than the end when
        it has to, because the decisions and the actions are at the end of a
        meeting and those are what a summary is for. Truncation is announced in
        the text so neither a reader nor the model can mistake a clipped
        transcript for a short meeting.
        """
        lines = [
            f"[{_clock(segment.start)}] {segment.label()}: {segment.text}"
            for segment in sorted(self.segments, key=lambda s: s.start)
        ]
        body = "\n".join(lines)
        if max_chars and len(body) > max_chars:
            kept = body[-max_chars:]
            # Never start mid-line: find the first line break in what is left.
            newline = kept.find("\n")
            if newline != -1:
                kept = kept[newline + 1 :]
            note = "[earlier part of the transcript omitted because it was too long]"
            body = f"{note}\n{kept}"
        return body

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "meta": self.meta.to_dict(),
            "segments": [s.to_dict() for s in self.segments],
            "participants": [p.to_dict() for p in self.participants],
            "notices": list(self.notices),
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "Transcript | None":
        if not isinstance(payload, dict):
            return None
        meta = SessionMeta.from_dict(payload.get("meta"))
        if meta is None:
            return None
        rows = payload.get("segments")
        parts = payload.get("participants")
        notices = payload.get("notices")
        return cls(
            meta=meta,
            segments=[
                s for s in (Segment.from_dict(r) for r in rows or []) if s is not None
            ]
            if isinstance(rows, list)
            else [],
            participants=[
                p for p in (Participant.from_dict(r) for r in parts or []) if p is not None
            ]
            if isinstance(parts, list)
            else [],
            notices=[_s(n) for n in notices if _s(n)] if isinstance(notices, list) else [],
        )

    def summary_context(self) -> dict[str, Any]:
        """The facts about the meeting the summary prompt puts above the text."""
        return {
            "title": self.meta.title or "Meeting",
            "room": self.meta.room,
            "provider": self.meta.provider,
            "started_at": self.meta.started_at,
            "ended_at": self.meta.ended_at,
            "duration_minutes": round(self.duration_seconds / 60.0, 1),
            "in_room": [p.name for p in self.participants if p.where == "room"],
            "remote": [p.name for p in self.participants if p.where == "remote"],
            "invited": list(self.meta.invited),
        }


def _clock(seconds: float) -> str:
    """``mm:ss``, or ``h:mm:ss`` once a meeting runs past the hour."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def new_session_id(now: datetime | None = None, *, token: str = "") -> str:
    """A sortable, collision-resistant id: ``YYYYMMDD-HHMMSS-xxxxxxxx``."""
    import secrets

    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{token or secrets.token_hex(4)}"
