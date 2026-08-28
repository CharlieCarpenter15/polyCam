"""Telling the people in the room apart by their voices.

This is the least reliable thing the appliance does, and the module is written
on that assumption. A conference bar's microphone is metres from the speaker,
in a room with hard walls and a table, and it hears reverberation as well as
speech. Speaker recognition is hard in a headset; on a far-field microphone it
is markedly harder, and one enrolment sample makes it harder still. So:

* it is off by default and labelled experimental in the settings;
* a match below the configured threshold produces **no name**, never a guess;
* two unidentified people are kept apart as "Room speaker 2" and "Room speaker
  3" rather than being merged or being given a name, because knowing that two
  different people spoke is genuinely useful even when neither can be named;
* the best enrolment path is not this module at all. It is somebody reading the
  transcript afterwards and saying who "Room speaker 2" was — which corrects the
  transcript and teaches the profile from the very audio that defeated it.

How it works: find the speech with a voice-activity detector, turn each stretch
of speech into a fixed-length vector, and compare vectors by cosine similarity
against the enrolled profiles.

Three ways to make that vector, in order of preference:

``sherpa-onnx``
    A modern speaker-embedding model — TitaNet is the one to use — run on the
    ONNX runtime that ships inside the package. No PyTorch, no compiler, about
    17 MB of wheels and a 40 MB model. In a simulated reverberant room it
    identified one of twenty speakers correctly 92% of the time, against 38-48%
    for the alternatives below. The reason is cepstral mean normalisation:
    these models carry it in their metadata and the older exports do not, and
    under reverberation that single difference decides the result.

``vosk``
    A 2020 Kaldi x-vector. It works, at roughly two and a half times the error
    rate, and it cannot produce a vector without a full speech-recognition model
    loaded alongside it — so it pays for transcription on every segment it only
    wants to fingerprint. Supported because an appliance may already have vosk
    installed for its transcripts.

``mfcc``
    A fallback built from nothing but numpy: the mean and spread of the
    mel-frequency cepstral coefficients over the segment. It is a decades-old
    technique, it needs no model file, and it is honestly not very good — it
    picks up the room and the microphone alongside the speaker. It exists so
    that "keep two speakers apart within one meeting" works without a download,
    which is the job it can actually do. Names come from vosk.

Which one produced a vector is recorded with it, and a vector made by one is
never compared against a vector made by the other.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import struct
import wave
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..logging_setup import get_logger, log_event
from . import deps, paths
from .people import KIND_VOICE, PeopleStore
from .transcript import TRACK_FAR_END, TRACK_ROOM, Segment

log = get_logger("minutes.voice")

#: Names recorded alongside every vector. Changing the maths behind one means
#: changing its name, so that old profiles are ignored rather than silently
#: compared against something incomparable.
MODEL_TITANET = "titanet-small-1"
MODEL_VOSK = "vosk-xvector-1"
MODEL_MFCC = "mfcc-stats-1"

#: The audio the recorder writes, and the only shape anything here accepts.
SAMPLE_RATE = 16000

#: Speech shorter than this is not worth fingerprinting — it is a "mm" or the
#: tail of somebody else's sentence, and it produces a vector that matches
#: almost anybody.
MIN_SPEECH_SECONDS = 1.2

#: Beyond this, more audio stops improving the vector.
MAX_SPEECH_SECONDS = 12.0

#: Enrolment needs more than recognition does: this is the floor below which a
#: sample is refused rather than stored as a bad profile.
MIN_ENROL_SECONDS = 3.0

#: Two unnamed segments this similar are treated as the same person. Chosen
#: below the recognition threshold on purpose — the question "are these two the
#: same voice in one room, one meeting, one microphone" is much easier than
#: "whose voice is this", so it can afford to be answered more readily.
CLUSTER_THRESHOLD = 0.55

#: A cluster smaller than this is left unlabelled rather than promoted to a
#: numbered speaker. One stray segment is more likely a mis-cut than a person.
MIN_CLUSTER_SEGMENTS = 2

#: webrtcvad frames must be 10, 20 or 30 ms. 30 ms is the most forgiving.
_VAD_FRAME_MS = 30
_VAD_AGGRESSIVENESS = 2

#: Energy-fallback tuning. A frame counts as speech when it is this many times
#: above the quietest tenth of the file, which adapts to a room's noise floor
#: instead of assuming one.
_ENERGY_RATIO = 3.0
_ENERGY_FRAME_MS = 30

#: Gaps shorter than this do not end a speech turn — people breathe.
_JOIN_GAP_SECONDS = 0.4


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def available(config: Any) -> tuple[bool, str]:
    """Can voices be recognised, and if not, what is missing?"""
    if not config.bool_("MINUTES_IDENTIFY_VOICES"):
        return False, "Voice recognition is switched off."
    if not deps.available("numpy"):
        return False, deps.explain("numpy")
    if _titanet_model() is not None or _vosk_models() is not None:
        if not deps.available("webrtcvad"):
            return True, (
                f"{deps.explain('webrtcvad').rstrip('.')}. Without it the "
                "appliance falls back to detecting speech by loudness alone, "
                "which misses far too much of it to put names to voices — so "
                "speakers will be told apart but not named."
            )
        return True, ""
    reason = deps.explain("sherpa_onnx") or (
        "No speaker model was found — put a TitaNet speaker-embedding model "
        f"(a .onnx file) in {paths.MODELS_DIR}."
    )
    return True, (
        f"{reason.rstrip('.')}. Until then, speakers in the room can be told "
        "apart from one another but cannot be named."
    )


def _titanet_model() -> Path | None:
    """The speaker-embedding ONNX file, when one has been installed."""
    if not deps.available("sherpa_onnx"):
        return None
    try:
        candidates = sorted(paths.MODELS_DIR.glob("*.onnx"))
    except OSError:
        return None
    for path in candidates:
        name = path.name.lower()
        if "titanet" in name or "speaker" in name or "ecapa" in name:
            return path
    return None


def _vad_is_reliable() -> bool:
    """Is speech being found properly, or only by loudness?

    The energy fallback misses between a third and two thirds of speech in a
    reverberant room. Naming a speaker from a segment that may be half somebody
    else's sentence is how a transcript ends up confidently wrong, so when the
    fallback is what is running, voices are separated but never named.
    """
    return deps.available("webrtcvad")


def _vosk_models() -> tuple[Path, Path] | None:
    """The speech model and speaker model directories, when both are present."""
    if not deps.available("vosk"):
        return None
    try:
        entries = [p for p in paths.MODELS_DIR.iterdir() if p.is_dir()]
    except OSError:
        return None
    speaker = next((p for p in entries if "spk" in p.name.lower()), None)
    speech = next(
        (p for p in entries if "spk" not in p.name.lower() and "vosk" in p.name.lower()),
        None,
    )
    if speaker is None or speech is None:
        return None
    return speech, speaker


def model_name() -> str:
    """Which kind of vector this appliance will produce right now."""
    if _titanet_model() is not None:
        return MODEL_TITANET
    return MODEL_VOSK if _vosk_models() is not None else MODEL_MFCC


def can_name_people() -> bool:
    """Is there a model good enough to put a name to a voice?"""
    return model_name() != MODEL_MFCC and _vad_is_reliable()


# ---------------------------------------------------------------------------
# Reading audio
# ---------------------------------------------------------------------------


def read_wav(path: Path, start: float = 0.0, end: float = 0.0) -> tuple[bytes, str]:
    """Raw 16-bit mono samples between ``start`` and ``end`` seconds.

    A recording cut short by a power cut has a RIFF header claiming more frames
    than the file holds, so a short read is expected rather than exceptional and
    is handled by asking for what is there.
    """
    try:
        with contextlib.closing(wave.open(str(path), "rb")) as handle:
            if handle.getsampwidth() != 2 or handle.getnchannels() != 1:
                return b"", "The recording is not 16-bit mono, which is all this reads."
            rate = handle.getframerate()
            total = handle.getnframes()
            first = max(0, int(start * rate))
            last = int(end * rate) if end > start else total
            last = min(last, total) if total > 0 else last
            if first:
                handle.setpos(min(first, max(0, total - 1)))
            wanted = max(0, last - first)
            return handle.readframes(wanted), ""
    except (wave.Error, OSError, EOFError) as exc:
        return b"", f"The recording could not be read ({exc.__class__.__name__})."


def _samples(raw: bytes) -> list[int]:
    count = len(raw) // 2
    if not count:
        return []
    return list(struct.unpack(f"<{count}h", raw[: count * 2]))


# ---------------------------------------------------------------------------
# Finding the speech
# ---------------------------------------------------------------------------


def speech_spans(path: Path) -> list[tuple[float, float]]:
    """``(start, end)`` seconds for every stretch of speech in a recording."""
    raw, error = read_wav(path)
    if error or not raw:
        return []
    frames = _vad_frames(raw) if deps.available("webrtcvad") else _energy_frames(raw)
    return _spans_from_frames(frames, _VAD_FRAME_MS / 1000.0)


def speech_segments(path: Path, track: str = TRACK_ROOM) -> list[Segment]:
    """Who spoke and when, with no words — a transcript without transcription.

    This is what the appliance produces when speech-to-text is switched off, or
    when an engine ran and recognised nothing. A record of the shape of a
    meeting — how many people spoke, for how long, in what order, and, where
    voices are enrolled, which of them was which — is genuinely useful on its
    own, and it is what somebody who turned transcription off on a slow Pi
    asked for. An empty file would be the wrong answer to that request.

    The segments carry no text. Everything downstream already copes: the
    attribution rules only look at times and tracks, and the renderer writes a
    duration where the words would go.
    """
    return [
        Segment(start=start, end=end, text="", track=track)
        for start, end in speech_spans(path)
    ]


def _vad_frames(raw: bytes) -> list[bool]:
    try:
        import webrtcvad
    except ImportError:  # pragma: no cover - probed before we get here
        return _energy_frames(raw)
    try:
        vad = webrtcvad.Vad(_VAD_AGGRESSIVENESS)
    except Exception:  # pragma: no cover - a bad build must not stop a meeting
        return _energy_frames(raw)
    step = int(SAMPLE_RATE * _VAD_FRAME_MS / 1000.0) * 2
    out: list[bool] = []
    for offset in range(0, len(raw) - step + 1, step):
        chunk = raw[offset : offset + step]
        try:
            out.append(bool(vad.is_speech(chunk, SAMPLE_RATE)))
        except Exception:
            out.append(False)
    return out


def _energy_frames(raw: bytes) -> list[bool]:
    """Speech detection with nothing but arithmetic.

    The threshold is derived from the recording's own quiet tenth rather than
    being a constant, because the noise floor of a meeting room with the air
    conditioning on is nothing like that of one without.
    """
    values = _samples(raw)
    step = int(SAMPLE_RATE * _ENERGY_FRAME_MS / 1000.0)
    if step <= 0 or not values:
        return []
    energies: list[float] = []
    for offset in range(0, len(values) - step + 1, step):
        window = values[offset : offset + step]
        energies.append(math.sqrt(sum(v * v for v in window) / step))
    if not energies:
        return []
    ranked = sorted(energies)
    floor = ranked[min(len(ranked) - 1, len(ranked) // 20)]
    loud = ranked[min(len(ranked) - 1, len(ranked) * 4 // 5)]
    # Three times the noise floor, except that the floor is only an estimate of
    # the quiet: in a recording that is almost all speech, the quietest frames
    # are speech too and the estimate comes out far too high. Capping the
    # threshold at half the loud level keeps such a recording detectable, and
    # the absolute floor keeps a recording of near-silence from being read as a
    # room full of people.
    threshold = max(min(floor * _ENERGY_RATIO, loud * 0.5), 120.0)
    return [energy > threshold for energy in energies]


def _spans_from_frames(frames: Sequence[bool], frame_seconds: float) -> list[tuple[float, float]]:
    spans: list[tuple[float, float]] = []
    start: float | None = None
    for index, speaking in enumerate(frames):
        at = index * frame_seconds
        if speaking and start is None:
            start = at
        elif not speaking and start is not None:
            spans.append((start, at))
            start = None
    if start is not None:
        spans.append((start, len(frames) * frame_seconds))

    merged: list[tuple[float, float]] = []
    for span in spans:
        if merged and span[0] - merged[-1][1] <= _JOIN_GAP_SECONDS:
            merged[-1] = (merged[-1][0], span[1])
        else:
            merged.append(span)
    return [s for s in merged if s[1] - s[0] >= MIN_SPEECH_SECONDS]


# ---------------------------------------------------------------------------
# Making a vector
# ---------------------------------------------------------------------------


def embed_file(path: Path, start: float = 0.0, end: float = 0.0) -> tuple[list[float], str, str]:
    """``(vector, model, error)`` for one stretch of a recording."""
    if not deps.available("numpy"):
        return [], "", deps.explain("numpy")
    length = (end - start) if end > start else _duration(path)
    if length < MIN_ENROL_SECONDS:
        return [], "", (
            f"That sample is only {length:.1f} seconds long. Speak for at least "
            f"{MIN_ENROL_SECONDS:.0f} seconds — a short sample makes a profile that "
            "matches everybody."
        )
    raw, error = read_wav(path, start, min(end, start + MAX_SPEECH_SECONDS) if end else 0.0)
    if error:
        return [], "", error
    return embed_samples(raw)


def embed_samples(raw: bytes) -> tuple[list[float], str, str]:
    """``(vector, model, error)`` for raw 16-bit mono samples."""
    if not raw:
        return [], "", "There was no audio to fingerprint."
    titanet = _titanet_model()
    if titanet is not None:
        vector, error = _embed_titanet(raw, titanet)
        if vector:
            return vector, MODEL_TITANET, ""
        log_event(log, logging.WARNING, "minutes.voice_titanet_failed", error=error)

    models = _vosk_models()
    if models is not None:
        vector, error = _embed_vosk(raw, *models)
        if vector:
            return vector, MODEL_VOSK, ""
        log_event(log, logging.WARNING, "minutes.voice_vosk_failed", error=error)

    vector, error = _embed_mfcc(raw)
    if not vector:
        return [], "", error
    return vector, MODEL_MFCC, ""


def _embed_titanet(raw: bytes, model: Path) -> tuple[list[float], str]:
    """A speaker embedding from sherpa-onnx — the path worth having.

    One thread on purpose. The appliance may be holding a meeting on the same
    four cores, and a fingerprint that takes a fifth of a second instead of a
    twentieth is a trade the room will never notice; a janky video call is one
    it notices immediately.
    """
    try:
        import numpy as np
        import sherpa_onnx
    except ImportError:  # pragma: no cover - probed before we get here
        return [], "sherpa-onnx is not installed."
    try:
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(model), num_threads=1, debug=False
        )
        extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        stream = extractor.create_stream()
        stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=samples)
        stream.input_finished()
        vector = extractor.compute(stream)
    except Exception as exc:
        return [], f"The voice could not be fingerprinted ({exc.__class__.__name__})."
    try:
        values = [float(v) for v in vector]
    except (TypeError, ValueError):
        return [], "The speaker model returned something unreadable."
    return (values, "") if values else ([], "The speaker model returned nothing.")


def _embed_vosk(raw: bytes, speech: Path, speaker: Path) -> tuple[list[float], str]:
    """An x-vector from vosk's speaker model — the good path."""
    try:
        import vosk
    except ImportError:  # pragma: no cover - probed before we get here
        return [], "vosk is not installed."
    try:
        vosk.SetLogLevel(-1)
        recogniser = vosk.KaldiRecognizer(
            vosk.Model(str(speech)), SAMPLE_RATE, vosk.SpkModel(str(speaker))
        )
        recogniser.AcceptWaveform(raw)
        result = json.loads(recogniser.FinalResult() or "{}")
    except Exception as exc:
        return [], f"vosk could not fingerprint the audio ({exc.__class__.__name__})."
    vector = result.get("spk")
    if not isinstance(vector, list) or not vector:
        return [], "vosk returned no speaker vector for that audio."
    try:
        return [float(v) for v in vector], ""
    except (TypeError, ValueError):
        return [], "vosk returned a speaker vector that could not be read."


def _embed_mfcc(raw: bytes) -> tuple[list[float], str]:
    """Mean and spread of the MFCCs — the no-download fallback.

    Deliberately plain: a Hann-windowed FFT, a mel filterbank, a DCT, then the
    mean and standard deviation of each coefficient across the segment. It
    describes the voice *and* the room it was recorded in, which is why it can
    separate two people in one meeting but should not be trusted to recognise
    the same person next week.
    """
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - probed before we get here
        return [], "numpy is not installed."
    try:
        signal = np.frombuffer(raw, dtype="<i2").astype(np.float64)
        if signal.size < SAMPLE_RATE // 2:
            return [], "There was not enough audio to fingerprint."
        signal = signal / 32768.0
        # Pre-emphasis lifts the high frequencies that carry most of the
        # speaker-specific detail and that a far-field microphone loses first.
        signal = np.append(signal[0], signal[1:] - 0.97 * signal[:-1])

        frame_length = int(0.025 * SAMPLE_RATE)
        hop = int(0.010 * SAMPLE_RATE)
        count = 1 + (signal.size - frame_length) // hop
        if count < 4:
            return [], "There was not enough audio to fingerprint."
        indices = np.arange(frame_length)[None, :] + hop * np.arange(count)[:, None]
        frames = signal[indices] * np.hanning(frame_length)

        spectrum = np.abs(np.fft.rfft(frames, 512)) ** 2 / 512
        bank = _mel_filterbank(np, 26, 512, SAMPLE_RATE)
        energies = np.maximum(spectrum @ bank.T, 1e-10)
        coefficients = _dct(np, np.log(energies))[:, 1:14]

        vector = np.concatenate([coefficients.mean(axis=0), coefficients.std(axis=0)])
        if not np.all(np.isfinite(vector)):
            return [], "The audio produced an unusable fingerprint."
        return [float(v) for v in vector], ""
    except Exception as exc:  # pragma: no cover - arithmetic must not stop a meeting
        return [], f"The audio could not be fingerprinted ({exc.__class__.__name__})."


def _mel_filterbank(np: Any, count: int, fft_size: int, rate: int) -> Any:
    def to_mel(hz: float) -> float:
        return 2595.0 * math.log10(1.0 + hz / 700.0)

    def to_hz(mel: float) -> float:
        return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)

    points = np.linspace(to_mel(0.0), to_mel(rate / 2.0), count + 2)
    bins = np.floor((fft_size + 1) * np.array([to_hz(m) for m in points]) / rate).astype(int)
    bank = np.zeros((count, fft_size // 2 + 1))
    for index in range(1, count + 1):
        left, middle, right = bins[index - 1], bins[index], bins[index + 1]
        for k in range(left, min(middle, bank.shape[1])):
            if middle > left:
                bank[index - 1, k] = (k - left) / (middle - left)
        for k in range(middle, min(right, bank.shape[1])):
            if right > middle:
                bank[index - 1, k] = (right - k) / (right - middle)
    return bank


def _dct(np: Any, values: Any) -> Any:
    """Type-II DCT, written out so scipy is not needed for thirteen numbers."""
    length = values.shape[1]
    grid = np.arange(length)
    basis = np.cos(np.pi / length * (grid[None, :] + 0.5) * grid[:, None])
    return values @ basis.T


def _duration(path: Path) -> float:
    try:
        with contextlib.closing(wave.open(str(path), "rb")) as handle:
            rate = handle.getframerate() or SAMPLE_RATE
            return handle.getnframes() / float(rate)
    except (wave.Error, OSError, EOFError):
        return 0.0


# ---------------------------------------------------------------------------
# Labelling a meeting
# ---------------------------------------------------------------------------


def label_room_segments(
    directory: Path,
    segments: Sequence[Segment],
    people: PeopleStore,
    config: Any,
    *,
    room_people: Iterable[str] = (),
) -> tuple[dict[int, tuple[str, str, float]], str]:
    """Work out who spoke each in-room line.

    Returns ``{segment index: (name, person_id, score)}`` plus a sentence for
    the meeting's notices. An entry with an empty ``person_id`` is a generated
    label — "Room speaker 2" — which keeps two unidentified people apart
    without pretending to know either of them.

    ``room_people`` narrows the search to the profiles the camera saw in the
    room. Comparing a voice against three colleagues instead of the whole
    company is both quicker and markedly more accurate, which is the main
    practical reason the two recognisers are worth having together.
    """
    wav = directory / "room.wav"
    if not wav.exists():
        return {}, ""
    usable, why = available(config)
    if not usable:
        return {}, why

    indexed = [
        (index, segment)
        for index, segment in enumerate(segments)
        if not segment.is_remote and segment.duration >= MIN_SPEECH_SECONDS
    ]
    if not indexed:
        return {}, ""

    threshold = float(config.float_("MINUTES_VOICE_THRESHOLD"))
    wanted = model_name()
    naming = can_name_people()
    candidates = _candidates(people, room_people)

    labels: dict[int, tuple[str, str, float]] = {}
    unmatched: list[tuple[int, list[float]]] = []
    failures = 0

    for index, segment in indexed:
        end = min(segment.end, segment.start + MAX_SPEECH_SECONDS)
        raw, error = read_wav(wav, segment.start, end)
        if error or not raw:
            failures += 1
            continue
        vector, model, error = embed_samples(raw)
        if error or model != wanted:
            failures += 1
            continue
        match = (
            people.match(
                KIND_VOICE, model, vector, threshold=threshold, candidates=candidates
            )
            if naming
            else None
        )
        if match is not None and match.ok:
            labels[index] = (match.name, match.person_id, match.score)
        else:
            unmatched.append((index, vector))

    labels.update(_cluster(unmatched))

    named = sum(1 for value in labels.values() if value[1])
    note = ""
    if not naming and labels:
        note = (
            "Voices in the room were told apart but not named: "
            + (
                "no speaker model is installed."
                if wanted == MODEL_MFCC
                else "speech is being found by loudness alone, which is too "
                "rough to identify anybody from."
            )
            + " See the Minutes page for what to install."
        )
    elif failures and not labels:
        note = "No voice in the room could be fingerprinted."
    log_event(
        log, logging.INFO, "minutes.voices_labelled",
        segments=len(indexed), named=named, clustered=len(labels) - named,
        model=wanted, failures=failures,
    )
    return labels, note


def _candidates(people: PeopleStore, room_people: Iterable[str]) -> list[Any] | None:
    ids = [str(pid) for pid in room_people if str(pid or "").strip()]
    if not ids:
        return None
    found = [people.get(pid) for pid in ids]
    narrowed = [person for person in found if person is not None and person.knows_voice()]
    return narrowed or None


def _cluster(unmatched: Sequence[tuple[int, list[float]]]) -> dict[int, tuple[str, str, float]]:
    """Group the voices nobody recognised, so at least they are kept apart."""
    from .people import cosine, normalise

    clusters: list[list[tuple[int, list[float]]]] = []
    for index, vector in unmatched:
        unit = normalise(vector)
        placed = False
        for cluster in clusters:
            best = max(cosine(unit, normalise(other)) for _, other in cluster)
            if best >= CLUSTER_THRESHOLD:
                cluster.append((index, unit))
                placed = True
                break
        if not placed:
            clusters.append([(index, unit)])

    out: dict[int, tuple[str, str, float]] = {}
    # Biggest first, so "Room speaker 2" is the person who talked most. The
    # numbering starts at 2 because an unlabelled line already reads "Room
    # speaker", and a room where one person is "Room speaker" and another is
    # "Room speaker 1" reads like a bug.
    ordered = sorted(clusters, key=len, reverse=True)
    number = 2
    for cluster in ordered:
        if len(cluster) < MIN_CLUSTER_SEGMENTS:
            continue
        for index, _ in cluster:
            out[index] = (f"Room speaker {number}", "", 0.0)
        number += 1
    return out


def learn_from_segments(
    directory: Path,
    segments: Sequence[Segment],
    people: PeopleStore,
    person_id: str,
) -> tuple[bool, str]:
    """Add the voice from some already-labelled segments to a profile.

    This is what runs when somebody reads a transcript and says who "Room
    speaker 2" was. The longest segment is used: a fingerprint of six seconds of
    continuous speech is worth several of one second.
    """
    wav = directory / "room.wav"
    if not wav.exists():
        return False, "The recording has been deleted, so the voice cannot be learned."
    usable = [s for s in segments if not s.is_remote and s.duration >= MIN_ENROL_SECONDS]
    if not usable:
        return False, (
            "None of those lines is long enough to learn a voice from — "
            f"{MIN_ENROL_SECONDS:.0f} seconds of continuous speech is the minimum."
        )
    longest = max(usable, key=lambda s: s.duration)
    end = min(longest.end, longest.start + MAX_SPEECH_SECONDS)
    raw, error = read_wav(wav, longest.start, end)
    if error or not raw:
        return False, error or "That part of the recording could not be read."
    vector, model, error = embed_samples(raw)
    if error:
        return False, error
    return people.add_vector(
        person_id, KIND_VOICE, model, vector,
        note="learned from a corrected transcript", automatic=True,
    )
