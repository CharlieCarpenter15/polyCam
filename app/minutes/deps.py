"""What is installed, and what to say when something is not.

Every part of this feature is optional and most of it needs something the base
appliance does not ship: a recorder binary, a speech-to-text engine, a face
model, an HTTP client for the Claude API. The rule the whole package follows is
that a missing piece is *reported*, never raised: the room screen, the calendar
and the meeting joining must behave exactly as they did before whether or not
any of this is installed.

So every capability is asked the same question — ``probe(name)`` — and answers
the same way: available or not, with a sentence a person can act on. The
Settings page and ``/api/minutes/status`` show those sentences, which is how
you find out that transcription is off because ``whisper.cpp`` was never
installed rather than because the switch is off.

Probes are cached because they shell out; ``refresh()`` clears the cache after
an install.
"""

from __future__ import annotations

import importlib.util
import shutil
import threading
from dataclasses import dataclass

#: A python module, the pip name that provides it, and what it buys you.
_MODULES: dict[str, tuple[str, str, str]] = {
    # probe name: (module, pip install name, what it is for)
    "anthropic": ("anthropic", "anthropic", "writing the meeting summary"),
    "numpy": ("numpy", "numpy", "comparing voices and faces"),
    # Plain ``opencv-python-headless``, not the contrib build: the face detector
    # and the face recogniser this uses (YuNet and SFace) live in the core
    # ``objdetect`` module, and the contrib wheel is three times the size for
    # nothing we need.
    "opencv": ("cv2", "opencv-python-headless", "finding and recognising faces"),
    "vosk": ("vosk", "vosk", "offline speech-to-text and voice fingerprints"),
    "faster_whisper": ("faster_whisper", "faster-whisper", "offline speech-to-text"),
    "webrtcvad": ("webrtcvad", "webrtcvad-wheels", "splitting audio into speech turns"),
    "soundfile": ("soundfile", "soundfile", "reading and writing audio files"),
}

#: An external program, and what it buys you.
_BINARIES: dict[str, tuple[str, str]] = {
    # Deliberately ``parecord`` and not ``parec``: they are the same binary, but
    # it picks its default output format from the name it was invoked under, and
    # ``parec`` defaults to headerless raw PCM that every transcriber rejects.
    "parecord": ("parecord", "recording audio from PulseAudio or PipeWire"),
    "pactl": ("pactl", "finding the microphone and speaker to record"),
    "ffmpeg": ("ffmpeg", "converting and trimming audio"),
    "whisper-cpp": ("whisper-cli", "offline speech-to-text"),
    "v4l2-ctl": ("v4l2-ctl", "listing the room camera"),
}

#: Alternative program names, tried in order, for probes whose binary has been
#: renamed between releases. whisper.cpp's CLI was ``main`` for years.
_BINARY_ALIASES: dict[str, tuple[str, ...]] = {
    "whisper-cpp": ("whisper-cli", "whisper-cpp", "whisper", "main"),
}


@dataclass(frozen=True)
class Probe:
    """The answer to "can we do this, and if not why not"."""

    name: str
    ok: bool
    detail: str = ""
    #: Set for a binary probe: the path that was found.
    path: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


_lock = threading.Lock()
_cache: dict[str, Probe] = {}


def probe(name: str) -> Probe:
    """Is ``name`` usable? Cached, because probing shells out."""
    with _lock:
        cached = _cache.get(name)
    if cached is not None:
        return cached
    result = _probe_uncached(name)
    with _lock:
        _cache[name] = result
    return result


def available(name: str) -> bool:
    """Shorthand for ``probe(name).ok``."""
    return probe(name).ok


def refresh() -> None:
    """Forget every cached answer. Call after installing something."""
    with _lock:
        _cache.clear()


def report() -> list[dict[str, object]]:
    """Every probe, for the Settings page and the diagnostics API."""
    names = sorted({*_MODULES, *_BINARIES})
    return [probe(name).to_dict() for name in names]


def missing(*names: str) -> list[str]:
    """The subset of ``names`` that is not available, in the order given."""
    return [name for name in names if not available(name)]


def explain(*names: str) -> str:
    """One sentence naming what is missing and how to install it.

    Empty when everything asked for is present, so a caller can write
    ``if reason := deps.explain("numpy", "opencv"): ...``.
    """
    gaps = missing(*names)
    if not gaps:
        return ""
    parts = [probe(name).detail for name in gaps]
    return " ".join(part for part in parts if part)


def _probe_uncached(name: str) -> Probe:
    if name in _MODULES:
        module, pip_name, purpose = _MODULES[name]
        if importlib.util.find_spec(module) is not None:
            return Probe(name, True)
        return Probe(
            name,
            False,
            f"“{pip_name}” is not installed, so {purpose} is unavailable. "
            f"Install it with: pip install {pip_name}",
        )
    if name in _BINARIES:
        default_binary, purpose = _BINARIES[name]
        for candidate in _BINARY_ALIASES.get(name, (default_binary,)):
            found = shutil.which(candidate)
            if found:
                return Probe(name, True, path=found)
        return Probe(
            name,
            False,
            f"“{default_binary}” was not found on PATH, so {purpose} is unavailable.",
        )
    return Probe(name, False, f"Unknown capability “{name}”.")


def binary_path(name: str) -> str:
    """The path a binary probe found, or "" when it found nothing."""
    return probe(name).path


def set_probe_for_tests(name: str, ok: bool, detail: str = "", path: str = "") -> None:
    """Pretend ``name`` is (or is not) installed.

    Tests need to exercise both branches of every degradation path on a machine
    that has none of this installed, and a real appliance never calls this.
    """
    with _lock:
        _cache[name] = Probe(name, ok, detail, path)
