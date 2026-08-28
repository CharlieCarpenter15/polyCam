"""Recording two tracks, and surviving the audio stack while doing it.

There is no sound card in CI and no PipeWire to talk to, so every one of these
tests fakes the two things the module actually touches: ``pactl``, through the
``run()`` helper, and ``parecord``, through ``subprocess.Popen``. The fake
recorder writes a real WAV, which is what makes the interesting cases — a
process killed mid-meeting, a default sink that moves, a disk that fills —
testable at all.
"""

from __future__ import annotations

import importlib
import subprocess
import time
import types
import wave
from pathlib import Path

import pytest

from app.minutes import audio

ROOM_SOURCE = "alsa_input.usb-Poly_Studio-00.mono-fallback"
SINK = "alsa_output.usb-Poly_Studio-00.analog-stereo"
MONITOR = f"{SINK}.monitor"


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


def set_free_percent(monkeypatch, percent):
    def usage(_path):
        return types.SimpleNamespace(total=100, used=100 - percent, free=percent)

    monkeypatch.setattr(audio.shutil, "disk_usage", usage)


@pytest.fixture(autouse=True)
def healthy_disk(monkeypatch):
    """Pin the free space, or these tests pass or fail with the build machine.

    The guard is real and the machine running the suite may genuinely be short
    of space, which would refuse recordings that the test is not about.
    """
    set_free_percent(monkeypatch, 40)


@pytest.fixture()
def minutes_paths(room_dirs):
    """Point ``app.minutes.paths`` at the temporary tree as well."""
    from app.minutes import deps
    from app.minutes import paths as minutes_paths_module

    importlib.reload(minutes_paths_module)
    minutes_paths_module.ensure_dirs()
    deps.refresh()
    yield minutes_paths_module
    deps.refresh()
    importlib.reload(minutes_paths_module)


@pytest.fixture()
def session_dir(minutes_paths, tmp_path):
    directory = tmp_path / "session"
    directory.mkdir()
    return directory


@pytest.fixture()
def installed(minutes_paths):
    """Pretend the appliance has its recorder and its control tool."""
    from app.minutes import deps

    deps.set_probe_for_tests("parecord", True, path="/usr/bin/parecord")
    deps.set_probe_for_tests("pactl", True, path="/usr/bin/pactl")
    return deps


class FakePactl:
    """Just enough ``pactl`` to answer the three questions the recorder asks."""

    def __init__(self, sink=SINK, source=ROOM_SOURCE, sources=None):
        self.sink = sink
        self.source = source
        self.sources = [source, MONITOR] if sources is None else list(sources)
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        from app.system_service import CommandResult

        self.calls.append(list(argv))
        if argv[:2] == ["pactl", "get-default-sink"]:
            return CommandResult(True, 0, f"{self.sink}\n", "")
        if argv[:2] == ["pactl", "get-default-source"]:
            return CommandResult(True, 0, f"{self.source}\n", "")
        if argv[:4] == ["pactl", "list", "short", "sources"]:
            lines = "\n".join(
                f"{index}\t{name}\tPipeWire\ts16le 1ch 48000Hz\tIDLE"
                for index, name in enumerate(self.sources)
            )
            return CommandResult(True, 0, lines, "")
        return CommandResult(False, 1, "", f"unexpected command: {argv}")


class FakeProcess:
    """A ``parecord`` that wrote a file and can be made to die on cue."""

    def __init__(self, argv, seconds=1.0, **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.target = Path(self.argv[-1])
        audio.write_silence(self.target, seconds)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = 0

    def kill(self):  # pragma: no cover - only reached if terminate is ignored
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("parecord", timeout or 0)
        return self.returncode

    def die(self, code=1):
        """Pretend PipeWire restarted underneath it."""
        self.returncode = code


class FakePopen:
    """Records every command line, and hands back a fake process for each."""

    def __init__(self, seconds=1.0):
        self.seconds = seconds
        self.calls: list[list[str]] = []
        self.processes: list[FakeProcess] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        process = FakeProcess(argv, seconds=self.seconds, **kwargs)
        self.processes.append(process)
        return process

    def for_stream(self, stream):
        return [p for p in self.processes if f"--stream-name={stream}" in p.argv]


@pytest.fixture()
def pactl(monkeypatch):
    fake = FakePactl()
    monkeypatch.setattr(audio, "run", fake)
    return fake


@pytest.fixture()
def popen(monkeypatch):
    fake = FakePopen()
    monkeypatch.setattr(subprocess, "Popen", fake)
    return fake


@pytest.fixture()
def recorder(config, session_dir, installed, pactl, popen):
    """A recorder wired to the fakes, whose supervisor is driven by hand."""
    made = audio.Recorder(config, session_dir)
    # The background thread would race the assertions; the tests call _tick().
    made.tick_seconds = 60.0
    return made


def frames_in(path):
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes()


def flag_of(argv, name):
    for item in argv:
        if item.startswith(f"--{name}="):
            return item.split("=", 1)[1]
    return ""


# ---------------------------------------------------------------------------
# Reporting honestly on what is possible
# ---------------------------------------------------------------------------


class TestAvailable:
    def test_development_mode_is_available_but_says_it_is_pretending(self, mock_config):
        ok, why = audio.available(mock_config)
        assert ok
        assert "Development mode" in why

    def test_a_missing_recorder_names_the_package(self, config, installed):
        installed.set_probe_for_tests(
            "parecord", False, "“parecord” was not found on PATH (apt install pulseaudio-utils)."
        )
        ok, why = audio.available(config)
        assert not ok
        assert "parecord" in why

    def test_a_missing_pactl_is_reported(self, config, installed):
        installed.set_probe_for_tests("pactl", False, "“pactl” was not found on PATH.")
        ok, why = audio.available(config)
        assert not ok
        assert "pactl" in why

    def test_both_tracks_switched_off_is_not_an_error_but_is_not_available(
        self, config, installed
    ):
        config.update({"MINUTES_RECORD_ROOM": False, "MINUTES_RECORD_FAR_END": False})
        ok, why = audio.available(config)
        assert not ok
        assert "nothing to write down" in why

    def test_everything_present(self, config, installed):
        assert audio.available(config) == (True, "")

    def test_it_never_shells_out(self, config, installed, monkeypatch):
        """The Settings page calls this on every refresh; it must stay cheap."""

        def explode(*args, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("available() must not run a command")

        monkeypatch.setattr(audio, "run", explode)
        assert audio.available(config)[0]


class TestDevices:
    def test_it_reports_the_devices_a_recording_would_use(self, config, installed, pactl):
        found = audio.devices(config)
        assert found["mock"] is False
        assert found["room"]["device"] == ROOM_SOURCE
        assert found["far_end"]["device"] == MONITOR
        assert found["far_end"]["sink"] == SINK
        assert found["format"] == {"rate": 16000, "channels": 1, "encoding": "s16le"}
        assert found["room"]["stream"] == audio.STREAM_ROOM
        assert found["far_end"]["stream"] == audio.STREAM_FAR_END

    def test_development_mode_asks_the_system_nothing(self, mock_config, monkeypatch):
        def explode(*args, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("devices() must not run a command in DEV_MODE")

        monkeypatch.setattr(audio, "run", explode)
        found = audio.devices(mock_config)
        assert found["mock"] is True
        assert found["room"]["device"] == "(silence)"


# ---------------------------------------------------------------------------
# Finding the far-end monitor — trap T9
# ---------------------------------------------------------------------------


class TestMonitorResolution:
    def test_the_monitor_is_the_sink_plus_a_suffix(self, pactl):
        device, sink, notice = audio._resolve_far_end(audio._source_names())
        assert device == MONITOR
        assert sink == SINK
        assert notice == ""

    def test_the_literal_default_sink_alias_falls_back(self, monkeypatch):
        monkeypatch.setattr(audio, "run", FakePactl(sink="@DEFAULT_SINK@", sources=[]))
        device, sink, notice = audio._resolve_far_end(audio._source_names())
        assert device == "@DEFAULT_MONITOR@"
        assert sink == ""
        assert "No speaker has been chosen" in notice

    def test_the_dummy_sink_falls_back(self, monkeypatch):
        monkeypatch.setattr(audio, "run", FakePactl(sink="auto_null", sources=[]))
        device, _sink, notice = audio._resolve_far_end(audio._source_names())
        assert device == "@DEFAULT_MONITOR@"
        assert notice

    def test_no_sink_at_all_falls_back(self, monkeypatch):
        monkeypatch.setattr(audio, "run", FakePactl(sink="", sources=[]))
        device, _sink, notice = audio._resolve_far_end(audio._source_names())
        assert device == "@DEFAULT_MONITOR@"
        assert notice

    def test_a_monitor_that_is_not_listed_falls_back_and_says_so(self, monkeypatch):
        """The sink is real, but its monitor is not a recordable source."""
        monkeypatch.setattr(audio, "run", FakePactl(sources=[ROOM_SOURCE]))
        device, sink, notice = audio._resolve_far_end(audio._source_names())
        assert device == "@DEFAULT_MONITOR@"
        assert sink == SINK
        assert SINK in notice

    def test_the_configured_microphone_is_used_when_it_exists(self, config, pactl):
        config.update({"MICROPHONE_DEVICE": ROOM_SOURCE})
        device, notice = audio._resolve_room(config, audio._source_names())
        assert device == ROOM_SOURCE
        assert notice == ""

    def test_a_configured_microphone_that_does_not_exist_falls_back(self, config, pactl):
        config.update({"MICROPHONE_DEVICE": "alsa_input.no_such_device"})
        device, notice = audio._resolve_room(config, audio._source_names())
        assert device == ROOM_SOURCE
        assert "no_such_device" in notice


# ---------------------------------------------------------------------------
# The command line — traps T10 and T11
# ---------------------------------------------------------------------------


class TestTheCommandLine:
    def test_both_tracks_are_recorded_with_the_exact_flags(self, recorder, popen, session_dir):
        started, why = recorder.start(room=True, far_end=True)
        assert started, why
        assert len(popen.calls) == 2

        room, far_end = popen.calls
        assert room == [
            "/usr/bin/parecord",
            f"--device={ROOM_SOURCE}",
            "--file-format=wav",
            "--format=s16le",
            "--rate=16000",
            "--channels=1",
            "--client-name=room-minutes",
            "--stream-name=room-minutes-room",
            str(session_dir / "room.wav"),
        ]
        assert far_end == [
            "/usr/bin/parecord",
            f"--device={MONITOR}",
            "--file-format=wav",
            "--format=s16le",
            "--rate=16000",
            "--channels=1",
            "--client-name=room-minutes",
            "--stream-name=room-minutes-farend",
            str(session_dir / "farend.wav"),
        ]
        recorder.stop()

    def test_the_file_format_flag_is_always_present(self, recorder, popen):
        """``parec`` without it writes headerless PCM every engine rejects."""
        recorder.start(room=True, far_end=False)
        assert "--file-format=wav" in popen.calls[0]
        recorder.stop()

    def test_the_stream_names_let_poly_service_recognise_us(self, recorder, popen):
        recorder.start(room=True, far_end=True)
        names = [flag_of(call, "stream-name") for call in popen.calls]
        assert names == ["room-minutes-room", "room-minutes-farend"]
        assert {flag_of(call, "client-name") for call in popen.calls} == {"room-minutes"}
        recorder.stop()

    def test_only_the_requested_tracks_are_started(self, recorder, popen):
        recorder.start(room=False, far_end=True)
        assert len(popen.calls) == 1
        assert flag_of(popen.calls[0], "stream-name") == "room-minutes-farend"
        capture = recorder.stop()
        assert capture.room_wav is None
        assert capture.far_end_wav is not None

    def test_neither_track_is_refused(self, recorder):
        started, why = recorder.start(room=False, far_end=False)
        assert not started
        assert "nothing to write down" in why


# ---------------------------------------------------------------------------
# The disk guard — trap T6
# ---------------------------------------------------------------------------


class TestDiskGuard:
    def test_a_nearly_full_disk_refuses_to_start(self, recorder, popen, monkeypatch):
        set_free_percent(monkeypatch, 9)
        started, why = recorder.start(room=True, far_end=True)
        assert not started
        assert "15%" in why and "9%" in why
        assert popen.calls == [], "nothing should have been spawned"

    def test_a_healthy_disk_starts(self, recorder, monkeypatch):
        set_free_percent(monkeypatch, 40)
        assert recorder.start(room=True, far_end=True)[0]
        recorder.stop()

    def test_a_disk_that_fills_mid_meeting_stops_the_recording(
        self, recorder, popen, monkeypatch
    ):
        set_free_percent(monkeypatch, 40)
        assert recorder.start(room=True, far_end=True)[0]
        set_free_percent(monkeypatch, 3)
        recorder._tick()
        assert all(p.terminated for p in popen.processes)
        capture = recorder.stop()
        assert any("3%" in notice for notice in capture.notices)
        # What was captured before the disk filled is still there.
        assert capture.room_wav is not None and capture.room_wav.exists()

    def test_reserved_blocks_do_not_make_a_healthy_disk_look_full(
        self, recorder, monkeypatch
    ):
        """The real numbers from a machine this guard wrongly refused.

        ``shutil.disk_usage`` reports ``free`` as what a non-root process may
        have but ``total`` as the whole filesystem, so ``free / total`` reads
        30 GB free out of 40 GB in use as 11 % and refuses to record. The share
        has to be measured against what is addressable.
        """
        monkeypatch.setattr(
            audio.shutil,
            "disk_usage",
            lambda _path: types.SimpleNamespace(
                total=270_600_000_000, used=9_300_000_000, free=30_500_000_000
            ),
        )
        assert audio.disk_free_percent() == pytest.approx(76.6, abs=0.1)
        started, why = recorder.start(room=True, far_end=True)
        assert started, why
        recorder.stop()

    def test_an_unmeasurable_disk_does_not_block_recording(self, recorder, monkeypatch):
        def refuse(_path):
            raise OSError("no such filesystem")

        monkeypatch.setattr(audio.shutil, "disk_usage", refuse)
        assert audio.disk_free_percent() is None
        assert recorder.start(room=True, far_end=True)[0]
        recorder.stop()


# ---------------------------------------------------------------------------
# Supervision — trap T4
# ---------------------------------------------------------------------------


class TestSupervision:
    def test_a_dead_recorder_is_restarted_into_a_continuation_file(
        self, recorder, popen, session_dir
    ):
        recorder.start(room=True, far_end=False)
        popen.processes[0].die(code=1)
        recorder._tick()

        assert len(popen.calls) == 2
        assert popen.calls[1][-1] == str(session_dir / "room.1.wav")
        assert (session_dir / "room.1.wav").exists()

    def test_the_pieces_are_joined_back_into_one_recording(
        self, recorder, popen, session_dir
    ):
        recorder.start(room=True, far_end=False)
        first = frames_in(session_dir / "room.wav")
        popen.processes[0].die()
        recorder._tick()
        second = frames_in(session_dir / "room.1.wav")

        capture = recorder.stop()
        assert capture.room_wav == session_dir / "room.wav"
        assert frames_in(capture.room_wav) == first + second
        assert not (session_dir / "room.1.wav").exists(), "the piece should be tidied away"
        assert any("joined" in notice for notice in capture.notices)

    def test_three_pieces_are_joined_in_order(self, recorder, popen, session_dir):
        recorder.start(room=True, far_end=False)
        total = frames_in(session_dir / "room.wav")
        for _ in range(2):
            popen.processes[-1].die()
            recorder._tick()
            total += frames_in(popen.processes[-1].target)
        capture = recorder.stop()
        assert frames_in(capture.room_wav) == total

    def test_a_truncated_piece_is_recovered(self, recorder, popen, session_dir):
        """A killed recorder leaves a header claiming the file is empty."""
        recorder.start(room=True, far_end=False)
        first = frames_in(session_dir / "room.wav")
        popen.processes[0].die(code=-9)
        recorder._tick()

        second_path = session_dir / "room.1.wav"
        second = frames_in(second_path)
        _truncate_header(second_path)
        assert frames_in(second_path) == 0, "the fixture must really be broken"

        capture = recorder.stop()
        assert frames_in(capture.room_wav) == first + second

    def test_a_wholly_unreadable_header_is_recovered_from_the_raw_bytes(
        self, recorder, popen, session_dir
    ):
        recorder.start(room=True, far_end=False)
        first = frames_in(session_dir / "room.wav")
        popen.processes[0].die()
        recorder._tick()
        second_path = session_dir / "room.1.wav"
        second = frames_in(second_path)
        _corrupt_riff_length(second_path)
        with pytest.raises(wave.Error):
            frames_in(second_path)

        capture = recorder.stop()
        assert frames_in(capture.room_wav) == first + second

    def test_a_first_piece_that_never_appeared_does_not_lose_the_rest(
        self, recorder, popen, session_dir
    ):
        """The recorder died before writing anything; the restart caught it all."""
        recorder.start(room=True, far_end=False)
        (session_dir / "room.wav").unlink()
        popen.processes[0].die()
        recorder._tick()
        second = frames_in(session_dir / "room.1.wav")

        capture = recorder.stop()
        assert capture.room_wav == session_dir / "room.wav"
        assert frames_in(capture.room_wav) == second
        assert not (session_dir / "room.1.wav").exists()

    def test_a_recorder_that_will_not_stay_up_is_given_up_on(self, recorder, popen):
        recorder.start(room=True, far_end=False)
        for _ in range(audio.MAX_RESTARTS + 2):
            if popen.processes[-1].poll() is None:
                popen.processes[-1].die()
            recorder._tick()
        capture = recorder.stop()
        assert any("would not stay up" in notice for notice in capture.notices)
        assert len(popen.calls) <= audio.MAX_RESTARTS + 1

    def test_the_supervisor_thread_does_the_same_work_on_its_own(
        self, config, session_dir, installed, pactl, popen
    ):
        """The hand-driven tests drive ``_tick``; this proves it is wired up."""
        made = audio.Recorder(config, session_dir)
        made.tick_seconds = 0.01
        assert made.start(room=True, far_end=False)[0]
        popen.processes[0].die()
        deadline = time.monotonic() + 5.0
        while len(popen.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        capture = made.stop()
        assert len(popen.calls) == 2, "the supervisor thread never restarted it"
        assert capture.room_wav is not None

    def test_stopping_terminates_every_recorder(self, recorder, popen):
        recorder.start(room=True, far_end=True)
        assert recorder.running
        recorder.stop()
        assert not recorder.running
        assert all(p.terminated for p in popen.processes)


# ---------------------------------------------------------------------------
# Following the default sink — the silent far-end track
# ---------------------------------------------------------------------------


class TestFollowingTheDefaultSink:
    def test_a_sink_that_moves_mid_meeting_moves_the_recording(
        self, recorder, popen, pactl, session_dir
    ):
        recorder.start(room=True, far_end=True)
        assert flag_of(popen.calls[1], "device") == MONITOR

        # The bar is re-plugged and poly_service makes it the default again.
        pactl.sink = "alsa_output.hdmi-stereo"
        pactl.sources = [ROOM_SOURCE, "alsa_output.hdmi-stereo.monitor"]
        recorder._tick()

        far_end_calls = [c for c in popen.calls if "--stream-name=room-minutes-farend" in c]
        assert len(far_end_calls) == 2
        assert flag_of(far_end_calls[1], "device") == "alsa_output.hdmi-stereo.monitor"
        assert far_end_calls[1][-1] == str(session_dir / "farend.1.wav")

        capture = recorder.stop()
        assert any("changed to" in notice for notice in capture.notices)
        assert capture.far_end_wav == session_dir / "farend.wav"
        assert frames_in(capture.far_end_wav) > 0

    def test_the_room_track_is_left_alone_when_the_sink_moves(
        self, recorder, popen, pactl
    ):
        recorder.start(room=True, far_end=True)
        pactl.sink = "alsa_output.hdmi-stereo"
        pactl.sources = [ROOM_SOURCE, "alsa_output.hdmi-stereo.monitor"]
        recorder._tick()
        assert len(popen.for_stream("room-minutes-room")) == 1
        recorder.stop()

    def test_an_unchanged_sink_restarts_nothing(self, recorder, popen, pactl):
        recorder.start(room=True, far_end=True)
        for _ in range(3):
            recorder._tick()
        assert len(popen.calls) == 2
        recorder.stop()

    def test_a_sink_that_disappears_is_not_followed(self, recorder, popen, pactl):
        """An empty or aliased answer is not a new device to chase."""
        recorder.start(room=True, far_end=True)
        pactl.sink = "@DEFAULT_SINK@"
        recorder._tick()
        assert len(popen.calls) == 2
        recorder.stop()

    def test_a_new_sink_without_a_listed_monitor_uses_the_alias(
        self, recorder, popen, pactl
    ):
        recorder.start(room=True, far_end=True)
        pactl.sink = "alsa_output.hdmi-stereo"
        pactl.sources = [ROOM_SOURCE]  # its monitor is not listed
        recorder._tick()
        far_end_calls = popen.for_stream("room-minutes-farend")
        assert flag_of(far_end_calls[-1].argv, "device") == "@DEFAULT_MONITOR@"
        recorder.stop()


# ---------------------------------------------------------------------------
# No hardware at all
# ---------------------------------------------------------------------------


class TestMockMode:
    def test_development_mode_records_a_readable_placeholder(
        self, mock_config, session_dir, monkeypatch
    ):
        def explode(*args, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("DEV_MODE must not spawn a recorder")

        monkeypatch.setattr(subprocess, "Popen", explode)
        made = audio.Recorder(mock_config, session_dir)
        started, why = made.start(room=True, far_end=True)
        assert started, why
        assert (session_dir / "room.wav").exists()

        capture = made.stop()
        assert capture.room_wav is not None and capture.far_end_wav is not None
        assert frames_in(capture.room_wav) > 0
        assert any("Development mode" in notice for notice in capture.notices)

    def test_missing_tools_fall_back_to_the_placeholder(
        self, config, session_dir, installed, monkeypatch
    ):
        """No recorder installed is a reason to degrade, not to fail."""
        installed.set_probe_for_tests("parecord", False, "“parecord” was not found.")
        monkeypatch.setattr(subprocess, "Popen", _never)
        made = audio.Recorder(config, session_dir)
        assert made.start(room=True, far_end=False)[0]
        capture = made.stop()
        assert capture.room_wav is not None
        # The notice must say what is actually wrong. Telling an appliance in a
        # meeting room that it is in "development mode" sends whoever reads it
        # looking in entirely the wrong place.
        assert any("No recorder is installed" in n for n in capture.notices)
        assert not any("Development mode" in n for n in capture.notices)

    def test_the_placeholder_lasts_as_long_as_the_recording_did(
        self, mock_config, session_dir
    ):
        made = audio.Recorder(mock_config, session_dir)
        made.start(room=True, far_end=False)
        capture = made.stop()
        assert audio.wav_seconds(capture.room_wav) >= 1.0

    def test_elapsed_and_running_track_the_recording(self, mock_config, session_dir):
        made = audio.Recorder(mock_config, session_dir)
        assert made.elapsed() == 0.0
        assert not made.running
        made.start(room=True, far_end=False)
        assert made.running
        assert made.elapsed() >= 0.0
        made.stop()
        assert not made.running
        settled = made.elapsed()
        time.sleep(0.01)
        assert made.elapsed() == settled, "the clock stops when the recording does"

    def test_stopping_a_recorder_that_never_started(self, mock_config, session_dir):
        made = audio.Recorder(mock_config, session_dir)
        capture = made.stop()
        assert capture.room_wav is None and capture.seconds == 0.0

    def test_a_recorder_is_not_reusable(self, mock_config, session_dir):
        made = audio.Recorder(mock_config, session_dir)
        assert made.start(room=True, far_end=False)[0]
        again, why = made.start(room=True, far_end=False)
        assert not again
        assert why == "This recorder has already been used."
        made.stop()


# ---------------------------------------------------------------------------
# The voice-enrolment sample
# ---------------------------------------------------------------------------


class TestRecordSample:
    def test_development_mode_writes_a_placeholder_sample(self, mock_config):
        wav, error = audio.record_sample(mock_config, 5)
        assert error == ""
        assert wav is not None and wav.exists()
        assert frames_in(wav) > 0
        wav.unlink()

    def test_it_records_the_room_microphone_with_our_stream_name(
        self, config, installed, pactl, monkeypatch
    ):
        fake = FakePopen(seconds=3.0)
        monkeypatch.setattr(subprocess, "Popen", fake)
        wav, error = audio.record_sample(config, 3)
        assert error == ""
        assert wav is not None and wav.exists()
        assert flag_of(fake.calls[0], "device") == ROOM_SOURCE
        assert flag_of(fake.calls[0], "stream-name") == "room-minutes-room"
        assert "--file-format=wav" in fake.calls[0]
        assert fake.processes[0].terminated
        wav.unlink()

    def test_an_unavailable_recorder_explains_itself(self, config, installed):
        installed.set_probe_for_tests("parecord", False, "“parecord” was not found.")
        installed.set_probe_for_tests("pactl", False, "“pactl” was not found.")
        config.update({"DEV_MODE": False})
        # With no tools at all the recorder is in mock mode, which still gives a
        # usable file; the honest failure is a recorder that starts and captures
        # nothing.
        wav, error = audio.record_sample(config, 3)
        assert (wav is not None) ^ bool(error)

    def test_a_recorder_that_captures_nothing_is_an_error(
        self, config, installed, pactl, monkeypatch
    ):
        fake = FakePopen(seconds=0.0)
        monkeypatch.setattr(subprocess, "Popen", fake)
        wav, error = audio.record_sample(config, 3)
        assert wav is None
        assert "Nothing was captured" in error


# ---------------------------------------------------------------------------
# WAV handling on its own
# ---------------------------------------------------------------------------


class TestWavHandling:
    def test_silence_is_a_readable_wav_of_the_right_shape(self, tmp_path):
        path = tmp_path / "silence.wav"
        assert audio.write_silence(path, 2.0)
        with wave.open(str(path), "rb") as handle:
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            assert handle.getframerate() == 16000
            assert handle.getnframes() == 32000

    def test_concatenation_sums_the_frames(self, tmp_path):
        first = tmp_path / "room.wav"
        second = tmp_path / "room.1.wav"
        audio.write_silence(first, 1.0)
        audio.write_silence(second, 0.5)
        frames, error = audio.concatenate(first, [first, second])
        assert error == ""
        assert frames == 24000
        assert frames_in(first) == 24000
        assert not second.exists()

    def test_a_truncated_file_is_repaired_in_place(self, tmp_path):
        path = tmp_path / "room.wav"
        audio.write_silence(path, 1.5)
        _truncate_header(path)
        assert frames_in(path) == 0
        assert audio.repair(path)
        assert frames_in(path) == 24000

    def test_a_healthy_file_is_left_alone(self, tmp_path):
        path = tmp_path / "room.wav"
        audio.write_silence(path, 1.0)
        before = path.read_bytes()
        assert audio.repair(path) is False
        assert path.read_bytes() == before

    def test_the_length_comes_from_the_bytes_when_the_header_lies(self, tmp_path):
        path = tmp_path / "room.wav"
        audio.write_silence(path, 3.0)
        _truncate_header(path)
        assert audio.wav_seconds(path) == pytest.approx(3.0, abs=0.01)

    def test_the_length_of_a_missing_file_is_nothing(self, tmp_path):
        assert audio.wav_seconds(tmp_path / "nothing.wav") == 0.0


def _truncate_header(path: Path) -> None:
    """What libsndfile leaves behind when it is killed: a header saying zero."""
    raw = bytearray(path.read_bytes())
    raw[4:8] = (36).to_bytes(4, "little")
    raw[40:44] = (0).to_bytes(4, "little")
    path.write_bytes(bytes(raw))


def _corrupt_riff_length(path: Path) -> None:
    """A RIFF length so wrong that the wave module will not open the file."""
    raw = bytearray(path.read_bytes())
    raw[4:8] = (0).to_bytes(4, "little")
    path.write_bytes(bytes(raw))


def _never(*args, **kwargs):  # pragma: no cover - must not be called
    raise AssertionError("no process should have been started")
