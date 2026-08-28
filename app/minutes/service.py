"""The part that decides when to record, and what to do afterwards.

This is the only module in the package that knows about the rest of the
appliance, and it knows about it in one direction only: it *reads* the room's
state and never writes to it. There is no hook in ``meeting_service.py``, no
callback registered on the browser, nothing added to the room's state machine.
A thread here looks at ``room.state()`` every couple of seconds and works out
for itself when a meeting started and when it ended.

That is a deliberately dull design, and it is the point. The room screen, the
calendar and the meeting joining are what this appliance is for; a feature that
writes up meetings is worth having only for as long as it cannot break them. A
reader that polls can be wrong about the odd second at a meeting boundary. A
hook that throws takes the room down.

Two threads:

*The supervisor* runs every couple of seconds. It starts and stops recordings,
and between meetings it lets the camera look at the room.

*The worker* takes a finished recording and does the slow, failure-prone part —
transcribe, work out who spoke, ask Claude for a summary, send the email — one
session at a time, off the supervisor, so a wedged transcription cannot stop the
next meeting from being recorded.

Everything is inert until ``MINUTES_ENABLED`` is on. With it off, ``start()``
starts no threads, opens no devices and writes no files.
"""

from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..background_service import detect_image_type
from ..logging_setup import get_logger, log_event
from ..store import read_json, write_json
from . import attribute, audio, deps, faces, mailer, paths, roster, summarize, transcribe, voiceprint
from .people import KIND_FACE, KIND_VOICE, PeopleStore
from .transcript import (
    SOURCE_MANUAL,
    Participant,
    SessionMeta,
    Transcript,
    new_session_id,
)

log = get_logger("minutes")

#: How often the supervisor looks at the room.
TICK_SECONDS = 2.0

#: Stages a session moves through, in order. Kept as plain strings because they
#: are shown to a person in the web page and stored in a file that outlives any
#: particular version of this code.
STAGE_RECORDING = "recording"
STAGE_CAPTURED = "captured"
STAGE_TRANSCRIBED = "transcribed"
STAGE_SUMMARISED = "summarised"
STAGE_SENT = "sent"
STAGE_FAILED = "failed"
STAGE_DISCARDED = "discarded"

#: A stage that means the worker still owes this session something.
UNFINISHED = (STAGE_RECORDING, STAGE_CAPTURED)

#: How long before a meeting is due to start the camera is left alone, so that
#: a sweep can never be holding it when the browser asks for it. Comfortably
#: more than the appliance's own early-join window.
CAMERA_QUIET_BEFORE = 180.0

#: How long to wait after a meeting before looking at the room again. Chromium
#: releases a camera asynchronously, so "the meeting has closed" is not the same
#: as "the camera is free".
CAMERA_SETTLE_AFTER = 8.0

#: Sweep expired sessions this often. Retention is measured in days, so once
#: every few hours is plenty and keeps the work off the meeting path.
SWEEP_SECONDS = 6 * 3600


class Recording:
    """One meeting currently being captured."""

    def __init__(self, meta: SessionMeta, directory: Path) -> None:
        self.meta = meta
        self.dir = directory
        self.started = datetime.now(timezone.utc)
        self.recorder: Any = None
        self.sampler: Any = None
        #: Who the camera saw in the room shortly before this meeting began.
        self.room_people: list[dict[str, Any]] = []

    @property
    def seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.started).total_seconds()


class MinutesService:
    """Records meetings, writes them up, and sends them out."""

    def __init__(self, config: Any, calendar: Any, room: Any, poly: Any, browser: Any) -> None:
        self.config = config
        self.calendar = calendar
        self.room = room
        self.poly = poly
        self.browser = browser

        self.people = PeopleStore()

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._supervisor: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._recording: Recording | None = None
        self._last_room_look: dict[str, Any] = {}
        self._last_look_at: datetime | None = None
        self._meeting_ended_at: datetime | None = None
        self._last_sweep: datetime | None = None
        #: What the worker is doing right now, for the web page.
        self._working_on: str = ""
        self._last_error: str = ""

        config.on_change(self._on_config_change)

    # -- lifecycle -------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.config.bool_("MINUTES_ENABLED"))

    def start(self) -> None:
        """Begin watching the room, if the feature is on."""
        if not self.enabled:
            log_event(log, logging.INFO, "minutes.disabled")
            return
        with self._lock:
            if self._supervisor and self._supervisor.is_alive():
                return
            paths.ensure_dirs()
            self.people.load()
            self._stop.clear()
            self._supervisor = threading.Thread(
                target=self._supervise, name="minutes-supervisor", daemon=True
            )
            self._worker = threading.Thread(
                target=self._work, name="minutes-worker", daemon=True
            )
            self._supervisor.start()
            self._worker.start()
        log_event(
            log, logging.INFO, "minutes.started",
            people=self.people.count(),
            engine=transcribe.chosen_engine_name(self.config),
        )
        self._recover_unfinished()

    def stop(self) -> None:
        """Stop cleanly, finishing any recording that is running."""
        self._stop.set()
        try:
            self._finish_recording("the appliance is shutting down")
        except Exception:  # pragma: no cover - shutdown must not raise
            log.exception("minutes.shutdown_finish_failed")
        for thread in (self._supervisor, self._worker):
            if thread and thread.is_alive():
                thread.join(timeout=5)
        with self._lock:
            self._supervisor = None
            self._worker = None

    def _on_config_change(self, values: dict[str, Any], changed: set[str]) -> None:
        if "MINUTES_ENABLED" not in changed:
            return
        if self.enabled:
            self._stop.clear()
            self.start()
        else:
            log_event(log, logging.INFO, "minutes.switched_off")
            self.stop()

    # -- the supervisor --------------------------------------------------
    def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # pragma: no cover - must never die
                log.exception("minutes.tick_failed")
            self._stop.wait(timeout=TICK_SECONDS)

    def tick(self) -> None:
        """Re-read the room and start or stop a recording to match it."""
        if not self.enabled:
            self._finish_recording("the feature was switched off")
            return

        state = self.room.state()
        active = getattr(state, "active", None)
        with self._lock:
            current = self._recording

        if active is not None:
            meeting_id = str(getattr(active, "meeting_id", ""))
            if current is None:
                self._begin_recording(active)
            elif current.meta.meeting_id != meeting_id:
                # One meeting followed straight after another.
                self._finish_recording("the next meeting started")
                self._begin_recording(active)
            elif current.seconds > self.config.int_("MINUTES_MAX_MEETING_MINUTES") * 60:
                self._finish_recording("the recording time limit was reached")
        elif current is not None:
            self._finish_recording("the meeting ended")
        else:
            self._maybe_look_at_room()

        self._maybe_sweep()

    def _begin_recording(self, active: Any) -> None:
        if not (
            self.config.bool_("MINUTES_RECORD_ROOM")
            or self.config.bool_("MINUTES_RECORD_FAR_END")
        ):
            return

        meeting_id = str(getattr(active, "meeting_id", ""))
        session_id = new_session_id()
        directory = paths.session_dir(session_id)
        if directory is None:
            return
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            log_event(log, logging.ERROR, "minutes.session_dir_failed", error=str(exc))
            return

        meeting = self._lookup_meeting(meeting_id)
        meta = SessionMeta(
            session_id=session_id,
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            meeting_id=meeting_id,
            title=str(getattr(active, "title", "")) or "Meeting",
            provider=str(getattr(active, "provider_id", "")),
            room=self.config.str_("ROOM_NAME"),
            organizer=getattr(meeting, "organizer", "") if meeting else "",
            invited=list(getattr(meeting, "attendees", []) or []) if meeting else [],
            stage=STAGE_RECORDING,
        )
        recording = Recording(meta, directory)
        recording.room_people = list(self._last_room_look.get("people") or [])
        self._write_meta(meta, directory)

        recording.recorder = audio.Recorder(self.config, directory)
        started, why = recording.recorder.start(
            room=self.config.bool_("MINUTES_RECORD_ROOM"),
            far_end=self.config.bool_("MINUTES_RECORD_FAR_END"),
        )
        if not started:
            meta.stage = STAGE_FAILED
            meta.error = why
            self._write_meta(meta, directory)
            log_event(log, logging.WARNING, "minutes.record_failed", error=why)
            return

        if self.config.bool_("MINUTES_IDENTIFY_REMOTE"):
            recording.sampler = roster.RosterSampler(self.config, self.browser)
            recording.sampler.start(meta.provider, directory)

        with self._lock:
            self._recording = recording
        log_event(
            log, logging.INFO, "minutes.recording_started",
            session=session_id, title=meta.title, provider=meta.provider,
        )

    def _finish_recording(self, reason: str) -> None:
        with self._lock:
            recording = self._recording
            self._recording = None
        if recording is None:
            return

        capture = None
        try:
            capture = recording.recorder.stop() if recording.recorder else None
        except Exception:  # pragma: no cover
            log.exception("minutes.recorder_stop_failed")
        samples = []
        try:
            samples = recording.sampler.stop() if recording.sampler else []
        except Exception:  # pragma: no cover
            log.exception("minutes.sampler_stop_failed")

        meta = recording.meta
        meta.ended_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        seconds = capture.seconds if capture else recording.seconds

        minimum = self.config.int_("MINUTES_MIN_MEETING_SECONDS")
        if seconds < minimum:
            log_event(
                log, logging.INFO, "minutes.recording_discarded",
                session=meta.session_id, seconds=round(seconds), minimum=minimum,
            )
            self._delete_session_dir(recording.dir)
            return

        write_json(
            recording.dir / "presence.json",
            {"people": recording.room_people},
            mode=0o600,
        )
        # "Three pieces were joined", "the speaker changed mid-meeting", "this
        # recording is silence" — the reader of the transcript is exactly who
        # needs to know, so these are kept rather than left in the log.
        if capture is not None and capture.notices:
            write_json(
                recording.dir / "capture.json",
                {"notices": list(capture.notices)},
                mode=0o600,
            )

        meta.stage = STAGE_CAPTURED
        self._write_meta(meta, recording.dir)
        self._meeting_ended_at = datetime.now(timezone.utc)
        log_event(
            log, logging.INFO, "minutes.recording_stopped",
            session=meta.session_id, seconds=round(seconds), reason=reason,
        )
        self._queue.put(meta.session_id)

    def _lookup_meeting(self, meeting_id: str) -> Any:
        try:
            return self.calendar.find(meeting_id)
        except Exception:  # pragma: no cover - a calendar fault is not fatal here
            return None

    # -- looking at the room ---------------------------------------------
    def _maybe_look_at_room(self) -> None:
        """Look at the room, unless a meeting is about to want the camera.

        A sweep holds the camera for about twelve seconds. The camera cannot be
        opened twice, so a meeting joining during one would find it busy — and
        between "the room screen misses a face" and "the room fails to join a
        meeting" there is no contest. So the sweep gives way twice over: it does
        not start when a meeting is due within :data:`CAMERA_QUIET_BEFORE`, and
        it waits :data:`CAMERA_SETTLE_AFTER` after a meeting ends, because
        Chromium releases a camera asynchronously and a sweep that starts the
        instant a meeting closes can still lose the race.
        """
        if not self.config.bool_("MINUTES_IDENTIFY_FACES"):
            return
        now = datetime.now(timezone.utc)

        if self._meeting_ended_at is not None:
            since = (now - self._meeting_ended_at).total_seconds()
            if since < CAMERA_SETTLE_AFTER:
                return

        if self._meeting_due_within(CAMERA_QUIET_BEFORE):
            return

        every = max(15, self.config.int_("MINUTES_ROOM_SCAN_SECONDS"))
        if self._last_look_at is not None:
            if (now - self._last_look_at).total_seconds() < every:
                return
        self._last_look_at = now
        self.look_at_room_now()

    def _meeting_due_within(self, seconds: float) -> bool:
        """Is a meeting about to start and want the camera?"""
        try:
            now = datetime.now(self.config.tz())
            _, upcoming = self.calendar.current_and_upcoming(now)
        except Exception:  # pragma: no cover - a calendar fault must not block
            return False
        for meeting in upcoming[:3]:
            if not getattr(meeting, "has_link", False):
                continue
            if 0 <= meeting.minutes_until(now) * 60.0 <= seconds:
                return True
        return False

    def look_at_room_now(self) -> dict[str, Any]:
        """Take one look through the room camera and remember who was there."""
        try:
            look = faces.look_at_room(self.config, self.people)
        except Exception as exc:  # pragma: no cover - a camera fault is not fatal
            log.exception("minutes.room_look_failed")
            return {"ok": False, "error": str(exc), "people": []}
        payload = look.to_dict()
        with self._lock:
            self._last_room_look = payload
        if look.people:
            log_event(
                log, logging.INFO, "minutes.room_look",
                seen=[p["name"] for p in look.people],
            )
        return payload

    # -- the worker ------------------------------------------------------
    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                session_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._working_on = session_id
                self.process(session_id)
            except Exception as exc:  # pragma: no cover - must never die
                log.exception("minutes.process_failed")
                self._last_error = str(exc)
            finally:
                self._working_on = ""
                self._queue.task_done()

    def process(self, session_id: str) -> tuple[bool, str]:
        """Turn a captured session into a transcript, a summary and an email.

        Safe to run again on a session that has already been through it: an
        existing transcript is reused rather than rebuilt. Transcribing is the
        slow, deterministic step and would produce the same words a second time
        — and by then the audio it was made from has usually been deleted.
        Summarising and sending are the steps worth repeating, which is what
        ``reprocess`` is for.
        """
        directory = paths.session_dir(session_id)
        if directory is None or not directory.is_dir():
            return False, "No such recording."
        meta = self._read_meta(directory)
        if meta is None:
            return False, "That recording has no meta file and cannot be processed."

        written = self._read_transcript(directory)
        if written is None:
            written, error = self._transcribe(meta, directory)
            if error and not written.segments:
                meta.stage = STAGE_FAILED
                meta.error = error
                self._write_meta(meta, directory)
                return False, error
            meta.stage = STAGE_TRANSCRIBED
            self._write_meta(meta, directory)
            self._save_transcript(directory, written)
            self._apply_audio_retention(directory)

        if self.config.bool_("MINUTES_SUMMARY_ENABLED"):
            ok, error = self._summarise(meta, directory, written)
            if not ok:
                meta.error = error
                self._write_meta(meta, directory)
                return False, error

        if self.config.bool_("MINUTES_EMAIL_ENABLED"):
            self._send(meta, directory, written)

        return True, ""

    def _transcribe(self, meta: SessionMeta, directory: Path) -> tuple[Transcript, str]:
        """Words, then who said them."""
        written = Transcript(meta=meta)
        engine = transcribe.choose_engine(self.config)
        captured = read_json(directory / "capture.json", default={}) or {}
        notices: list[str] = [
            str(note) for note in (captured.get("notices") or []) if str(note).strip()
        ]

        # When the meeting app was captioning, it has already written down every
        # remote speaker's words with their name attached, which is both more
        # accurate than transcribing the call audio here and free. Transcribing
        # the far-end track as well would only produce a second, worse copy.
        caption_segments = roster.caption_segments(directory)
        far_end_covered = len(caption_segments) >= roster.MIN_USEFUL_CAPTIONS
        if far_end_covered:
            notices.append(
                f"The remote side was taken from the meeting's own live captions "
                f"({len(caption_segments)} lines), not from the call audio."
            )

        segments, notes = transcribe.transcribe_session(
            engine, directory, self.config, skip_far_end=far_end_covered
        )
        merged = sorted(segments + caption_segments, key=lambda item: item.start)

        # The room microphone hears the speaker as well as the room, so a
        # remote sentence can arrive twice: once from the call and once as a
        # room-coloured echo a moment later. transcribe_session already removes
        # those within its own output, but it never sees the caption lines, and
        # skipping the far-end track leaves the echo as the *only* copy of that
        # speech that has not been checked.
        if caption_segments:
            merged, echoed = transcribe.drop_echoes(merged)
            if echoed:
                notices.append(
                    f"{echoed} lines the room microphone picked up from the "
                    "speaker were removed, because the captions already had them."
                )

        written.segments = merged
        notices.extend(notes)

        samples = roster.load_samples(directory)
        presence = read_json(directory / "presence.json", default={}) or {}
        room_people = presence.get("people") if isinstance(presence, dict) else []

        voice_labels: dict[int, tuple[str, str, float]] = {}
        if self.config.bool_("MINUTES_IDENTIFY_VOICES"):
            voice_labels, voice_note = voiceprint.label_room_segments(
                directory, written.segments, self.people, self.config,
                room_people=[p.get("person_id", "") for p in room_people or []],
            )
            if voice_note:
                notices.append(voice_note)

        attribute.attribute(
            written,
            roster_samples=samples,
            voice_labels=voice_labels,
            room_people=room_people or [],
            invited=meta.invited,
        )
        written.notices = notices
        error = "" if written.segments else (
            notices[0] if notices else "Nothing was transcribed."
        )
        return written, error

    def _summarise(self, meta: SessionMeta, directory: Path, written: Transcript) -> tuple[bool, str]:
        prior = self._prior_summaries(meta)
        result = summarize.summarise(written, self.config, prior)
        write_json(directory / "summary.json", result.to_dict(), mode=0o600)
        # summarize.py has already logged the outcome, with the model and the
        # token counts. Logging it again here would put every summary in the
        # journal twice and make the second one look like a second attempt.
        if not result.ok:
            return False, result.error
        meta.stage = STAGE_SUMMARISED
        self._write_meta(meta, directory)
        return True, ""

    def _send(self, meta: SessionMeta, directory: Path, written: Transcript) -> None:
        payload = read_json(directory / "summary.json", default=None)
        result = summarize.Summary.from_dict(payload)
        if result is None or not result.ok:
            return
        delivery = mailer.send_summary(self.config, written, result)
        write_json(directory / "delivery.json", delivery.to_dict(), mode=0o600)
        if delivery.ok:
            meta.stage = STAGE_SENT
            self._write_meta(meta, directory)
            log_event(
                log, logging.INFO, "minutes.summary_sent",
                session=meta.session_id, recipients=len(delivery.sent_to),
            )
        else:
            log_event(
                log, logging.WARNING, "minutes.summary_not_sent",
                session=meta.session_id, error=delivery.error,
            )

    def _prior_summaries(self, meta: SessionMeta) -> list[dict[str, str]]:
        """Earlier write-ups of the same recurring meeting, newest first.

        A weekly stand-up summarised without last week's actions in view is a
        list of things that sound new. Matching on the title is crude, but it is
        the only thing a calendar reliably keeps the same across a series.
        """
        wanted = (meta.title or "").strip().lower()
        limit = self.config.int_("MINUTES_SUMMARY_CONTEXT_MEETINGS")
        if not wanted or limit <= 0:
            return []
        out: list[dict[str, str]] = []
        for session_id in paths.list_session_ids():
            if len(out) >= limit:
                break
            # The session being written up is the newest, so it is the first
            # thing this loop sees. Skipping it is not the same as stopping.
            if session_id == meta.session_id:
                continue
            directory = paths.session_dir(session_id)
            if directory is None:
                continue
            other = self._read_meta(directory)
            if other is None or (other.title or "").strip().lower() != wanted:
                continue
            payload = read_json(directory / "summary.json", default=None)
            summary = summarize.Summary.from_dict(payload)
            if summary is None or not summary.ok or not summary.text:
                continue
            out.append(
                {
                    "title": other.title,
                    "date": _readable_date(other.started_at),
                    "summary": summary.text,
                }
            )
        return out

    # -- retention -------------------------------------------------------
    def _apply_audio_retention(self, directory: Path) -> None:
        """Delete the recording once the transcript exists, if that is the rule."""
        if self.config.int_("MINUTES_KEEP_AUDIO_DAYS") > 0:
            return
        for wav in directory.glob("*.wav"):
            try:
                wav.unlink()
            except OSError:
                pass

    def _maybe_sweep(self) -> None:
        now = datetime.now(timezone.utc)
        if self._last_sweep is not None:
            if (now - self._last_sweep).total_seconds() < SWEEP_SECONDS:
                return
        self._last_sweep = now
        self.sweep()

    def sweep(self) -> int:
        """Delete anything past its retention date. Returns how many went."""
        keep_days = max(1, self.config.int_("MINUTES_KEEP_DAYS"))
        audio_days = self.config.int_("MINUTES_KEEP_AUDIO_DAYS")
        now = datetime.now(timezone.utc)
        removed = 0
        for session_id in paths.list_session_ids():
            directory = paths.session_dir(session_id)
            if directory is None:
                continue
            started = self._session_started(session_id, directory)
            if started is None:
                continue
            age = now - started
            if age > timedelta(days=keep_days):
                self._delete_session_dir(directory)
                removed += 1
                continue
            if audio_days and age > timedelta(days=audio_days):
                self._apply_audio_retention_forced(directory)
        if removed:
            log_event(log, logging.INFO, "minutes.swept", removed=removed)
        return removed

    def _apply_audio_retention_forced(self, directory: Path) -> None:
        for wav in directory.glob("*.wav"):
            try:
                wav.unlink()
            except OSError:
                pass

    @staticmethod
    def _session_started(session_id: str, directory: Path) -> datetime | None:
        """When a session began, from its id — no file read required."""
        try:
            stamp = datetime.strptime(session_id[:15], "%Y%m%d-%H%M%S")
        except ValueError:
            try:
                return datetime.fromtimestamp(directory.stat().st_mtime, timezone.utc)
            except OSError:
                return None
        return stamp.replace(tzinfo=timezone.utc)

    def _recover_unfinished(self) -> None:
        """Pick up anything a restart interrupted.

        A meeting recorded through a power cut leaves a directory whose meta
        file still says "recording". The audio in it is as good as whatever was
        flushed to disk, which is usually most of the meeting, so it is worth
        finishing rather than throwing away.
        """
        for session_id in paths.list_session_ids():
            directory = paths.session_dir(session_id)
            if directory is None:
                continue
            meta = self._read_meta(directory)
            if meta is None or meta.stage not in UNFINISHED:
                continue
            if meta.stage == STAGE_RECORDING:
                meta.stage = STAGE_CAPTURED
                meta.ended_at = meta.ended_at or datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )
                meta.error = "The appliance restarted while this meeting was recording."
                self._write_meta(meta, directory)
            log_event(log, logging.INFO, "minutes.recovered", session=session_id)
            self._queue.put(session_id)

    # -- files -----------------------------------------------------------
    @staticmethod
    def _write_meta(meta: SessionMeta, directory: Path) -> None:
        write_json(directory / "meta.json", meta.to_dict(), mode=0o600)

    @staticmethod
    def _read_meta(directory: Path) -> SessionMeta | None:
        return SessionMeta.from_dict(read_json(directory / "meta.json", default=None))

    @staticmethod
    def _save_transcript(directory: Path, written: Transcript) -> None:
        write_json(directory / "transcript.json", written.to_dict(), mode=0o600)

    @staticmethod
    def _read_transcript(directory: Path) -> Transcript | None:
        return Transcript.from_dict(read_json(directory / "transcript.json", default=None))

    @staticmethod
    def _delete_session_dir(directory: Path) -> None:
        try:
            for child in directory.iterdir():
                try:
                    child.unlink()
                except OSError:
                    pass
            directory.rmdir()
        except OSError:
            pass

    # -- what the web page asks for --------------------------------------
    def dashboard_payload(self) -> dict[str, Any]:
        """The one line the room screen shows about this feature.

        The kiosk is on the meeting's own page while a meeting runs, so this
        notice is mostly seen before and after. That is still the right place
        for it: a standing notice on the room screen is how somebody walking in
        finds out that meetings here are recorded, which is the part that
        actually matters.
        """
        if not self.enabled:
            return {"enabled": False, "recording": False, "notice": ""}
        with self._lock:
            recording = self._recording is not None
        notice = ""
        if self.config.bool_("MINUTES_SHOW_RECORDING_NOTICE"):
            notice = (
                "Recording — this meeting is being transcribed."
                if recording
                else "Meetings in this room are recorded and summarised."
            )
        return {"enabled": True, "recording": recording, "notice": notice}

    def status(self) -> dict[str, Any]:
        """Everything the settings and minutes pages need to explain themselves."""
        with self._lock:
            recording = self._recording
            working_on = self._working_on
            look = dict(self._last_room_look)
        summary_ok, summary_why = summarize.available(self.config)
        email_ok, email_why = mailer.available(self.config)
        face_ok, face_why = faces.available(self.config)
        voice_ok, voice_why = voiceprint.available(self.config)
        return {
            "enabled": self.enabled,
            "recording": (
                {
                    "session_id": recording.meta.session_id,
                    "title": recording.meta.title,
                    "seconds": round(recording.seconds),
                }
                if recording
                else None
            ),
            "working_on": working_on,
            "queued": self._queue.qsize(),
            "people": self.people.stats(),
            "sessions": len(paths.list_session_ids()),
            "room_look": look,
            "capabilities": {
                "audio": _cap(*audio.available(self.config)),
                "transcribe": _cap(*transcribe.available(self.config)),
                "roster": _cap(*roster.available(self.config)),
                "faces": _cap(face_ok, face_why),
                "voices": _cap(voice_ok, voice_why),
                "summary": _cap(summary_ok, summary_why),
                "email": _cap(email_ok, email_why),
            },
            "engines": transcribe.engine_report(self.config),
            "dependencies": deps.report(),
            "last_error": self._last_error,
        }

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        """A row per recorded meeting, newest first, without loading transcripts."""
        out: list[dict[str, Any]] = []
        for session_id in paths.list_session_ids()[: max(1, limit)]:
            directory = paths.session_dir(session_id)
            if directory is None:
                continue
            meta = self._read_meta(directory)
            if meta is None:
                continue
            summary = summarize.Summary.from_dict(
                read_json(directory / "summary.json", default=None)
            )
            delivery = read_json(directory / "delivery.json", default=None) or {}
            out.append(
                {
                    "session_id": session_id,
                    "title": meta.title,
                    "started_at": meta.started_at,
                    "ended_at": meta.ended_at,
                    "provider": meta.provider,
                    "stage": meta.stage,
                    "error": meta.error,
                    "has_summary": bool(summary and summary.ok),
                    "has_audio": any(directory.glob("*.wav")),
                    "sent_to": len(delivery.get("sent_to") or []),
                }
            )
        return out

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        directory = paths.session_dir(session_id)
        if directory is None or not directory.is_dir():
            return None
        meta = self._read_meta(directory)
        if meta is None:
            return None
        written = self._read_transcript(directory)
        summary = summarize.Summary.from_dict(
            read_json(directory / "summary.json", default=None)
        )
        return {
            "session_id": session_id,
            "meta": meta.to_dict(),
            "transcript": written.to_dict() if written else None,
            "text": written.render_text() if written else "",
            "speakers": written.speakers() if written else [],
            "summary": summary.to_dict() if summary else None,
            "delivery": read_json(directory / "delivery.json", default=None),
            "recipients": written.recipients() if written else [],
        }

    def delete_session(self, session_id: str) -> bool:
        directory = paths.session_dir(session_id)
        if directory is None or not directory.is_dir():
            return False
        self._delete_session_dir(directory)
        log_event(log, logging.INFO, "minutes.session_deleted", session=session_id)
        return True

    def relabel(self, session_id: str, label: str, person_id: str) -> tuple[bool, str]:
        """Say who an unidentified speaker actually was, and remember the voice.

        This is the enrolment path that costs nobody anything: after a meeting
        somebody reads "Room speaker 2" in the transcript, picks a name, and the
        appliance both fixes the transcript and learns the voice from the very
        segments it just got wrong.
        """
        directory = paths.session_dir(session_id)
        if directory is None or not directory.is_dir():
            return False, "No such recording."
        written = self._read_transcript(directory)
        if written is None:
            return False, "That recording has no transcript yet."
        person = self.people.get(person_id)
        if person is None:
            return False, "No such person."

        touched = [s for s in written.segments if s.label() == label]
        if not touched:
            return False, f"Nothing in this transcript is labelled “{label}”."
        for segment in touched:
            segment.speaker = person.name
            segment.person_id = person.id
            segment.source = SOURCE_MANUAL
            segment.confidence = 1.0

        known = {p.person_id for p in written.participants}
        if person.id not in known:
            written.participants.append(
                Participant(
                    name=person.name,
                    email=person.email,
                    person_id=person.id,
                    where="remote" if touched[0].is_remote else "room",
                    source="manual",
                )
            )
        self._save_transcript(directory, written)

        learned = ""
        if not touched[0].is_remote and self.config.bool_("MINUTES_IDENTIFY_VOICES"):
            ok, why = voiceprint.learn_from_segments(
                directory, touched, self.people, person.id
            )
            learned = " The voice was added to the profile." if ok else f" {why}"
        log_event(
            log, logging.INFO, "minutes.relabelled",
            session=session_id, person=person.id, segments=len(touched),
        )
        count = len(touched)
        lines = "line is" if count == 1 else "lines are"
        return True, f"{count} {lines} now labelled “{person.name}”.{learned}"

    def reprocess(self, session_id: str) -> tuple[bool, str]:
        """Run the summary and the email again for a session already recorded."""
        directory = paths.session_dir(session_id)
        if directory is None or not directory.is_dir():
            return False, "No such recording."
        self._queue.put(session_id)
        return True, "Queued. It will be written up again in a moment."

    # -- enrolment -------------------------------------------------------
    def enrol_photo(self, person_id: str, data: bytes) -> tuple[bool, str]:
        person = self.people.get(person_id)
        if person is None:
            return False, "No such person."
        vector, model, error = faces.embed_image(data, self.config)
        if error:
            return False, error
        kind = detect_image_type(data[:32])
        index = self.people.next_photo_index(person_id)
        path = paths.photo_path(person_id, index, kind[0] if kind else ".jpg")
        if path is not None:
            try:
                paths.ensure_dirs()
                path.write_bytes(data)
                path.chmod(0o600)
                self.people.record_photo(person_id, index)
            except OSError as exc:
                log_event(log, logging.WARNING, "minutes.photo_write_failed", error=str(exc))
        added, why = self.people.add_vector(
            person_id, KIND_FACE, model, vector, note="uploaded photo"
        )
        if not added:
            return False, why
        return True, f"{person.name} can now be recognised by sight."

    def enrol_voice(self, person_id: str, wav_path: Path) -> tuple[bool, str]:
        person = self.people.get(person_id)
        if person is None:
            return False, "No such person."
        vector, model, error = voiceprint.embed_file(wav_path)
        if error:
            return False, error
        added, why = self.people.add_vector(
            person_id, KIND_VOICE, model, vector, note="enrolment sample"
        )
        if not added:
            return False, why
        return True, f"{person.name}'s voice has been added."

    def record_voice_sample(self, person_id: str, seconds: int) -> tuple[bool, str]:
        """Record from the room microphone right now and enrol what it hears.

        A browser cannot open a microphone over plain HTTP on the LAN, so the
        phone in somebody's hand cannot capture the sample. The appliance can:
        stand in the room, press the button, say a couple of sentences, and it
        is the room's own far-field microphone doing the recording — which is
        the microphone that will have to recognise them later. That makes this
        the *better* way round, not a workaround.
        """
        person = self.people.get(person_id)
        if person is None:
            return False, "No such person."
        with self._lock:
            if self._recording is not None:
                return False, "A meeting is being recorded. Try again afterwards."
        wav, error = audio.record_sample(self.config, max(3, min(30, seconds)))
        if error:
            return False, error
        try:
            return self.enrol_voice(person_id, wav)
        finally:
            try:
                wav.unlink()
            except OSError:
                pass


def _cap(ok: bool, why: str) -> dict[str, Any]:
    return {"ok": bool(ok), "detail": why}


def _readable_date(stamp: str) -> str:
    """``2026-08-21T09:00:00+00:00`` as ``21 August 2026``.

    The prompt puts these above earlier summaries as headings, and a person
    reading the summary will see them, so an ISO timestamp is the wrong shape.
    An unparseable value is handed back untouched rather than dropped: a odd
    date is more use than no date.
    """
    try:
        return datetime.fromisoformat(stamp).strftime("%d %B %Y").lstrip("0")
    except (TypeError, ValueError):
        return stamp
