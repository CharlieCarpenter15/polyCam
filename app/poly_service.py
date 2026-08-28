"""Detect and configure the Poly USB conference bar.

The bar presents itself as three ordinary Linux devices — a UVC camera, an
audio capture source and an audio sink — so nothing here is Poly-specific
beyond *recognising the name*. No model is hard-coded: matching is done against
a configurable list of words (``POLY_USB_MATCH``), which defaults to poly /
plantronics / polycom / studio / hp. Any USB conference device can therefore be
used by adding its name in Settings.

Tools used, all standard on Raspberry Pi OS:

* ``lsusb``   – is the bar plugged in at all
* ``v4l2-ctl``– which /dev/video* node is the camera (sysfs is used as a fallback,
  so the appliance still works if v4l-utils is not installed)
* ``pactl``   – PipeWire/PulseAudio sources, sinks, defaults, volume and mute

Everything degrades gracefully: a missing tool or an unplugged bar produces a
warning on the dashboard, never an exception.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import ConfigManager
from .logging_setup import get_logger, log_event
from .models import FAIL, OFF, OK, UNKNOWN, WARN
from .system_service import CommandResult, run, which

log = get_logger("poly")


@dataclass
class AudioDevice:
    """A PipeWire/PulseAudio source or sink."""

    name: str
    description: str
    index: str = ""
    is_default: bool = False
    matched: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "is_default": self.is_default,
            "matched": self.matched,
        }


@dataclass
class CameraDevice:
    path: str
    name: str
    matched: bool = False

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "name": self.name, "matched": self.matched}


@dataclass
class PolyState:
    """Everything known about the conference bar right now."""

    usb_present: bool = False
    usb_name: str = ""
    camera: CameraDevice | None = None
    microphone: AudioDevice | None = None
    speaker: AudioDevice | None = None
    all_cameras: list[CameraDevice] = field(default_factory=list)
    all_sources: list[AudioDevice] = field(default_factory=list)
    all_sinks: list[AudioDevice] = field(default_factory=list)
    volume_percent: int | None = None
    muted: bool | None = None
    tools_missing: list[str] = field(default_factory=list)
    checked_at: float = 0.0

    def camera_status(self) -> str:
        if self.camera is None:
            return FAIL if self.usb_present else UNKNOWN
        return OK if self.camera.matched else WARN

    def microphone_status(self) -> str:
        if self.microphone is None:
            return FAIL
        return OK if self.microphone.matched else WARN

    def speaker_status(self) -> str:
        if self.speaker is None:
            return FAIL
        return OK if self.speaker.matched else WARN


class PolyService:
    """Keeps the conference bar selected as the room's camera, mic and speaker."""

    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._state = PolyState()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._was_present: bool | None = None
        self._applied_once = False

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="poly-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    def refresh_now(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.detect()
                if self.config.bool_("POLY_ENABLED"):
                    self.apply_defaults(first_run=not self._applied_once)
            except Exception:  # pragma: no cover - the monitor must not die
                log.exception("poly.monitor_crashed")
            self._wake.wait(timeout=max(5, self.config.int_("POLY_CHECK_SECONDS")))
            self._wake.clear()

    # -- state -----------------------------------------------------------
    @property
    def state(self) -> PolyState:
        with self._lock:
            return self._state

    def status(self) -> dict[str, object]:
        state = self.state
        if not self.config.bool_("POLY_ENABLED"):
            return {
                "enabled": False,
                "camera": {"status": OFF},
                "microphone": {"status": OFF},
                "speaker": {"status": OFF},
            }
        if self.config.bool_("DEV_MODE"):
            return self._mock_status()
        return {
            "enabled": True,
            "usb_present": state.usb_present,
            "usb_name": state.usb_name,
            "tools_missing": state.tools_missing,
            "camera": {
                "status": state.camera_status(),
                "path": state.camera.path if state.camera else "",
                "name": state.camera.name if state.camera else "",
            },
            "microphone": {
                "status": state.microphone_status(),
                "name": state.microphone.name if state.microphone else "",
                "description": state.microphone.description if state.microphone else "",
                "muted": state.muted,
            },
            "speaker": {
                "status": state.speaker_status(),
                "name": state.speaker.name if state.speaker else "",
                "description": state.speaker.description if state.speaker else "",
                "volume": state.volume_percent,
            },
        }

    def _mock_status(self) -> dict[str, object]:
        """Pretend a bar is connected, for development on a laptop."""
        return {
            "enabled": True,
            "mock": True,
            "usb_present": True,
            "usb_name": "Poly Studio (mock)",
            "tools_missing": [],
            "camera": {"status": OK, "path": "/dev/video0", "name": "Poly Studio (mock)"},
            "microphone": {"status": OK, "name": "mock_source", "description": "Poly Studio (mock)", "muted": False},
            "speaker": {"status": OK, "name": "mock_sink", "description": "Poly Studio (mock)", "volume": 65},
        }

    def inventory(self) -> dict[str, object]:
        """Full device listing for the diagnostics page."""
        state = self.state
        return {
            "usb_present": state.usb_present,
            "usb_name": state.usb_name,
            "cameras": [c.to_dict() for c in state.all_cameras],
            "sources": [s.to_dict() for s in state.all_sources],
            "sinks": [s.to_dict() for s in state.all_sinks],
            "tools_missing": state.tools_missing,
            "match_words": self.config.list_("POLY_USB_MATCH"),
        }

    # -- matching --------------------------------------------------------
    def _match_words(self) -> list[str]:
        return [w.lower() for w in self.config.list_("POLY_USB_MATCH") if w.strip()]

    def _matches(self, *texts: str) -> bool:
        words = self._match_words()
        if not words:
            return False
        haystack = " ".join(t.lower() for t in texts if t)
        return any(word in haystack for word in words)

    # -- detection -------------------------------------------------------
    def detect(self) -> PolyState:
        """Probe USB, camera and audio; store and return the new state."""
        if self.config.bool_("DEV_MODE"):
            with self._lock:
                self._state = PolyState(
                    usb_present=True, usb_name="Poly Studio (mock)", checked_at=time.time()
                )
                return self._state

        tools_missing: list[str] = []
        usb_present, usb_name = self._detect_usb(tools_missing)
        cameras = self._detect_cameras(tools_missing)
        sources, sinks = self._detect_audio(tools_missing)

        camera = self._choose_camera(cameras)
        microphone = self._choose_audio(sources, self.config.str_("MICROPHONE_DEVICE"))
        speaker = self._choose_audio(sinks, self.config.str_("SPEAKER_DEVICE"))

        volume, muted = self._read_levels(speaker, microphone)

        state = PolyState(
            usb_present=usb_present,
            usb_name=usb_name,
            camera=camera,
            microphone=microphone,
            speaker=speaker,
            all_cameras=cameras,
            all_sources=sources,
            all_sinks=sinks,
            volume_percent=volume,
            muted=muted,
            tools_missing=tools_missing,
            checked_at=time.time(),
        )
        with self._lock:
            self._state = state

        # Log arrival and departure once, not on every poll.
        if self._was_present is None:
            if usb_present:
                log_event(log, logging.INFO, "poly.device_detected", device=usb_name or "unknown")
            else:
                log_event(log, logging.WARNING, "poly.device_not_found")
        elif usb_present and not self._was_present:
            log_event(log, logging.INFO, "poly.device_detected", device=usb_name or "unknown")
            self._applied_once = False  # re-apply defaults after a re-plug
        elif not usb_present and self._was_present:
            log_event(log, logging.WARNING, "poly.device_disconnected")
        self._was_present = usb_present
        return state

    def _detect_usb(self, tools_missing: list[str]) -> tuple[bool, str]:
        if not which("lsusb"):
            tools_missing.append("lsusb (apt install usbutils)")
            # Fall back to sysfs product names.
            for path in Path("/sys/bus/usb/devices").glob("*/product"):
                try:
                    product = path.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    continue
                if self._matches(product):
                    return True, product
            return False, ""

        result = run(["lsusb"], timeout=8)
        if not result.ok:
            return False, ""
        for line in result.stdout.splitlines():
            # e.g. "Bus 002 Device 004: ID 095d:9203 Polycom Poly Studio"
            if self._matches(line):
                description = line.split(":", 2)[-1].strip()
                description = re.sub(r"^ID [0-9a-fA-F]{4}:[0-9a-fA-F]{4}\s*", "", description)
                return True, description or line.strip()
        return False, ""

    def _detect_cameras(self, tools_missing: list[str]) -> list[CameraDevice]:
        cameras: list[CameraDevice] = []

        if which("v4l2-ctl"):
            result = run(["v4l2-ctl", "--list-devices"], timeout=10)
            if result.ok and result.stdout.strip():
                current_name = ""
                for raw in result.stdout.splitlines():
                    line = raw.rstrip()
                    if not line:
                        continue
                    if not line.startswith(("\t", " ")):
                        current_name = re.sub(r"\s*\([^)]*\)\s*$", "", line).strip(":").strip()
                        continue
                    device = line.strip()
                    if device.startswith("/dev/video"):
                        cameras.append(
                            CameraDevice(
                                path=device,
                                name=current_name,
                                matched=self._matches(current_name),
                            )
                        )
        else:
            tools_missing.append("v4l2-ctl (apt install v4l-utils)")

        if not cameras:
            # sysfs fallback: /sys/class/video4linux/video0/name
            for node in sorted(Path("/sys/class/video4linux").glob("video*")):
                try:
                    name = (node / "name").read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    continue
                cameras.append(
                    CameraDevice(
                        path=f"/dev/{node.name}",
                        name=name,
                        matched=self._matches(name),
                    )
                )

        return self._filter_capture_nodes(cameras)

    @staticmethod
    def _filter_capture_nodes(cameras: list[CameraDevice]) -> list[CameraDevice]:
        """Drop the metadata nodes UVC cameras also register.

        A UVC device exposes several /dev/video* nodes; only some can capture
        video. Where possible ask the kernel, otherwise keep the lowest-numbered
        node per camera name, which is the capture node in practice.
        """
        usable: list[CameraDevice] = []
        for camera in cameras:
            node = Path("/sys/class/video4linux") / Path(camera.path).name
            caps_file = node / "device" / "capabilities"
            try:
                if caps_file.exists():
                    usable.append(camera)
                    continue
            except OSError:
                pass
            index_file = node / "index"
            try:
                if index_file.exists() and index_file.read_text().strip() not in ("0", ""):
                    continue
            except OSError:
                pass
            usable.append(camera)

        # Keep the first node per distinct name.
        seen: set[str] = set()
        out: list[CameraDevice] = []
        for camera in usable:
            key = camera.name or camera.path
            if key in seen:
                continue
            seen.add(key)
            out.append(camera)
        return out

    def _detect_audio(
        self, tools_missing: list[str]
    ) -> tuple[list[AudioDevice], list[AudioDevice]]:
        if not which("pactl"):
            tools_missing.append("pactl (apt install pulseaudio-utils)")
            return [], []

        sources = self._list_audio("sources")
        sinks = self._list_audio("sinks")
        default_source = self._default_device("source")
        default_sink = self._default_device("sink")
        for device in sources:
            device.is_default = device.name == default_source
        for device in sinks:
            device.is_default = device.name == default_sink
        return sources, sinks

    def _list_audio(self, kind: str) -> list[AudioDevice]:
        """Parse ``pactl list short`` plus descriptions from the long form."""
        short = run(["pactl", "list", "short", kind], timeout=10)
        if not short.ok:
            return []
        devices: list[AudioDevice] = []
        for line in short.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            index, name = parts[0].strip(), parts[1].strip()
            if not name or name.startswith("auto_null"):
                continue
            devices.append(AudioDevice(name=name, description="", index=index))

        descriptions = self._describe_audio(kind)
        for device in devices:
            device.description = descriptions.get(device.name, "")
            device.matched = self._matches(device.name, device.description)
        return devices

    def _describe_audio(self, kind: str) -> dict[str, str]:
        long = run(["pactl", "list", kind], timeout=12)
        if not long.ok:
            return {}
        out: dict[str, str] = {}
        name = ""
        for raw in long.stdout.splitlines():
            line = raw.strip()
            if line.startswith("Name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("Description:") and name:
                out[name] = line.split(":", 1)[1].strip()
        return out

    def _default_device(self, kind: str) -> str:
        result = run(["pactl", f"get-default-{kind}"], timeout=8)
        return result.stdout.strip() if result.ok else ""

    # -- choosing --------------------------------------------------------
    def _choose_camera(self, cameras: list[CameraDevice]) -> CameraDevice | None:
        preference = self.config.str_("CAMERA_DEVICE").strip()
        if preference and preference.lower() != "auto":
            for camera in cameras:
                if camera.path == preference:
                    return camera
            # An explicit path that is not enumerated is still worth reporting.
            if Path(preference).exists():
                return CameraDevice(path=preference, name="(configured)", matched=True)
            return None
        for camera in cameras:
            if camera.matched:
                return camera
        return cameras[0] if cameras else None

    def _choose_audio(
        self, devices: list[AudioDevice], preference: str
    ) -> AudioDevice | None:
        preference = (preference or "").strip()
        lowered = preference.lower()

        if preference and lowered not in ("auto", ""):
            if lowered == "hdmi":
                for device in devices:
                    if "hdmi" in device.name.lower() or "hdmi" in device.description.lower():
                        return device
            for device in devices:
                if device.name == preference:
                    return device
            for device in devices:
                if preference.lower() in device.description.lower():
                    return device
            return None

        for device in devices:
            if device.matched:
                return device
        # Nothing matched: fall back to whatever the system already prefers so
        # the room still has sound.
        for device in devices:
            if device.is_default:
                return device
        return devices[0] if devices else None

    # -- applying --------------------------------------------------------
    def apply_defaults(self, *, first_run: bool = False) -> dict[str, bool]:
        """Make the chosen devices the system defaults."""
        if self.config.bool_("DEV_MODE") or not which("pactl"):
            return {}

        state = self.state
        results: dict[str, bool] = {}

        if state.speaker and not state.speaker.is_default:
            results["speaker"] = run(
                ["pactl", "set-default-sink", state.speaker.name], timeout=8
            ).ok
            if results["speaker"]:
                log_event(
                    log, logging.INFO, "poly.default_speaker_set",
                    device=state.speaker.description or state.speaker.name,
                )
                self._move_existing_streams("sink-inputs", "move-sink-input", state.speaker.name)

        if state.microphone and not state.microphone.is_default:
            results["microphone"] = run(
                ["pactl", "set-default-source", state.microphone.name], timeout=8
            ).ok
            if results["microphone"]:
                log_event(
                    log, logging.INFO, "poly.default_microphone_set",
                    device=state.microphone.description or state.microphone.name,
                )
                self._move_existing_streams(
                    "source-outputs", "move-source-output", state.microphone.name
                )

        if first_run and state.speaker:
            target = self.config.int_("POLY_STARTUP_VOLUME")
            if target > 0:
                results["volume"] = self.set_volume(target)
            # A bar that came up muted is a classic "the room has no sound" call.
            run(["pactl", "set-sink-mute", state.speaker.name, "0"], timeout=8)
            if state.microphone:
                run(["pactl", "set-source-mute", state.microphone.name, "0"], timeout=8)
            self._applied_once = True

        if results:
            self.refresh_now()
        return results

    def _move_existing_streams(self, list_kind: str, move_command: str, target: str) -> None:
        """Move already-playing/recording streams onto the new default.

        Chromium grabs a device when it starts; without this, changing the
        default has no effect until the browser restarts.
        """
        listing = run(["pactl", "list", "short", list_kind], timeout=10)
        if not listing.ok:
            return
        for line in listing.stdout.splitlines():
            stream_id = line.split("\t")[0].strip()
            if stream_id.isdigit():
                run(["pactl", move_command, stream_id, target], timeout=8)

    # -- levels ----------------------------------------------------------
    def _read_levels(
        self, speaker: AudioDevice | None, microphone: AudioDevice | None
    ) -> tuple[int | None, bool | None]:
        volume: int | None = None
        muted: bool | None = None
        if not which("pactl"):
            return None, None
        if speaker:
            result = run(["pactl", "get-sink-volume", speaker.name], timeout=8)
            if result.ok:
                match = re.search(r"(\d+)%", result.stdout)
                if match:
                    volume = int(match.group(1))
        if microphone:
            result = run(["pactl", "get-source-mute", microphone.name], timeout=8)
            if result.ok:
                muted = "yes" in result.stdout.lower()
        return volume, muted

    def set_volume(self, percent: int) -> bool:
        percent = max(0, min(100, int(percent)))
        state = self.state
        if self.config.bool_("DEV_MODE"):
            log_event(log, logging.INFO, "poly.volume_simulated", volume=percent)
            return True
        if not state.speaker or not which("pactl"):
            return False
        ok = run(
            ["pactl", "set-sink-volume", state.speaker.name, f"{percent}%"], timeout=8
        ).ok
        if ok:
            log_event(log, logging.INFO, "poly.volume_set", volume=percent)
            self.refresh_now()
        return ok

    def adjust_volume(self, delta: int) -> int | None:
        """Nudge the volume; returns the new level."""
        state = self.state
        current = state.volume_percent
        if current is None:
            current = self.config.int_("POLY_STARTUP_VOLUME")
        target = max(0, min(100, current + int(delta)))
        return target if self.set_volume(target) else None

    def set_mute(self, muted: bool | None = None) -> bool | None:
        """Mute, unmute, or toggle (``muted=None``) the microphone."""
        state = self.state
        if self.config.bool_("DEV_MODE"):
            new_value = (not state.muted) if muted is None else muted
            log_event(log, logging.INFO, "poly.mute_simulated", muted=new_value)
            return new_value
        if not state.microphone or not which("pactl"):
            return None
        argument = "toggle" if muted is None else ("1" if muted else "0")
        if not run(
            ["pactl", "set-source-mute", state.microphone.name, argument], timeout=8
        ).ok:
            return None
        result = run(["pactl", "get-source-mute", state.microphone.name], timeout=8)
        new_value = "yes" in result.stdout.lower() if result.ok else None
        log_event(log, logging.INFO, "poly.mute_changed", muted=new_value)
        self.refresh_now()
        return new_value

    def set_speaker_mute(self, muted: bool | None = None) -> bool | None:
        state = self.state
        if self.config.bool_("DEV_MODE") or not state.speaker or not which("pactl"):
            return None
        argument = "toggle" if muted is None else ("1" if muted else "0")
        if not run(["pactl", "set-sink-mute", state.speaker.name, argument], timeout=8).ok:
            return None
        result = run(["pactl", "get-sink-mute", state.speaker.name], timeout=8)
        return "yes" in result.stdout.lower() if result.ok else None
