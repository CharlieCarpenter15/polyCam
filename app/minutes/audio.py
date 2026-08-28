"""Getting the meeting onto disk, and keeping it there when the audio stack blinks.

Two tracks, recorded as two separate processes:

*The room track* is the conference bar's microphone — the people physically in
the room. *The far-end track* is the monitor of whatever sink the room is
playing through, which is everyone dialled in. Recording them separately is the
one fact this whole feature can be certain of afterwards: a voice on the far-end
track was on the call, a voice on the room track was in the room, and no amount
of clever attribution can be as reliable as that.

``parecord`` does the recording, for three reasons. It is the only recorder the
appliance is guaranteed to have — ``scripts/install.sh`` installs
``pulseaudio-utils`` for ``pactl`` already. It speaks the same device names
``poly_service`` uses, so ``pactl get-default-sink`` plus ``.monitor`` is the
whole of device discovery. And it works unchanged on PulseAudio and on
PipeWire's PulseAudio server, which is what Raspberry Pi OS Bookworm actually
runs. It is invoked as ``parecord`` *and* passed ``--file-format=wav``: the same
binary called ``parec`` writes headerless PCM that every transcriber rejects,
and a headerless blob is a silent failure — the recording looks fine until an
hour later when nothing can read it.

**Everything here assumes the recording will be interrupted.** Restarting
PipeWire, or the desktop session, or an apt upgrade, kills every client: the
``parecord`` process exits and the WAV is truncated where it stood, with nobody
told. Worse, when the default sink changes mid-meeting — which
``PolyService.apply_defaults()`` causes every time the bar is re-plugged — the
far-end stream stays pinned to the *old* node and goes silent with no error at
all. So a supervisor thread watches both processes every second, restarts a dead
one into a numbered continuation file, follows the default sink when it moves,
and ``stop()`` stitches the pieces back into one WAV. A meeting recorded through
a PipeWire hiccup should still transcribe; that robustness is the point of this
module, not a nicety.

Nothing here raises. A recorder that cannot start reports why, and the meeting
carries on being a meeting.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..logging_setup import get_logger, log_event
from ..system_service import run
from . import deps, paths

log = get_logger("minutes.audio")

#: The format every engine in ``transcribe.py`` wants, so it is the only format
#: written: 16 kHz mono signed 16-bit. Nothing downstream has to resample.
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_FORMAT = "s16le"
SAMPLE_WIDTH = 2
FRAME_BYTES = SAMPLE_WIDTH * CHANNELS

#: What the recorder processes call themselves. ``poly_service`` re-points every
#: live stream at the new default device when the bar is re-plugged, and it
#: recognises ours by these names and leaves them alone — moving the far-end
#: recorder onto the room microphone would silently record the wrong thing.
CLIENT_NAME = "room-minutes"
STREAM_ROOM = "room-minutes-room"
STREAM_FAR_END = "room-minutes-farend"

ROOM_WAV = "room.wav"
FAR_END_WAV = "farend.wav"

#: Refuse to start below the first figure, abandon a running recording below the
#: second. A full ``var/`` breaks the config file, the calendar cache and the
#: browser profile — the room itself stops working — and this feature is
#: optional. It gets to lose.
DISK_FLOOR_START_PERCENT = 15.0
DISK_FLOOR_STOP_PERCENT = 5.0

#: How often the supervisor looks at its children. A second is frequent enough
#: that a crash costs a second of audio, and cheap enough that nobody notices.
SUPERVISOR_TICK_SECONDS = 1.0

#: Give up restarting after this many deaths. Something is properly broken, and
#: a restart loop would fill the disk with fragments of nothing.
MAX_RESTARTS = 20

#: Mock recordings are a placeholder, not a simulation: long enough to be a
#: valid file the rest of the pipeline can read, short enough to cost nothing.
MOCK_MAX_SECONDS = 30.0

_ROOM = "room"
_FAR_END = "far-end"


@dataclass
class Capture:
    """What one recording produced, and anything worth telling a person about."""

    room_wav: Path | None = None
    far_end_wav: Path | None = None
    seconds: float = 0.0
    notices: list[str] = field(default_factory=list)

    @property
    def tracks(self) -> list[Path]:
        return [p for p in (self.room_wav, self.far_end_wav) if p is not None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "room_wav": str(self.room_wav) if self.room_wav else "",
            "far_end_wav": str(self.far_end_wav) if self.far_end_wav else "",
            "seconds": round(self.seconds, 1),
            "notices": list(self.notices),
        }


# ---------------------------------------------------------------------------
# Can we record at all?
# ---------------------------------------------------------------------------


def available(config: Any) -> tuple[bool, str]:
    """Could a recording start right now, and if not why not.

    Deliberately cheap: it asks the cached capability probes and the
    configuration, and never shells out. The Settings page calls it on every
    refresh, and ``pactl`` costs a process each time.
    """
    try:
        if not (
            _flag(config, "MINUTES_RECORD_ROOM") or _flag(config, "MINUTES_RECORD_FAR_END")
        ):
            return False, (
                "Neither the room microphone nor the room’s own output is being "
                "recorded, so there is nothing to write down."
            )
        if _dev_mode(config):
            return True, (
                "Development mode: recordings are silent placeholder files, not "
                "the room."
            )
        recorder = _recorder_probe()
        if not recorder.ok:
            return False, recorder.detail or (
                "“parecord” was not found on PATH (apt install pulseaudio-utils)."
            )
        pactl = deps.probe("pactl")
        if not pactl.ok:
            return False, pactl.detail or (
                "“pactl” was not found on PATH (apt install pulseaudio-utils)."
            )
        return True, ""
    except Exception:  # pragma: no cover - a status call must never fail
        log.exception("minutes.audio.available_failed")
        return False, "The recorder could not be checked."


def devices(config: Any) -> dict[str, Any]:
    """What a recording started now would actually record from.

    This one *does* shell out — it is the diagnostic that answers “which
    microphone, which speaker” on the Settings page, and there is no way to
    answer it without asking the audio server.
    """
    try:
        mock = _mock_mode(config)
        payload: dict[str, Any] = {
            "mock": mock,
            "format": {
                "rate": SAMPLE_RATE,
                "channels": CHANNELS,
                "encoding": SAMPLE_FORMAT,
            },
            "client_name": CLIENT_NAME,
        }
        if mock:
            why = _mock_reason(config)
            payload["room"] = {
                "enabled": _flag(config, "MINUTES_RECORD_ROOM"),
                "device": "(silence)",
                "stream": STREAM_ROOM,
                "notice": why,
            }
            payload["far_end"] = {
                "enabled": _flag(config, "MINUTES_RECORD_FAR_END"),
                "device": "(silence)",
                "stream": STREAM_FAR_END,
                "sink": "",
                "notice": why,
            }
            return payload

        sources = _source_names()
        room_device, room_notice = _resolve_room(config, sources)
        far_device, sink, far_notice = _resolve_far_end(sources)
        payload["room"] = {
            "enabled": _flag(config, "MINUTES_RECORD_ROOM"),
            "device": room_device,
            "stream": STREAM_ROOM,
            "notice": room_notice,
        }
        payload["far_end"] = {
            "enabled": _flag(config, "MINUTES_RECORD_FAR_END"),
            "device": far_device,
            "stream": STREAM_FAR_END,
            "sink": sink,
            "notice": far_notice,
        }
        payload["sources"] = sources
        return payload
    except Exception:  # pragma: no cover - a diagnostic must never fail
        log.exception("minutes.audio.devices_failed")
        return {"mock": False, "room": {}, "far_end": {}, "error": "unreadable"}


def record_sample(config: Any, seconds: int) -> tuple[Path | None, str]:
    """One short room-microphone WAV, for enrolling somebody’s voice.

    The room’s own far-field microphone takes the sample, which is the right
    way round: it is the microphone that will have to recognise them later.
    """
    wanted = max(1, min(120, int(seconds or 0)))
    try:
        paths.ensure_dirs()
        handle, name = tempfile.mkstemp(
            prefix="voice-", suffix=".wav", dir=str(paths.MINUTES_DIR)
        )
        os.close(handle)
        target = Path(name)
    except (OSError, ValueError) as exc:
        return None, f"The sample could not be written: {exc}"

    if _mock_mode(config):
        wrote = write_silence(target, min(float(wanted), MOCK_MAX_SECONDS))
        if not wrote:
            _discard(target)
            return None, "The placeholder sample could not be written."
        log_event(log, logging.INFO, "minutes.audio.sample_simulated", seconds=wanted)
        return target, ""

    ok, why = available(config)
    if not ok:
        _discard(target)
        return None, why

    device, notice = _resolve_room(config, _source_names())
    argv = parecord_argv(_recorder_binary(), device, STREAM_ROOM, target)
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError) as exc:
        _discard(target)
        return None, f"The recorder would not start: {exc}"

    try:
        process.wait(timeout=wanted)
    except subprocess.TimeoutExpired:
        pass  # The expected path: parecord records until it is asked to stop.
    except Exception:  # pragma: no cover - defensive
        pass
    _terminate(process)

    # A recorder that was killed rather than asked leaves a header claiming no
    # audio at all, so the file is repaired before anybody tries to read it.
    repair(target)
    if _payload_bytes(target) < FRAME_BYTES * SAMPLE_RATE // 2:
        _discard(target)
        complaint = "Nothing was captured from the microphone."
        return None, f"{complaint} {notice}".strip()
    log_event(
        log, logging.INFO, "minutes.audio.sample_recorded",
        seconds=round(wav_seconds(target), 1), device=device,
    )
    return target, ""


# ---------------------------------------------------------------------------
# The recorder
# ---------------------------------------------------------------------------


@dataclass
class _Track:
    """One ``parecord`` process and the files it has written so far."""

    key: str
    label: str
    stream: str
    target: Path
    device: str = ""
    parts: list[Path] = field(default_factory=list)
    process: Any = None
    errors: Any = None
    restarts: int = 0
    dead: str = ""

    def next_part(self) -> Path:
        """``room.wav``, then ``room.1.wav``, ``room.2.wav``, …"""
        index = len(self.parts)
        if index == 0:
            return self.target
        return self.target.with_name(
            f"{self.target.stem}.{index}{self.target.suffix}"
        )


class Recorder:
    """Two ``parecord`` processes, supervised, joined back together at the end."""

    def __init__(self, config: Any, directory: Path) -> None:
        self.config = config
        self.directory = Path(directory)
        #: Overridable so tests do not have to wait a second per supervisor tick.
        self.tick_seconds = SUPERVISOR_TICK_SECONDS

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tracks: list[_Track] = []
        self._notices: list[str] = []
        self._started_at: float | None = None
        self._ended_at: float | None = None
        self._mock = False
        self._sink = ""
        self._halted = ""

    # -- state -----------------------------------------------------------
    @property
    def running(self) -> bool:
        with self._lock:
            return self._started_at is not None and self._ended_at is None

    def elapsed(self) -> float:
        with self._lock:
            if self._started_at is None:
                return 0.0
            end = self._ended_at if self._ended_at is not None else time.monotonic()
            return max(0.0, end - self._started_at)

    # -- starting --------------------------------------------------------
    def start(self, *, room: bool, far_end: bool) -> tuple[bool, str]:
        """Begin recording. Returns ``(started, why not)``; never raises."""
        try:
            return self._start(room=room, far_end=far_end)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("minutes.audio.start_failed")
            return False, f"The recorder could not start: {exc}"

    def _start(self, *, room: bool, far_end: bool) -> tuple[bool, str]:
        with self._lock:
            if self._started_at is not None:
                return False, "This recorder has already been used."
        if not (room or far_end):
            return False, (
                "Neither the room microphone nor the room’s own output is being "
                "recorded, so there is nothing to write down."
            )

        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            return False, f"The recording directory could not be made: {exc}"

        free = disk_free_percent()
        if free is not None and free < DISK_FLOOR_START_PERCENT:
            reason = (
                f"Not enough space to record: {free:.0f}% of the disk is free and "
                f"recording needs at least {DISK_FLOOR_START_PERCENT:.0f}%."
            )
            log_event(log, logging.WARNING, "minutes.audio.disk_low", free=free)
            return False, reason

        self._mock = _mock_mode(self.config)
        tracks: list[_Track] = []
        if room:
            tracks.append(
                _Track(
                    key=_ROOM,
                    label="the room microphone",
                    stream=STREAM_ROOM,
                    target=self.directory / ROOM_WAV,
                )
            )
        if far_end:
            tracks.append(
                _Track(
                    key=_FAR_END,
                    label="the room’s own output",
                    stream=STREAM_FAR_END,
                    target=self.directory / FAR_END_WAV,
                )
            )

        if self._mock:
            return self._start_mock(tracks)

        ok, why = available(self.config)
        if not ok:
            return False, why

        sources = _source_names()
        for track in tracks:
            if track.key == _ROOM:
                track.device, notice = _resolve_room(self.config, sources)
            else:
                track.device, self._sink, notice = _resolve_far_end(sources)
            if notice:
                self._note(notice)

        started: list[_Track] = []
        for track in tracks:
            error = self._spawn(track, "recording started")
            if error:
                for other in started:
                    _terminate(other.process)
                return False, error
            started.append(track)

        with self._lock:
            self._tracks = started
            self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._supervise, name="minutes-recorder", daemon=True
        )
        self._thread.start()
        log_event(
            log, logging.INFO, "minutes.audio.started",
            room=room, far_end=far_end,
            devices=",".join(t.device for t in started),
        )
        return True, ""

    def _start_mock(self, tracks: list[_Track]) -> tuple[bool, str]:
        """Write a silent placeholder per track so the rest of the pipeline runs.

        Mirrors ``PolyService._mock_status()``: development mode fabricates a
        *plausible* answer rather than an empty one, because an empty one makes
        every layer above it untestable.
        """
        for track in tracks:
            if not write_silence(track.target, 1.0):
                return False, "The placeholder recording could not be written."
            track.parts.append(track.target)
        with self._lock:
            self._tracks = tracks
            self._started_at = time.monotonic()
        self._note(_mock_reason(self.config))
        log_event(
            log, logging.INFO, "minutes.audio.simulated",
            tracks=len(tracks), dev_mode=_dev_mode(self.config),
        )
        return True, ""

    def _spawn(self, track: _Track, why: str) -> str:
        """Start one ``parecord``. Returns "" or a reason it would not start."""
        target = track.next_part()
        argv = parecord_argv(_recorder_binary(), track.device, track.stream, target)
        # stderr goes to a temporary file rather than a pipe: nobody is reading
        # a pipe for the length of a meeting, and a full pipe buffer would wedge
        # the recorder. A file has no such limit and answers “why did it die”.
        errors = None
        try:
            errors = tempfile.TemporaryFile()
        except OSError:
            errors = None
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=errors or subprocess.DEVNULL,
            )
        except (OSError, ValueError) as exc:
            _close(errors)
            return f"The recorder would not start: {exc}"
        track.process = process
        track.errors = errors
        track.parts.append(target)
        log_event(
            log, logging.INFO, "minutes.audio.track_started",
            track=track.key, device=track.device, file=target.name, reason=why,
        )
        return ""

    # -- supervision -----------------------------------------------------
    def _supervise(self) -> None:
        """Watch the children until ``stop()``. Never dies of an exception."""
        while not self._stop.is_set():
            if self._stop.wait(self.tick_seconds):
                break
            try:
                self._tick()
            except Exception:  # pragma: no cover - a wedged supervisor is worse
                log.exception("minutes.audio.tick_failed")

    def _tick(self) -> None:
        if self._halted:
            return
        if self._guard_disk():
            return
        if self._guard_length():
            return
        self._follow_default_sink()
        self._restart_the_dead()

    def _guard_disk(self) -> bool:
        free = disk_free_percent()
        if free is None or free >= DISK_FLOOR_STOP_PERCENT:
            return False
        log_event(log, logging.WARNING, "minutes.audio.disk_exhausted", free=free)
        self._halt(
            f"Recording stopped early: only {free:.0f}% of the disk was left, and "
            "the room needs that space more than this meeting does."
        )
        return True

    def _guard_length(self) -> bool:
        """A backstop for the service’s own limit, in case its thread wedges."""
        limit = _number(self.config, "MINUTES_MAX_MEETING_MINUTES", 240)
        if limit <= 0 or self.elapsed() <= limit * 60:
            return False
        log_event(log, logging.WARNING, "minutes.audio.length_limit", minutes=limit)
        self._halt(
            f"Recording stopped after {limit:g} minutes, which is the limit set "
            "in Settings."
        )
        return True

    def _follow_default_sink(self) -> None:
        """Follow the default sink when it moves, or the far end goes silent.

        When a record stream connects, the server pins it to one concrete node.
        So if the bar is re-plugged — and ``PolyService.apply_defaults()`` sets a
        new default sink every time it is — the far-end recorder keeps happily
        recording a device nothing plays through any more. No error, no exit, no
        truncation: just an hour of silence nobody notices until the transcript
        has no remote speakers in it. The only defence is to keep asking.
        """
        track = self._track(_FAR_END)
        if track is None or track.dead:
            return
        sink = _default_sink()
        if not sink or not _usable_sink(sink) or sink == self._sink:
            return
        previous, self._sink = self._sink, sink
        monitor = f"{sink}.monitor"
        if monitor not in _source_names():
            monitor = "@DEFAULT_MONITOR@"
        track.device = monitor
        log_event(
            log, logging.WARNING, "minutes.audio.sink_changed",
            was=previous, now=sink, device=monitor,
        )
        self._note(
            f"The room’s speaker changed to “{sink}” during the meeting, so the "
            "far-end recording was moved across to it."
        )
        _terminate(track.process)
        track.process = None
        self._restart(track, "the default speaker changed")

    def _restart_the_dead(self) -> None:
        for track in list(self._tracks):
            if track.dead or track.process is None:
                continue
            try:
                code = track.process.poll()
            except Exception:  # pragma: no cover - defensive
                code = -1
            if code is None:
                continue
            detail = _stderr_tail(track.errors)
            log_event(
                log, logging.WARNING, "minutes.audio.track_died",
                track=track.key, code=code, detail=detail,
            )
            self._note(
                f"The recorder for {track.label} stopped unexpectedly and was "
                "restarted; the meeting is in several pieces and has been joined "
                "back together."
            )
            self._restart(track, f"the recorder exited with code {code}")

    def _restart(self, track: _Track, why: str) -> None:
        _close(track.errors)
        track.errors = None
        track.process = None
        track.restarts += 1
        if track.restarts > MAX_RESTARTS:
            track.dead = why
            self._note(
                f"The recorder for {track.label} would not stay up, so that track "
                "stops here."
            )
            log_event(
                log, logging.ERROR, "minutes.audio.track_abandoned",
                track=track.key, restarts=track.restarts,
            )
            return
        error = self._spawn(track, why)
        if error:
            track.dead = error
            self._note(f"The recorder for {track.label} could not restart: {error}")

    def _halt(self, reason: str) -> None:
        """Stop the children early but leave the files for ``stop()`` to join."""
        self._halted = reason
        self._note(reason)
        for track in list(self._tracks):
            _terminate(track.process)
            track.process = None

    # -- stopping --------------------------------------------------------
    def stop(self) -> Capture:
        """Stop, join the pieces, and report what was captured. Never raises."""
        try:
            return self._stop_now()
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("minutes.audio.stop_failed")
            return Capture(
                seconds=self.elapsed(),
                notices=[*self._notices, f"The recording could not be closed: {exc}"],
            )

    def _stop_now(self) -> Capture:
        with self._lock:
            if self._started_at is None:
                return Capture(notices=list(self._notices))
            if self._ended_at is None:
                self._ended_at = time.monotonic()
            tracks = list(self._tracks)

        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._thread = None

        for track in tracks:
            _terminate(track.process)
            track.process = None
            _close(track.errors)
            track.errors = None

        seconds = self.elapsed()
        capture = Capture(seconds=seconds)
        for track in tracks:
            if self._mock:
                # Give the placeholder a plausible length, so a development
                # transcript has something to hang timestamps on.
                write_silence(track.target, max(1.0, min(seconds, MOCK_MAX_SECONDS)))
            path = self._finish(track)
            if track.key == _ROOM:
                capture.room_wav = path
            else:
                capture.far_end_wav = path
        capture.notices = list(self._notices)
        log_event(
            log, logging.INFO, "minutes.audio.stopped",
            seconds=round(seconds), room=bool(capture.room_wav),
            far_end=bool(capture.far_end_wav), notices=len(capture.notices),
        )
        return capture

    def _finish(self, track: _Track) -> Path | None:
        """Join a track’s pieces into its one file, or report why not."""
        parts = [p for p in track.parts if p.exists()]
        if not parts:
            self._note(f"Nothing was recorded from {track.label}.")
            return None
        # More than one piece, or a first piece that never appeared at all —
        # either way what survives has to be moved into the expected name, or a
        # recording that exists would be reported as a meeting nobody recorded.
        if len(parts) > 1 or parts[0] != track.target:
            frames, error = concatenate(track.target, parts)
            if error:
                self._note(
                    f"The pieces of {track.label} could not be joined: {error}"
                )
                return track.target if _payload_bytes(track.target) else None
            log_event(
                log, logging.INFO, "minutes.audio.joined",
                track=track.key, pieces=len(parts),
                seconds=round(frames / SAMPLE_RATE, 1),
            )
            if len(parts) > 1:
                self._note(
                    f"{len(parts)} pieces of {track.label} were joined into one "
                    "recording."
                )
        else:
            repair(track.target)
        if _payload_bytes(track.target) < FRAME_BYTES:
            self._note(f"Nothing was recorded from {track.label}.")
            return None
        return track.target

    # -- small helpers ---------------------------------------------------
    def _track(self, key: str) -> _Track | None:
        for track in self._tracks:
            if track.key == key:
                return track
        return None

    def _note(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            if text not in self._notices:
                self._notices.append(text)

    @property
    def notices(self) -> list[str]:
        with self._lock:
            return list(self._notices)


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


def parecord_argv(binary: str, device: str, stream: str, target: Path) -> list[str]:
    """The exact command line, in one place, because every flag matters.

    ``--file-format=wav`` is not redundant with invoking ``parecord``: the
    binary picks its default from the name it was called as, and one wrong
    symlink would produce headerless PCM that reads as an hour of noise.
    """
    return [
        binary,
        f"--device={device}",
        "--file-format=wav",
        f"--format={SAMPLE_FORMAT}",
        f"--rate={SAMPLE_RATE}",
        f"--channels={CHANNELS}",
        f"--client-name={CLIENT_NAME}",
        f"--stream-name={stream}",
        str(target),
    ]


def _resolve_room(config: Any, sources: list[str]) -> tuple[str, str]:
    """Which source is the room microphone, and anything odd about the answer."""
    preference = _text(config, "MICROPHONE_DEVICE")
    if preference and preference.lower() not in ("", "auto"):
        if not sources or preference in sources:
            return preference, ""
        notice = (
            f"The microphone set in Settings, “{preference}”, is not one of the "
            "devices this machine lists, so the default microphone was recorded "
            "instead."
        )
        return _default_source_device(sources), notice
    device = _default_source_device(sources)
    if device == "@DEFAULT_SOURCE@":
        return device, (
            "No microphone has been chosen, so whatever the system decides is "
            "the default was recorded."
        )
    return device, ""


def _default_source_device(sources: list[str]) -> str:
    """The default source’s name, or the server-resolved alias for it."""
    name = _default_device("source")
    if not name or name.startswith("@") or name.startswith("auto_null"):
        return "@DEFAULT_SOURCE@"
    if sources and name not in sources:
        return "@DEFAULT_SOURCE@"
    return name


def _resolve_far_end(sources: list[str]) -> tuple[str, str, str]:
    """``(device, sink, notice)`` for the far-end track.

    A sink’s monitor is always ``<sink>.monitor`` — the PulseAudio server builds
    the name that way and strips it back the same way — but the sink itself may
    not be a real one. With nothing chosen the server answers with the literal
    ``@DEFAULT_SINK@``, and with no hardware at all it invents ``auto_null``;
    appending ``.monitor`` to either produces a device that does not exist.
    """
    sink = _default_sink()
    if not _usable_sink(sink):
        return "@DEFAULT_MONITOR@", "", (
            "No speaker has been chosen yet, so the far-end recording follows "
            "whatever the system is playing through."
        )
    monitor = f"{sink}.monitor"
    if sources and monitor not in sources:
        return "@DEFAULT_MONITOR@", sink, (
            f"The monitor of “{sink}” is not listed as a recordable source, so "
            "the far-end recording follows the system default instead."
        )
    return monitor, sink, ""


def _usable_sink(sink: str) -> bool:
    """Is this a real sink name, rather than an alias or the dummy one?"""
    if not sink:
        return False
    if sink.startswith("@"):
        return False
    return not sink.startswith("auto_null")


def _default_sink() -> str:
    return _default_device("sink")


def _default_device(kind: str) -> str:
    """``pactl get-default-source`` / ``-sink``, the way ``poly_service`` asks."""
    result = run(["pactl", f"get-default-{kind}"], timeout=8)
    return result.stdout.strip() if result.ok else ""


def _source_names() -> list[str]:
    """Every recordable source, monitors included, from ``pactl list short``."""
    result = run(["pactl", "list", "short", "sources"], timeout=10)
    if not result.ok:
        return []
    names: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[1].strip()
        if name:
            names.append(name)
    return names


# ---------------------------------------------------------------------------
# WAV files: reading them, joining them, and repairing the truncated ones
# ---------------------------------------------------------------------------


def wav_seconds(path: Path) -> float:
    """How long a WAV is, believing the file over its header.

    A recorder that was killed leaves a header that says the file is empty, so
    a length taken from the header alone would report a whole meeting as zero
    seconds and every engine downstream would skip it.
    """
    frames, width, rate, channels = _wav_header(path)
    frame = max(1, width * channels)
    if frames > 0 and rate > 0:
        return frames / float(rate)
    payload = _payload_bytes(path)
    if payload <= 0:
        return 0.0
    rate = rate or SAMPLE_RATE
    return payload / float(frame * rate)


def concatenate(target: Path, parts: list[Path]) -> tuple[int, str]:
    """Join ``parts`` into ``target``, returning ``(frames, error)``.

    ``parts[0]`` is normally ``target`` itself, so the join is written beside it
    and moved into place — never over the top of a file being read from.

    Deliberately done with the standard library. Everything else in this feature
    degrades when a tool is missing; a recording that can only be assembled when
    ffmpeg happens to be installed would be a recording that is sometimes lost.
    """
    if not parts:
        return 0, "there was nothing to join"
    temporary = target.with_name(target.name + ".joining")
    written = 0
    try:
        with contextlib.closing(wave.open(str(temporary), "wb")) as out:
            out.setnchannels(CHANNELS)
            out.setsampwidth(SAMPLE_WIDTH)
            out.setframerate(SAMPLE_RATE)
            for part in parts:
                for block in _frame_blocks(part):
                    out.writeframes(block)
                    written += len(block)
        os.replace(temporary, target)
    except (OSError, wave.Error, ValueError) as exc:
        _discard(temporary)
        return 0, str(exc)
    for part in parts:
        if part != target:
            _discard(part)
    return written // FRAME_BYTES, ""


def repair(path: Path) -> bool:
    """Rewrite a WAV whose header lies about how much audio it holds.

    ``parecord`` writes the real length when it closes cleanly. Killed, it never
    gets the chance, and leaves a header claiming zero frames over a perfectly
    good hour of audio.
    """
    frames, width, rate, channels = _wav_header(path)
    frame = max(1, width * channels)
    payload = _payload_bytes(path)
    if payload <= 0:
        return False
    if frames > 0 and frames * frame >= payload - frame:
        return False
    _frames, error = concatenate(path, [path])
    if error:
        log_event(log, logging.WARNING, "minutes.audio.repair_failed", error=error)
        return False
    log_event(
        log, logging.INFO, "minutes.audio.repaired",
        file=path.name, seconds=round(payload / float(frame * (rate or SAMPLE_RATE)), 1),
    )
    return True


def write_silence(path: Path, seconds: float) -> bool:
    """A valid, readable, silent WAV — the placeholder development mode uses."""
    frames = max(1, int(max(0.0, seconds) * SAMPLE_RATE))
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.closing(wave.open(str(path), "wb")) as out:
            out.setnchannels(CHANNELS)
            out.setsampwidth(SAMPLE_WIDTH)
            out.setframerate(SAMPLE_RATE)
            out.writeframes(b"\x00" * (frames * FRAME_BYTES))
        return True
    except (OSError, wave.Error, ValueError):
        log.exception("minutes.audio.silence_failed")
        return False


def disk_free_percent() -> float | None:
    """Free space where the recordings go, or None when it cannot be measured.

    ``shutil`` rather than ``SystemService``: this is one syscall and asking for
    a whole service to make it would tie the recorder to the appliance’s object
    graph for no benefit.

    The share is deliberately ``free / (used + free)`` and **not**
    ``free / total``. Those two figures are not on the same scale:
    ``shutil.disk_usage`` reports ``free`` as the space a non-root process may
    actually have (``f_bavail``) but ``total`` as the whole filesystem, so on
    every ext4 root — 5 % reserved for root by default — and on any thinly
    provisioned volume, ``free / total`` reads far lower than the truth. A
    machine with 30 GB genuinely free out of 40 GB in use measured as 11 % that
    way, and this guard refused to record on it.
    """
    for candidate in (paths.MINUTES_DIR, paths.MINUTES_DIR.parent, Path("/")):
        try:
            usage = shutil.disk_usage(str(candidate))
        except (OSError, ValueError):
            continue
        addressable = usage.used + usage.free
        if addressable <= 0:
            continue
        return round(100.0 * usage.free / addressable, 1)
    return None


def _wav_header(path: Path) -> tuple[int, int, int, int]:
    """``(frames, width, rate, channels)`` from the header, zeroes if unreadable."""
    try:
        with contextlib.closing(wave.open(str(path), "rb")) as handle:
            return (
                handle.getnframes(),
                handle.getsampwidth(),
                handle.getframerate(),
                handle.getnchannels(),
            )
    except (wave.Error, OSError, EOFError, ValueError):
        return 0, 0, 0, 0


def _frame_blocks(path: Path, block: int = 1 << 20) -> Iterator[bytes]:
    """Audio out of a WAV, in bounded pieces, header or no header.

    An hour of a meeting is 115 MB; reading it into memory on a Pi that is also
    running a browser is not on. The header is used when it is trustworthy, and
    the raw bytes after the ``data`` chunk when it is not.
    """
    frames, width, rate, channels = _wav_header(path)
    frame = max(1, width * channels)
    if frames > 0:
        try:
            with contextlib.closing(wave.open(str(path), "rb")) as handle:
                per_read = max(1, block // frame)
                remaining = frames
                while remaining > 0:
                    chunk = handle.readframes(min(per_read, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk) // frame
                    yield chunk
            return
        except (wave.Error, OSError, EOFError, ValueError):
            log_event(
                log, logging.WARNING, "minutes.audio.header_unreadable",
                file=path.name,
            )

    offset = _data_offset(path)
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            size = max(frame, block - (block % frame))
            while True:
                chunk = handle.read(size)
                if not chunk:
                    break
                extra = len(chunk) % frame
                if extra:
                    chunk = chunk[: len(chunk) - extra]
                if chunk:
                    yield chunk
    except OSError:
        return


def _data_offset(path: Path) -> int:
    """Where the samples start: the ``data`` chunk, or the usual 44 bytes."""
    try:
        with path.open("rb") as handle:
            head = handle.read(4096)
    except OSError:
        return 44
    if len(head) < 12 or head[0:4] != b"RIFF" or head[8:12] != b"WAVE":
        return 44
    index = 12
    while index + 8 <= len(head):
        chunk = head[index : index + 4]
        size = int.from_bytes(head[index + 4 : index + 8], "little")
        if chunk == b"data":
            return index + 8
        index += 8 + size + (size % 2)
        if size <= 0:
            break
    return 44


def _payload_bytes(path: Path) -> int:
    """How many bytes of audio the file actually holds, header excluded."""
    try:
        return max(0, path.stat().st_size - _data_offset(path))
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Odds and ends
# ---------------------------------------------------------------------------


def _recorder_probe() -> Any:
    """The capability probe for the recorder binary.

    Named ``parecord`` because that is the name it must be *invoked* as. An
    older checkout of ``deps.py`` registered it as ``parec``, so both are tried
    rather than reporting an unknown capability as a missing one.
    """
    found = deps.probe("parecord")
    if found.ok or "Unknown capability" not in (found.detail or ""):
        return found
    return deps.probe("parec")


def _recorder_binary() -> str:
    return _recorder_probe().path or "parecord"


def _mock_mode(config: Any) -> bool:
    """Is there anything real to record from?"""
    if _dev_mode(config):
        return True
    return not (_recorder_probe().ok and deps.probe("pactl").ok)


def _mock_reason(config: Any) -> str:
    """Why this recording is silence — the two reasons are not the same thing.

    A developer on a laptop expects a placeholder. An appliance in a meeting
    room producing one is a fault, and saying “development mode” to whoever
    reads that notice would send them looking in entirely the wrong place.
    """
    if _dev_mode(config):
        return "Development mode: this recording is silence, not the room."
    return (
        "No recorder is installed on this machine, so the meeting was not "
        "actually captured — install pulseaudio-utils and record it again."
    )


def _dev_mode(config: Any) -> bool:
    return _flag(config, "DEV_MODE")


def _flag(config: Any, key: str) -> bool:
    try:
        return bool(config.bool_(key))
    except Exception:  # pragma: no cover - a stub config in a test
        return False


def _text(config: Any, key: str) -> str:
    try:
        return str(config.str_(key) or "").strip()
    except Exception:  # pragma: no cover - a stub config in a test
        return ""


def _number(config: Any, key: str, fallback: int) -> int:
    try:
        return int(config.int_(key))
    except Exception:  # pragma: no cover - a stub config in a test
        return fallback


def _terminate(process: Any) -> None:
    """Ask a recorder to stop, insist if it will not. Never raises."""
    if process is None:
        return
    try:
        if process.poll() is not None:
            return
        process.terminate()
    except Exception:  # pragma: no cover - the process may already be gone
        return
    try:
        process.wait(timeout=5)
        return
    except Exception:
        pass
    try:
        process.kill()
        process.wait(timeout=2)
    except Exception:  # pragma: no cover - nothing more can be done
        pass


def _close(handle: Any) -> None:
    if handle is None:
        return
    try:
        handle.close()
    except Exception:  # pragma: no cover
        pass


def _stderr_tail(handle: Any, limit: int = 200) -> str:
    """The last thing a dead recorder complained about, for the log."""
    if handle is None:
        return ""
    try:
        handle.seek(0)
        raw = handle.read()
    except Exception:  # pragma: no cover
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return " ".join(str(raw).split())[-limit:]


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
