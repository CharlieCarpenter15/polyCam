"""Turning the two recordings into words, with whatever engine this box has.

Speech-to-text is the one part of this feature that cannot be made cheap. An
hour-long meeting is two hours of audio, and on a Raspberry Pi 5 that is one to
two hours of busy CPU *after* the meeting — on the same machine that has to
drive the room screen and be ready for the next meeting. Three consequences run
through this module:

*It is pluggable, and every engine is optional.* ``whisper.cpp`` is the default
because it is a single small binary and one model file, it keeps the audio in
the room, and it punctuates. ``faster-whisper`` suits a mini-PC, ``vosk`` suits
a machine that cannot spare the cores, and ``mock`` exists so the whole pipeline
can be exercised with no models at all. An engine that is not installed says so
in a sentence a person can act on; it never raises.

*Nothing is transcribed on a thread anybody is waiting for.* ``service.py``
runs this on its worker thread, one session at a time, after the meeting has
ended. So the timeouts here are generous — thirty times real time, with a floor
of five minutes — because a slow transcription on a hot Pi is normal and
killing it half way through loses the meeting. Where an engine is a subprocess
it is run under ``nice``, and where it is a library the calling thread lowers
its own priority: the room screen staying responsive matters more than the
transcript arriving ten minutes sooner.

*The two tracks are transcribed separately and then reconciled.* Which file a
voice arrived in is the only wholly reliable fact about who was speaking, so the
tracks are never mixed. But the room microphone also *hears* the speaker, so the
far end can turn up on both tracks: the same sentence, a moment later, in
room acoustics. Left alone that reads as the remote side saying everything
twice. ``drop_echoes`` removes a room line that closely matches an overlapping
far-end line — conservatively, because dropping something somebody in the room
actually said is much worse than leaving a duplicate in.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import hardware_profile
from ..logging_setup import get_logger, log_event
from ..system_service import run, which
from . import deps, paths
from .audio import FAR_END_WAV, ROOM_WAV, wav_seconds
from .transcript import TRACK_FAR_END, TRACK_ROOM, Segment

log = get_logger("minutes.transcribe")

#: Engine names, as they appear in ``MINUTES_STT_ENGINE`` and in the logs.
WHISPER_CPP = "whisper-cpp"
FASTER_WHISPER = "faster-whisper"
VOSK = "vosk"
MOCK = "mock"
NONE = "none"

#: Which engine ``auto`` tries first. Best transcript per megabyte installed.
AUTO_ORDER = (WHISPER_CPP, FASTER_WHISPER, VOSK)

#: A transcription may take this many times the length of the audio before it
#: is assumed to be wedged rather than merely slow. A Pi 4 running ``base.en``
#: is genuinely three times slower than real time, and thermal throttling makes
#: that worse as the run goes on.
TIMEOUT_REALTIME_FACTOR = 30.0
TIMEOUT_FLOOR_SECONDS = 300.0

#: Below this a track is silence or a fragment, and worth a notice rather than
#: an hour of an engine's time.
MIN_TRACK_SECONDS = 0.25

#: Echo suppression. The ratio is high because the two versions of a sentence
#: differ only in recognition mistakes; the word floor keeps “yes” and “mm-hm”
#: — which two people genuinely do say at once — out of it entirely.
ECHO_SIMILARITY = 0.86
ECHO_MIN_WORDS = 3
#: The room hears the speaker a moment after the call does, and the two engines
#: cut their sentences in different places, so “at the same time” needs slack.
ECHO_TOLERANCE_SECONDS = 1.5

_PUNCTUATION = re.compile(r"[^\w\s]+", re.UNICODE)
_TIMESTAMP_LINE = re.compile(
    r"^\s*\[\s*([\d:.,]+)\s*-->\s*([\d:.,]+)\s*\]\s*(.*\S)\s*$"
)


@dataclass(frozen=True)
class Utterance:
    """One stretch of recognised speech, in seconds from the start of the WAV."""

    start: float
    end: float
    text: str
    confidence: float = 0.0

    def to_segment(self, track: str) -> Segment:
        return Segment(
            start=max(0.0, self.start),
            end=max(self.start, self.end),
            text=self.text,
            track=track,
            confidence=self.confidence,
        )


class Engine(ABC):
    """A transcription backend. Implementations must never raise.

    An engine is constructed with the configuration it was chosen under, so
    that ``transcribe()`` needs only the file in front of it — which is what
    makes an engine testable on its own, with one WAV and nothing else.
    """

    #: Stable id used in the configuration, the logs and the Settings page.
    name: str = "unnamed"

    def __init__(self, config: Any = None) -> None:
        self.config: Any = config if config is not None else _ConfigLess()

    @abstractmethod
    def available(self, config: Any) -> tuple[bool, str]:
        """``(True, "")`` when usable, else ``(False, what is missing)``."""

    @abstractmethod
    def transcribe(
        self, wav: Path, *, language: str, timeout: float
    ) -> tuple[list[Utterance], str]:
        """Transcribe one 16 kHz mono WAV. ``([], reason)`` on any failure."""

    def describe(self, config: Any) -> dict[str, Any]:
        ok, detail = self.available(config)
        return {"name": self.name, "ok": bool(ok), "detail": detail}


# ---------------------------------------------------------------------------
# The engines
# ---------------------------------------------------------------------------


class MockEngine(Engine):
    """Plausible words, no model, same answer every time.

    Development mode and the test suite both need a transcript to work on, and
    every real engine needs hundreds of megabytes that CI will never have. The
    text is derived from the file’s length and name, so a room track and a
    far-end track read like two sides of a conversation rather than an echo of
    each other — which would make the echo suppression look broken when it is
    not.
    """

    name = MOCK

    #: Deliberately dull meeting-shaped sentences: they have to look like a
    #: transcript in a screenshot without ever being mistaken for a real one.
    ROOM_LINES = (
        "Right, shall we make a start.",
        "I have put the figures for last month in the shared folder.",
        "That is roughly where we were at the end of the quarter.",
        "Can we come back to that once the numbers are in?",
        "Agreed — I will pick that up this week.",
        "Anything else before we finish?",
    )
    FAR_END_LINES = (
        "Morning everyone, can you hear me at the back?",
        "We have had the same question from two other sites.",
        "Let me share my screen for a moment.",
        "That matches what we are seeing on our side.",
        "I will send the summary round after this.",
        "Thanks all, speak next week.",
    )

    #: One line per this many seconds of audio, which is about the pace people
    #: actually talk at in a meeting.
    SECONDS_PER_LINE = 6.0

    def available(self, config: Any) -> tuple[bool, str]:
        return True, ""

    def transcribe(
        self, wav: Path, *, language: str, timeout: float
    ) -> tuple[list[Utterance], str]:
        try:
            seconds = wav_seconds(wav)
        except Exception:  # pragma: no cover - defensive
            return [], "The placeholder engine could not read the recording."
        if seconds <= 0:
            return [], ""
        far_end = "far" in wav.stem.lower()
        lines = self.FAR_END_LINES if far_end else self.ROOM_LINES
        # Offset the two tracks against each other so they interleave in the
        # merged transcript the way a conversation does.
        offset = self.SECONDS_PER_LINE / 2.0 if far_end else 0.0
        out: list[Utterance] = []
        index = 0
        while offset + (index * self.SECONDS_PER_LINE) < seconds:
            start = offset + index * self.SECONDS_PER_LINE
            end = min(seconds, start + self.SECONDS_PER_LINE - 1.0)
            if end <= start:
                break
            out.append(
                Utterance(
                    start=round(start, 3),
                    end=round(end, 3),
                    text=lines[index % len(lines)],
                    confidence=0.9,
                )
            )
            index += 1
        return out, ""


class WhisperCppEngine(Engine):
    """``whisper-cli`` in a subprocess: the default, and the one that ships.

    A binary and a model file, both of which can be staged onto the appliance
    ahead of time, with nothing pulled from the network at the moment somebody
    needs it. JSON output is asked for because it carries the segment offsets
    exactly; the timestamped stdout is parsed as a fallback because older builds
    write nothing else.
    """

    name = WHISPER_CPP

    def available(self, config: Any) -> tuple[bool, str]:
        probe = deps.probe("whisper-cpp")
        if not probe.ok:
            return False, probe.detail or (
                "“whisper-cli” was not found on PATH, so there is nothing to "
                "transcribe with. See docs/meeting-minutes.md."
            )
        gate = _too_slow_for_local_transcription(config)
        if gate:
            return False, gate
        model = whisper_model(config)
        if model is None:
            return False, (
                "No whisper.cpp model was found. Put a ggml model file — "
                "“ggml-base.en.bin” is the usual choice — in "
                f"{paths.MODELS_DIR}, or name one in Settings."
            )
        return True, ""

    def transcribe(
        self, wav: Path, *, language: str, timeout: float
    ) -> tuple[list[Utterance], str]:
        binary = deps.binary_path("whisper-cpp") or "whisper-cli"
        model = whisper_model(self.config)
        if model is None:
            return [], (
                "No whisper.cpp model was found, so nothing could be transcribed."
            )
        prefix = wav.with_suffix("")
        argv = nice_prefix() + [
            binary,
            "-m", str(model),
            "-f", str(wav),
            "-t", str(_threads()),
            "-l", language or "en",
            "-oj",
            "-of", str(prefix),
            "-np",
        ]
        result = run(argv, timeout=max(TIMEOUT_FLOOR_SECONDS, timeout))
        candidates = [
            prefix.with_name(prefix.name + ".json"),
            wav.with_name(wav.name + ".json"),
        ]
        utterances: list[Utterance] = []
        for candidate in candidates:
            payload = _read_json(candidate)
            if payload is not None:
                utterances = parse_whisper_json(payload)
                _discard(candidate)
                if utterances:
                    break
        if not utterances:
            utterances = parse_whisper_output(result.stdout)
        if not result.ok and not utterances:
            detail = _tail(result.stderr or result.stdout)
            log_event(
                log, logging.WARNING, "minutes.transcribe.failed",
                engine=self.name, code=result.code, detail=detail,
            )
            return [], f"whisper.cpp could not transcribe {wav.name}: {detail}"
        return utterances, ""


class FasterWhisperEngine(Engine):
    """The ``faster_whisper`` library, for a machine with room for it.

    Several hundred megabytes of wheels and a model fetched from the network on
    first use, which is why it is not the default on an appliance — but on a
    mini-PC it is the better transcript.
    """

    name = FASTER_WHISPER

    def available(self, config: Any) -> tuple[bool, str]:
        probe = deps.probe("faster_whisper")
        if not probe.ok:
            return False, probe.detail or (
                "“faster-whisper” is not installed. Install it with: "
                "pip install faster-whisper"
            )
        gate = _too_slow_for_local_transcription(config)
        if gate:
            return False, gate
        return True, ""

    def transcribe(
        self, wav: Path, *, language: str, timeout: float
    ) -> tuple[list[Utterance], str]:
        # Imported here and nowhere else: the appliance must start on a machine
        # that has never heard of it, and importing it costs seconds.
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - not installed in CI
            return [], f"faster-whisper could not be loaded: {exc}"
        _be_polite()
        model_name = faster_whisper_model(self.config)
        try:
            model = WhisperModel(
                model_name,
                device="cpu",
                compute_type="int8",
                cpu_threads=_threads(),
            )
            segments, _info = model.transcribe(
                str(wav),
                language=None if language in ("", "auto") else language,
                beam_size=1,
            )
            out: list[Utterance] = []
            for segment in segments:
                text = str(getattr(segment, "text", "") or "").strip()
                if not text:
                    continue
                out.append(
                    Utterance(
                        start=float(getattr(segment, "start", 0.0) or 0.0),
                        end=float(getattr(segment, "end", 0.0) or 0.0),
                        text=text,
                        confidence=_probability(getattr(segment, "avg_logprob", None)),
                    )
                )
            return out, ""
        except Exception as exc:  # pragma: no cover - not installed in CI
            log_event(
                log, logging.WARNING, "minutes.transcribe.failed",
                engine=self.name, detail=str(exc),
            )
            return [], f"faster-whisper could not transcribe {wav.name}: {exc}"


class VoskEngine(Engine):
    """Kaldi in a 40 MB box: fast even on slow hardware, and no punctuation.

    Kept because it is the only engine that runs comfortably on a Pi 3, and
    because the same package carries the speaker-embedding model the voice
    fingerprints use. The transcript is harder to read than whisper’s.
    """

    name = VOSK

    def available(self, config: Any) -> tuple[bool, str]:
        probe = deps.probe("vosk")
        if not probe.ok:
            return False, probe.detail or (
                "“vosk” is not installed. Install it with: pip install vosk"
            )
        if vosk_model(config) is None:
            return False, (
                "No vosk model was found. Unpack one — "
                "“vosk-model-small-en-us-0.15” is a good start — into "
                f"{paths.MODELS_DIR}, or name it in Settings."
            )
        return True, ""

    def transcribe(
        self, wav: Path, *, language: str, timeout: float
    ) -> tuple[list[Utterance], str]:
        try:
            import wave

            from vosk import KaldiRecognizer, Model  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - not installed in CI
            return [], f"vosk could not be loaded: {exc}"
        model_dir = vosk_model(self.config)
        if model_dir is None:
            return [], "No vosk model was found, so nothing could be transcribed."
        _be_polite()
        try:
            with wave.open(str(wav), "rb") as handle:
                if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                    return [], (
                        f"{wav.name} is not mono 16-bit audio, which is the only "
                        "thing vosk reads."
                    )
                recogniser = KaldiRecognizer(Model(str(model_dir)), handle.getframerate())
                recogniser.SetWords(True)
                out: list[Utterance] = []
                while True:
                    block = handle.readframes(4000)
                    if not block:
                        break
                    if recogniser.AcceptWaveform(block):
                        out.extend(parse_vosk_result(recogniser.Result()))
                out.extend(parse_vosk_result(recogniser.FinalResult()))
            return out, ""
        except Exception as exc:  # pragma: no cover - not installed in CI
            log_event(
                log, logging.WARNING, "minutes.transcribe.failed",
                engine=self.name, detail=str(exc),
            )
            return [], f"vosk could not transcribe {wav.name}: {exc}"


class DisabledEngine(Engine):
    """Speech-to-text switched off in Settings, which is a valid answer."""

    name = NONE

    def available(self, config: Any) -> tuple[bool, str]:
        return False, (
            "Speech-to-text is switched off, so the meeting is recorded and who "
            "was there is worked out, but no words are written down."
        )

    def transcribe(
        self, wav: Path, *, language: str, timeout: float
    ) -> tuple[list[Utterance], str]:
        return [], self.available(None)[1]


#: Every engine, in the order the Settings page should list them.
ENGINES: dict[str, type[Engine]] = {
    WHISPER_CPP: WhisperCppEngine,
    FASTER_WHISPER: FasterWhisperEngine,
    VOSK: VoskEngine,
    MOCK: MockEngine,
    NONE: DisabledEngine,
}


# ---------------------------------------------------------------------------
# Choosing one
# ---------------------------------------------------------------------------


def choose_engine(config: Any) -> Engine:
    """The engine this configuration and this machine should use.

    Never returns None. When nothing at all is installed it returns the engine
    we would *like* to use, because its ``available()`` is the sentence that
    tells somebody what to install.
    """
    try:
        wanted = _text(config, "MINUTES_STT_ENGINE").lower() or "auto"
        if wanted in ENGINES:
            return ENGINES[wanted](config)
        if wanted != "auto":
            log_event(
                log, logging.WARNING, "minutes.transcribe.unknown_engine",
                engine=wanted,
            )
        for name in AUTO_ORDER:
            engine = ENGINES[name](config)
            ok, _why = engine.available(config)
            if ok:
                return engine
        if _flag(config, "DEV_MODE"):
            return MockEngine(config)
        return WhisperCppEngine(config)
    except Exception:  # pragma: no cover - defensive
        log.exception("minutes.transcribe.choose_failed")
        return WhisperCppEngine(config)


def chosen_engine_name(config: Any) -> str:
    return choose_engine(config).name


def available(config: Any) -> tuple[bool, str]:
    """Can this appliance write down what was said, and if not why not."""
    try:
        return choose_engine(config).available(config)
    except Exception:  # pragma: no cover - a status call must never fail
        log.exception("minutes.transcribe.available_failed")
        return False, "The speech-to-text engine could not be checked."


def engine_report(config: Any) -> list[dict[str, Any]]:
    """Every engine, whether it is usable, and what is missing when it is not."""
    chosen = chosen_engine_name(config)
    rows: list[dict[str, Any]] = []
    for name, factory in ENGINES.items():
        try:
            row = factory(config).describe(config)
        except Exception:  # pragma: no cover - defensive
            row = {"name": name, "ok": False, "detail": "This engine could not be checked."}
        row["chosen"] = name == chosen
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Transcribing a session
# ---------------------------------------------------------------------------


def transcribe_session(
    engine: Engine,
    directory: Path,
    config: Any,
    *,
    skip_far_end: bool = False,
) -> tuple[list[Segment], list[str]]:
    """Both tracks of one recording, merged and de-duplicated.

    ``skip_far_end`` is for the case where the meeting window’s own live
    captions already carry the remote speakers’ words *with their names* — which
    is both more accurate and free. The caller supplies those segments itself
    and merges them afterwards, so it must run :func:`drop_echoes` over the
    merged list: the room microphone still hears the far end, and the echoes in
    this half of the transcript cannot be seen from here.

    The far-end recording is left on disk either way. What happens to it is the
    retention policy’s decision, not this module’s.
    """
    notices: list[str] = []
    try:
        if engine is None:
            return [], ["No speech-to-text engine is configured."]
        ok, why = engine.available(config)
        if not ok:
            return [], [why or "The speech-to-text engine is not available."]

        language = _language(config)
        wanted = [(TRACK_ROOM, ROOM_WAV, "the room microphone")]
        if skip_far_end:
            notices.append(
                "The call’s own audio was not transcribed: the meeting window’s "
                "captions were used for the remote speakers instead."
            )
        else:
            wanted.append((TRACK_FAR_END, FAR_END_WAV, "the room’s own output"))

        segments: list[Segment] = []
        found = 0
        for track, filename, label in wanted:
            wav = directory / filename
            if not wav.exists():
                continue
            found += 1
            seconds = wav_seconds(wav)
            if seconds < MIN_TRACK_SECONDS:
                notices.append(f"The recording of {label} was empty.")
                continue
            utterances, error = _transcribe_one(
                engine, config, wav, language=language, seconds=seconds
            )
            if error:
                notices.append(error)
                continue
            if not utterances:
                notices.append(f"No speech was recognised in the recording of {label}.")
            segments.extend(u.to_segment(track) for u in utterances)
            log_event(
                log, logging.INFO, "minutes.transcribe.track",
                engine=engine.name, track=track,
                seconds=round(seconds), segments=len(utterances),
            )

        if not found:
            notices.append("There was no audio to transcribe.")

        segments.sort(key=lambda s: (s.start, s.end))
        kept, dropped = drop_echoes(segments)
        if dropped == 1:
            notices.append(
                "One line the room microphone picked up from the speaker was "
                "removed, because the call’s own recording already had it."
            )
        elif dropped:
            notices.append(
                f"{dropped} lines the room microphone picked up from the speaker "
                "were removed, because the call’s own recording already had them."
            )
        return kept, notices
    except Exception as exc:  # pragma: no cover - the worker must never die
        log.exception("minutes.transcribe.session_failed")
        return [], [*notices, f"The transcription failed: {exc}"]


def _transcribe_one(
    engine: Engine,
    config: Any,
    wav: Path,
    *,
    language: str,
    seconds: float,
) -> tuple[list[Utterance], str]:
    """One file, with a timeout derived from how long the audio actually is."""
    timeout = timeout_for(seconds)
    # An engine chosen by hand in a test may not have been given the session's
    # configuration; hand it over rather than fall back to the defaults.
    if getattr(engine, "config", None) is None or isinstance(
        getattr(engine, "config", None), _ConfigLess
    ):
        try:
            engine.config = config
        except Exception:  # pragma: no cover - a frozen or exotic engine
            pass
    try:
        return engine.transcribe(wav, language=language, timeout=timeout)
    except Exception as exc:  # an engine should not raise, but one day one will
        log.exception("minutes.transcribe.engine_raised")
        return [], f"{engine.name} failed on {wav.name}: {exc}"


def timeout_for(seconds: float) -> float:
    """How long to let an engine run: thirty times real time, floor five minutes."""
    return max(TIMEOUT_FLOOR_SECONDS, float(max(0.0, seconds)) * TIMEOUT_REALTIME_FACTOR)


def drop_echoes(segments: list[Segment]) -> tuple[list[Segment], int]:
    """Remove room lines that are really the far end coming back off the speaker.

    Returns ``(kept, dropped)``. Public because the caller may merge segments
    from elsewhere — the meeting window’s captions, most obviously — and the
    same comparison has to run once over the whole transcript rather than twice
    over half of it.

    Conservative on purpose: a high similarity ratio, an overlap in time, and a
    minimum length. A duplicated line is untidy; a deleted line that somebody in
    the room actually said is a transcript that lies.
    """
    if not segments:
        return [], 0
    far_end = [
        (segment, _normalise(segment.text))
        for segment in segments
        if segment.track == TRACK_FAR_END
    ]
    far_end = [(segment, text) for segment, text in far_end if text]
    if not far_end:
        return list(segments), 0

    dropped: set[int] = set()
    for index, segment in enumerate(segments):
        if segment.track != TRACK_ROOM:
            continue
        text = _normalise(segment.text)
        if len(text.split()) < ECHO_MIN_WORDS:
            continue
        for other, other_text in far_end:
            if not _overlaps(segment, other):
                continue
            if _similar(text, other_text):
                dropped.add(index)
                break
    if not dropped:
        return list(segments), 0
    kept = [s for i, s in enumerate(segments) if i not in dropped]
    log_event(log, logging.INFO, "minutes.transcribe.echoes_dropped", count=len(dropped))
    return kept, len(dropped)


def _overlaps(one: Segment, other: Segment) -> bool:
    """Do these two overlap in time, allowing for the delay round the room?"""
    tolerance = ECHO_TOLERANCE_SECONDS
    return one.start < other.end + tolerance and other.start < one.end + tolerance


def _similar(one: str, other: str) -> bool:
    matcher = difflib.SequenceMatcher(None, one, other)
    # The cheap bounds first: on a long meeting this comparison runs thousands
    # of times, and both of these are O(n) where ratio() is not.
    if matcher.real_quick_ratio() < ECHO_SIMILARITY:
        return False
    if matcher.quick_ratio() < ECHO_SIMILARITY:
        return False
    return matcher.ratio() >= ECHO_SIMILARITY


def _normalise(text: str) -> str:
    """Case, punctuation and spacing removed: what was said, not how it was cut."""
    return " ".join(_PUNCTUATION.sub(" ", (text or "").lower()).split())


# ---------------------------------------------------------------------------
# Parsing what the engines produce
# ---------------------------------------------------------------------------


def parse_whisper_json(payload: Any) -> list[Utterance]:
    """whisper.cpp’s ``-oj`` output: offsets in milliseconds, one per segment."""
    if isinstance(payload, dict):
        rows = payload.get("transcription")
    else:
        rows = payload
    if not isinstance(rows, list):
        return []
    out: list[Utterance] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        start = end = None
        offsets = row.get("offsets")
        if isinstance(offsets, dict):
            start = _milliseconds(offsets.get("from"))
            end = _milliseconds(offsets.get("to"))
        if start is None or end is None:
            stamps = row.get("timestamps")
            if isinstance(stamps, dict):
                start = _clock_seconds(stamps.get("from"))
                end = _clock_seconds(stamps.get("to"))
        if start is None:
            continue
        out.append(Utterance(start=start, end=end if end is not None else start, text=text))
    return out


def parse_whisper_output(text: str) -> list[Utterance]:
    """The ``[00:00:00.000 --> 00:00:02.000]  text`` lines whisper.cpp prints."""
    out: list[Utterance] = []
    for line in (text or "").splitlines():
        match = _TIMESTAMP_LINE.match(line)
        if not match:
            continue
        start = _clock_seconds(match.group(1))
        end = _clock_seconds(match.group(2))
        said = match.group(3).strip()
        if start is None or not said:
            continue
        out.append(Utterance(start=start, end=end if end is not None else start, text=said))
    return out


def parse_vosk_result(raw: Any) -> list[Utterance]:
    """One vosk result: word times give the span, word confidences the average."""
    try:
        payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (ValueError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    text = str(payload.get("text") or "").strip()
    if not text:
        return []
    words = payload.get("result")
    start = end = 0.0
    confidence = 0.0
    if isinstance(words, list) and words:
        starts = [_float(w.get("start")) for w in words if isinstance(w, dict)]
        ends = [_float(w.get("end")) for w in words if isinstance(w, dict)]
        confidences = [_float(w.get("conf")) for w in words if isinstance(w, dict)]
        start = min(starts) if starts else 0.0
        end = max(ends) if ends else start
        confidence = round(sum(confidences) / len(confidences), 3) if confidences else 0.0
    return [Utterance(start=start, end=end, text=text, confidence=confidence)]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def whisper_model(config: Any) -> Path | None:
    """The ggml model to use: the configured one, else the newest downloaded."""
    named = _text(config, "MINUTES_STT_MODEL")
    if named:
        path = Path(named).expanduser()
        if path.is_file():
            return path
        if path.is_dir():
            newest = _newest(path, "*.bin")
            if newest is not None:
                return newest
        return None
    return _newest(paths.MODELS_DIR, "*.bin")


def vosk_model(config: Any) -> Path | None:
    """The vosk model directory: the configured one, else one in the models dir."""
    named = _text(config, "MINUTES_STT_MODEL")
    if named:
        path = Path(named).expanduser()
        return path if _looks_like_vosk(path) else None
    try:
        entries = sorted(paths.MODELS_DIR.iterdir())
    except OSError:
        return None
    for entry in entries:
        if _looks_like_vosk(entry):
            return entry
    return None


def faster_whisper_model(config: Any) -> str:
    """A path, or the name of a model faster-whisper will fetch for itself."""
    named = _text(config, "MINUTES_STT_MODEL")
    if named:
        return named
    profile, _machine = hardware_profile.resolve(_text(config, "PERFORMANCE_PROFILE"))
    return "small.en" if profile == hardware_profile.HIGH else "base.en"


def _looks_like_vosk(path: Path) -> bool:
    """A vosk model is a directory with a recogniser configuration in it."""
    try:
        if not path.is_dir():
            return False
        return (path / "conf").is_dir() or (path / "am").is_dir()
    except OSError:
        return False


def _newest(directory: Path, pattern: str) -> Path | None:
    try:
        candidates = [p for p in directory.glob(pattern) if p.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: _mtime(p))


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# Being a good neighbour on a small machine
# ---------------------------------------------------------------------------


def nice_prefix() -> list[str]:
    """Run a transcription behind everything else the room is doing."""
    return ["nice", "-n", "15"] if which("nice") else []


def _threads() -> int:
    """Leave a core for the room. A saturated Pi makes the kiosk stutter."""
    cores = os.cpu_count() or 2
    return max(1, min(4, cores - 1))


def _be_polite() -> None:
    """Lower the calling thread’s priority before a long in-process run.

    On Linux ``nice`` applies to the calling thread, and transcription always
    happens on the minutes worker thread, so this deprioritises the slow work
    without touching the web server or the room’s state machine. It cannot be
    undone without privileges, which is fine: that thread never does anything
    the room is waiting for.
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        os.nice(15)
    except (OSError, AttributeError):  # pragma: no cover - not permitted
        pass


def _too_slow_for_local_transcription(config: Any) -> str:
    """Why this machine should not run a whisper model, or "" if it can.

    A Pi 3 takes most of a working day over an hour-long meeting, on the same
    four slow cores that are trying to hold a video call together. Refusing is
    kinder than a transcript that arrives tomorrow.
    """
    profile, machine = hardware_profile.resolve(_text(config, "PERFORMANCE_PROFILE"))
    if profile != hardware_profile.LOW:
        return ""
    return (
        f"This machine ({machine.describe()}) is too slow to transcribe a meeting "
        "locally — it would take most of a day. Use a faster appliance, or switch "
        "speech-to-text off in Settings."
    )


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


class _ConfigLess:
    """Stands in for the configuration when an engine is called without one.

    ``Engine.transcribe`` is deliberately narrow — a WAV, a language, a timeout —
    so that an engine can be exercised on its own. Given no configuration, the
    defaults apply: whatever model is on disk, in the usual place.
    """

    def str_(self, key: str) -> str:
        return ""

    def int_(self, key: str) -> int:
        return 0

    def bool_(self, key: str) -> bool:
        return False


def _language(config: Any) -> str:
    language = _text(config, "MINUTES_STT_LANGUAGE").lower()
    return language or "en"


def _text(config: Any, key: str) -> str:
    try:
        return str(config.str_(key) or "").strip()
    except Exception:  # pragma: no cover - a stub config in a test
        return ""


def _flag(config: Any, key: str) -> bool:
    try:
        return bool(config.bool_(key))
    except Exception:  # pragma: no cover - a stub config in a test
        return False


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _probability(log_probability: Any) -> float:
    """An average log probability as something between nought and one."""
    try:
        import math

        return round(min(1.0, max(0.0, math.exp(float(log_probability)))), 3)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _milliseconds(value: Any) -> float | None:
    try:
        return max(0.0, float(value) / 1000.0)
    except (TypeError, ValueError):
        return None


def _clock_seconds(value: Any) -> float | None:
    """``00:00:02.500`` or ``00:00:02,500`` or ``00:02.500`` in seconds."""
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    parts = text.split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60.0 + number
    return round(seconds, 3)


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _tail(text: str, limit: int = 200) -> str:
    return " ".join(str(text or "").split())[-limit:] or "no output"
