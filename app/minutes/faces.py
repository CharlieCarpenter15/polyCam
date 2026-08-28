"""Who is in the room, according to the conference bar's camera.

This exists to answer one narrow question — *which enrolled colleagues can be
seen in the room right now* — so that a transcript can be attributed to people
rather than to “Speaker 1”. It is deliberately the least trusted source of
names in the package: everything here is a suggestion that better evidence
(the meeting window's participant list, a voice profile, a person correcting
the transcript by hand) is allowed to overrule.

**What it does.** Between meetings it opens the camera, grabs five frames a
couple of seconds apart at 1920×1080, runs YuNet to find faces and SFace to
turn each one into a 128-number vector, and asks ``people.py`` who that
resembles. Both models ship inside plain ``opencv-python-headless`` — they live
in OpenCV's core ``objdetect`` module, not in the contrib build — so the whole
dependency is two pip packages and two ONNX files.

**What it deliberately does not do.**

*It never looks during a meeting.* A V4L2 device belongs to whichever process
allocated its buffers first, so while Chromium is in a call the camera is
simply not available; a loopback device could work around that, and it is not
worth putting a transcode and an out-of-tree kernel module in the path of a
live meeting. The handle is therefore opened late, released early, and never
held between sweeps — a leaked handle means the *next* meeting has no camera,
which is the worst failure this appliance could have.

*It never downloads anything.* The two model files must be put in
``var/minutes/models`` by hand; :func:`models_report` says exactly which files
and where they come from. An appliance that reaches out to the internet on its
own is a surprise.

*It never guesses.* A name goes on the roster only if the face was matched in
at least two of the five frames *and* the best profile beat the second-best by
a clear margin. Faces that match nobody are counted, never named, because a
roster that is confidently wrong is worse than one that is honestly short.

**How well it works, honestly.** For a typical six-person meeting expect
roughly 60–80% of the people present to be named correctly. Someone sitting
within two metres of the bar and facing it is recognised most of the time;
past three metres it degrades quickly and past four metres it does not work at
all, because at 1080p and a 120° lens a face that far away is fewer than 35
pixels wide. Backlighting from a window, a side profile and a face mask each
defeat it outright. Anyone who arrives after the meeting has started is
invisible by design.
"""

from __future__ import annotations

import glob
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..config import ConfigManager
from ..hardware_profile import LOW
from ..logging_setup import get_logger, log_event
from . import deps, paths
from .people import KIND_FACE

log = get_logger("minutes.faces")

#: The name stored alongside every vector this module produces. ``people.py``
#: refuses to compare vectors made by different models, and it tells them apart
#: by this string — so changing the recogniser means changing this name, which
#: retires the old vectors instead of silently comparing incomparable numbers.
MODEL_NAME = "sface-1"

#: SFace's output width. Checked on every embedding: a model that returned a
#: different shape would be a different model wearing this one's name.
EMBEDDING_DIMS = 128

#: What to tell an administrator who has just switched this on. It is returned
#: by :func:`available` even when everything is working, because the failure
#: mode of this feature is not an error message — it is a roster that looks
#: authoritative and is missing half the room.
ACCURACY_NOTE = (
    "Expect roughly 60–80% of the people in a typical six-person meeting to be "
    "named correctly, and the rest to be counted but not named. Somebody sitting "
    "within two metres of the bar and facing it is recognised most of the time; "
    "past three metres that falls away quickly, and past four metres it does not "
    "work at all. A window behind the table, a face turned to one side and a face "
    "mask each defeat it entirely. Anyone who arrives after the meeting has "
    "started is never seen, because the camera belongs to the meeting by then."
)

#: Capture size. This is the single most important number in the file. At
#: 640×480 a face two metres from the bar is 23 pixels wide, which SFace will
#: happily turn into a vector that matches the wrong person; at 1920×1080 the
#: same face is 69 pixels, which is about the floor of usable. Anything lower
#: does not fail loudly, it just gets people's names wrong.
CAPTURE_WIDTH = 1920
CAPTURE_HEIGHT = 1080

#: Frames per sweep, and the gap between them. Consecutive frames of a seated
#: person are nearly identical, so grabbing faster costs inference and buys no
#: new information; the gap also lets the bar's auto-exposure settle.
FRAMES_PER_SWEEP = 5
_FRAME_GAP_SECONDS = 2.5

#: Frames read and thrown away before the first real one. A UVC camera's first
#: frames after it starts streaming are badly exposed — this is the usual cause
#: of “it worked on my desk and not in the room”.
_WARMUP_FRAMES = 10

#: A face must be matched in this many of the sweep's frames to be named. One
#: frame is how a motion-blurred face becomes a wrong name in a transcript.
MIN_SIGHTINGS = 2

#: The best profile must beat the second-best by this much. Without it, two
#: enrolled people scoring 0.41 and 0.40 produce a confident coin toss.
MATCH_MARGIN = 0.05

#: Detections narrower than this are counted but never identified. The
#: embedding of a 20-pixel face is noise, and noise scores surprisingly well:
#: two images of pure static can come out 0.78 similar.
MIN_FACE_WIDTH = 70

#: An enrolment photo is a deliberate act with a good picture available, so it
#: is held to a higher standard than a snatched frame of the room.
MIN_ENROLMENT_FACE_WIDTH = 80

#: YuNet's own thresholds: confidence, non-maximum suppression, and the cap on
#: candidate boxes before suppression. The defaults from the OpenCV tutorial.
_DETECT_SCORE = 0.9
_DETECT_NMS = 0.3
_DETECT_TOP_K = 5000

#: Largest enrolment photo accepted, and the longest side kept after decoding.
#: The byte cap is the real defence against a decompression bomb; the pixel cap
#: stops a 24-megapixel phone photo costing a second of a Raspberry Pi's time
#: for detail no face model can use.
MAX_PHOTO_BYTES = 12 * 1024 * 1024
MAX_PHOTO_SIDE = 2560
MAX_PHOTO_PIXELS = 40_000_000

#: ``magic bytes -> what it is``. Checked before OpenCV sees the data, because
#: “decode whatever this is” is not a safe thing to ask of a C++ library that
#: is fed by an upload form.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
)

#: The two ONNX files, as they are named in the OpenCV Zoo.
_YUNET_GLOB = "face_detection_yunet*.onnx"
_SFACE_GLOB = "face_recognition_sface*.onnx"
_ZOO_URL = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"

_MODELS: tuple[dict[str, Any], ...] = (
    {
        "role": "detector",
        "pattern": _YUNET_GLOB,
        "file": "face_detection_yunet_2023mar.onnx",
        "directory": "face_detection_yunet",
        "bytes": 232_589,
        "purpose": "finding the faces in a frame (YuNet, MIT licence)",
    },
    {
        "role": "recogniser",
        "pattern": _SFACE_GLOB,
        "file": "face_recognition_sface_2021dec.onnx",
        "directory": "face_recognition_sface",
        "bytes": 38_696_353,
        "purpose": "turning a face into numbers (SFace, Apache-2.0 licence)",
    },
)

#: Where the camera nodes announce themselves. A module-level constant so that
#: a test can point it somewhere harmless.
_SYSFS_VIDEO = Path("/sys/class/video4linux")

#: MJPEG, as V4L2 spells it. OpenCV has moved this helper between releases
#: (``cv2.VideoWriter_fourcc`` in 4.x, ``VideoWriter.fourcc`` in 5.x), and the
#: value is a documented little-endian packing of four characters, so it is
#: cheaper to pack it here than to guess which name this build has. Without it
#: OpenCV may negotiate raw YUYV, which does not fit down USB 2.0 at 1080p.
_MJPG = ord("M") | (ord("J") << 8) | (ord("P") << 16) | (ord("G") << 24)


@dataclass
class RoomLook:
    """What one sweep of the room saw."""

    at: str = ""
    #: ``{"person_id", "name", "email", "score"}`` per recognised colleague.
    people: list[dict[str, Any]] = field(default_factory=list)
    faces_seen: int = 0
    frames: int = 0
    error: str = ""
    ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        """The shape stored with a recording and shown in the web page.

        ``unrecognised`` is derived rather than stored because it is the number
        that keeps the roster honest: “Charlie and Priya, plus two we did not
        recognise” is a true statement, and “Charlie and Priya” on its own is
        not.
        """
        return {
            "at": self.at,
            "people": [dict(person) for person in self.people],
            "faces_seen": self.faces_seen,
            "unrecognised": max(0, self.faces_seen - len(self.people)),
            "frames": self.frames,
            "error": self.error,
            "ok": self.ok,
        }


# ---------------------------------------------------------------------------
# Can we do this at all?
# ---------------------------------------------------------------------------


def available(config: ConfigManager) -> tuple[bool, str]:
    """``(can we recognise faces, and a sentence for the administrator)``.

    The order of the checks is the order somebody would fix them in: a switch
    that is off is a choice, development mode and the hardware profile are
    decisions already taken, a missing package is one command, missing model
    files are a download, and a missing camera is a cable.

    Unlike its neighbours in this package, the second half of the pair is not
    empty when the answer is yes: it is :data:`ACCURACY_NOTE`. This feature's
    real failure mode is not a red cross on the Settings page, it is a roster
    that reads like a register and quietly leaves out the far end of the table,
    so the caveat belongs next to the tick rather than in a manual.
    """
    try:
        if not config.bool_("MINUTES_IDENTIFY_FACES"):
            return False, "Recognising faces in the room is switched off."
        if config.bool_("DEV_MODE"):
            return False, (
                "Development mode is on, so the camera is never opened and the "
                "room is always reported as empty."
            )
        if _profile(config) == LOW:
            return False, (
                "This machine is on the “low” performance profile — a Pi 3 or "
                "anything under 2 GB — where a face sweep would take longer "
                "than it is worth. Recognising faces is skipped here."
            )
        if reason := deps.explain("opencv", "numpy"):
            return False, reason
        if reason := _models_missing():
            return False, reason
        if not _camera_paths(config):
            return False, _no_camera_reason(config)
        return True, ACCURACY_NOTE
    except Exception as exc:  # pragma: no cover - a status check must not raise
        log_event(log, logging.WARNING, "minutes.faces.available_failed", error=str(exc))
        return False, "Face recognition could not work out whether it can run."


def models_report() -> list[dict[str, Any]]:
    """Which model files are present, how big they are, and where to get them.

    The appliance will not fetch these on its own, so this is what the Settings
    page shows an administrator instead: two filenames, one directory, and the
    URL they come from.
    """
    out: list[dict[str, Any]] = []
    for spec in _MODELS:
        found = _model_file(str(spec["pattern"]))
        size = 0
        if found is not None:
            try:
                size = found.stat().st_size
            except OSError:
                size = 0
        out.append(
            {
                "role": spec["role"],
                "purpose": spec["purpose"],
                "file": found.name if found is not None else spec["file"],
                "present": found is not None,
                "path": str(found) if found is not None else "",
                "bytes": size,
                "expected_bytes": spec["bytes"],
                "directory": str(paths.MODELS_DIR),
                "url": f"{_ZOO_URL}/{spec['directory']}/{spec['file']}",
            }
        )
    return out


# ---------------------------------------------------------------------------
# Enrolment: one photo in, one vector out
# ---------------------------------------------------------------------------


def embed_image(data: bytes, config: ConfigManager) -> tuple[list[float], str, str]:
    """``(vector, model name, error)`` for one uploaded photo.

    The vector is SFace's raw output — 128 numbers whose length is about 6.17,
    not 1 — because ``people.py`` normalises on the way in and doing it twice
    would be a silent no-op today and a bug the day that changes.

    A photo with no face, or with more than one, is refused rather than
    guessed at: two people in an enrolment photo is a mistake, and picking the
    larger face would quietly teach one person's profile another person's face.

    On failure the model name comes back empty along with the vector, so that
    nothing half-formed can be filed under a name that means something.
    """
    try:
        if reason := _photo_refused(data):
            return [], "", reason
        if config.bool_("DEV_MODE"):
            return [], "", (
                "Development mode is on, so a photo cannot be turned into a "
                "face vector. Switch it off to enrol anybody."
            )
        if reason := deps.explain("opencv", "numpy"):
            return [], "", reason
        if reason := _models_missing():
            return [], "", reason

        import cv2
        import numpy as np

        frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return [], "", "That file could not be decoded as an image."
        frame, reason = _shrink(cv2, frame)
        if reason:
            return [], "", reason

        detector, recogniser, reason = _load_models(cv2)
        if reason:
            return [], "", reason

        rows = _detect(detector, frame)
        if not rows:
            return [], "", (
                "No face was found in that photo. A head-and-shoulders picture, "
                "facing the camera and well lit, works best."
            )
        if len(rows) > 1:
            return [], "", (
                f"That photo has {len(rows)} faces in it. Enrol one person at a "
                "time, so that nobody's profile learns somebody else's face."
            )
        if _face_width(rows[0]) < MIN_ENROLMENT_FACE_WIDTH:
            return [], "", (
                "The face in that photo is too small to be useful — it needs to "
                "be at least a hundred pixels or so across. Crop it closer, or "
                "use a bigger picture."
            )

        values = _feature(recogniser, frame, rows[0])
        if len(values) != EMBEDDING_DIMS:
            return [], "", (
                "The face model returned an unexpected result. Check that the "
                "file in the models directory is the SFace model."
            )
        return values, MODEL_NAME, ""
    except Exception as exc:  # pragma: no cover - an upload must not raise
        log_event(log, logging.WARNING, "minutes.faces.embed_failed", error=str(exc))
        return [], "", "That photo could not be read."


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def look_at_room(config: ConfigManager, store: Any) -> RoomLook:
    """Grab a few frames from the room camera and report who is visible.

    Never raises: a camera that is busy, missing or dark is an ordinary state
    of the world for an appliance, and the caller has a room screen to draw.
    """
    look = RoomLook(at=_now())
    try:
        if not config.bool_("MINUTES_IDENTIFY_FACES"):
            look.error = "Recognising faces in the room is switched off."
            return look
        if config.bool_("DEV_MODE"):
            # Mock hardware. Succeeding with an empty room lets everything
            # downstream — the recording, the transcript, the summary — be
            # exercised on a laptop that has no camera and no models.
            look.ok = True
            return look

        ready, why = available(config)
        if not ready:
            look.error = why
            log_event(log, logging.DEBUG, "minutes.faces.unavailable", reason=why)
            return look

        import cv2

        detector, recogniser, reason = _load_models(cv2)
        if reason:
            look.error = reason
            return look

        frames, reason = _capture(cv2, config)
        if reason:
            look.error = reason
            # A busy camera is a meeting in progress, which is normal and not
            # worth a warning in the journal every time somebody has a call.
            log_event(log, logging.INFO, "minutes.faces.no_frames", reason=reason)
            return look

        look.frames = len(frames)
        threshold = _threshold(config)
        sightings, look.faces_seen = _examine(frames, detector, recogniser, store, threshold)
        look.people = _roster(sightings)
        look.ok = True
        log_event(
            log, logging.INFO, "minutes.faces.swept",
            frames=look.frames,
            faces=look.faces_seen,
            named=len(look.people),
        )
        return look
    except Exception as exc:  # pragma: no cover - the room screen must survive this
        log_event(log, logging.WARNING, "minutes.faces.look_failed", error=str(exc))
        look.people = []
        look.ok = False
        look.error = "The room camera could not be read."
        return look


def _examine(
    frames: Sequence[Any],
    detector: Any,
    recogniser: Any,
    store: Any,
    threshold: float,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Match every usable face in every frame. Returns the tally and a headcount.

    The headcount is the *most* faces seen in any one frame rather than the sum
    over all of them: the same person sitting still appears in all five, and
    reporting five people would be nonsense.
    """
    sightings: dict[str, dict[str, Any]] = {}
    headcount = 0
    for frame in frames:
        rows = [row for row in _detect(detector, frame) if _face_width(row) >= MIN_FACE_WIDTH]
        headcount = max(headcount, len(rows))
        named_here: set[str] = set()
        for row in rows:
            values = _feature(recogniser, frame, row)
            if len(values) != EMBEDDING_DIMS:
                continue
            person, score = _identify(store, values, threshold)
            if person is None or person.id in named_here:
                continue
            named_here.add(person.id)
            tally = sightings.setdefault(
                person.id,
                {"name": person.name, "email": person.email, "score": 0.0, "frames": 0},
            )
            tally["frames"] += 1
            tally["score"] = max(float(tally["score"]), score)
    return sightings, headcount


def _identify(store: Any, values: Sequence[float], threshold: float) -> tuple[Any, float]:
    """The profile this face belongs to, or ``(None, 0.0)`` if we cannot tell.

    Two questions, not one. *Is this anybody?* is the threshold. *Is it clearly
    this person rather than that one?* is the margin, and it is the rule that
    stops the room calling somebody by a colleague's name — which is the one
    failure that makes a transcript actively misleading rather than merely
    incomplete.
    """
    best = store.match(KIND_FACE, MODEL_NAME, values, threshold=threshold)
    if not best.ok:
        return None, 0.0
    others = [person for person in store.all() if person.id != best.person_id]
    # ``threshold=0.0`` because we want the runner-up's score whatever it is;
    # with nobody else enrolled this scores 0.0 and the margin is trivially met.
    second = store.match(KIND_FACE, MODEL_NAME, values, threshold=0.0, candidates=others)
    margin = best.score - second.score
    if margin < MATCH_MARGIN:
        log_event(
            log, logging.DEBUG, "minutes.faces.ambiguous",
            best=round(best.score, 3), margin=round(margin, 3),
        )
        return None, 0.0
    return best.person, float(best.score)


def _roster(sightings: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Everybody seen often enough to name, in alphabetical order."""
    people = [
        {
            "person_id": person_id,
            "name": tally["name"],
            "email": tally["email"],
            "score": round(float(tally["score"]), 3),
        }
        for person_id, tally in sightings.items()
        if int(tally["frames"]) >= MIN_SIGHTINGS
    ]
    people.sort(key=lambda person: str(person["name"]).lower())
    return people


# ---------------------------------------------------------------------------
# The camera
# ---------------------------------------------------------------------------


def _capture(cv2: Any, config: ConfigManager) -> tuple[list[Any], str]:
    """Frames from the first camera node that will actually give us any.

    The chosen node is not always right — the kernel does not expose enough
    over sysfs to be sure which of a bar's several ``/dev/video*`` nodes carries
    pixels — so if the first one opens and delivers nothing, the rest are tried
    before giving up.
    """
    first_error = ""
    for path in _camera_paths(config):
        frames, reason = _grab(cv2, path)
        if frames:
            return frames, ""
        first_error = first_error or reason
    return [], first_error or _no_camera_reason(config)


def _grab(cv2: Any, path: str) -> tuple[list[Any], str]:
    """Open one node, take :data:`FRAMES_PER_SWEEP` frames, and let go of it.

    The release is the important line in this function. A V4L2 handle that is
    still open when a meeting starts means the meeting has no camera, so it
    happens in a ``finally`` and it happens before anything is analysed.
    """
    if _device_in_use(path):
        return [], (
            f"The room camera ({path}) is in use by another program — most "
            "likely a meeting is running — so the room was not looked at."
        )

    capture = None
    frames: list[Any] = []
    try:
        capture = cv2.VideoCapture(path, cv2.CAP_V4L2)
        if not capture.isOpened():
            return [], (
                f"The room camera ({path}) could not be opened. It is either in "
                "use by another program or not a capture device."
            )
        # Order matters: the pixel format has to be settled before the size, or
        # the driver may refuse a 1080p raw-YUYV stream it cannot carry.
        capture.set(cv2.CAP_PROP_FOURCC, _MJPG)
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_WIDTH)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_HEIGHT)
        # One frame of buffer: we want what the room looks like now, not what
        # it looked like when the queue started filling.
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        warm = False
        for _ in range(_WARMUP_FRAMES):
            ok, _frame = capture.read()
            warm = warm or bool(ok)
        if not warm:
            return [], (
                f"The room camera ({path}) opened but sent no pictures. It may "
                "be in use, or it may be the wrong node for this device."
            )

        for index in range(FRAMES_PER_SWEEP):
            if index:
                time.sleep(max(0.0, _FRAME_GAP_SECONDS))
            ok, frame = capture.read()
            if ok and frame is not None:
                frames.append(frame)
        if not frames:
            return [], f"The room camera ({path}) stopped sending pictures."
        return frames, ""
    except Exception as exc:  # pragma: no cover - OpenCV reports, it rarely raises
        return [], f"The room camera ({path}) could not be read: {exc}"
    finally:
        if capture is not None:
            try:
                capture.release()
            except Exception:  # pragma: no cover - releasing must not mask anything
                pass


def _camera_paths(config: ConfigManager) -> list[str]:
    """Camera nodes worth trying, best first.

    Same reasoning as ``poly_service._detect_cameras``: an explicit
    ``CAMERA_DEVICE`` wins outright, otherwise sysfs is read directly — no
    subprocess, because this runs on a timer — the metadata nodes a UVC device
    also registers are dropped, and a node whose name looks like the conference
    bar is preferred over a laptop's built-in webcam.
    """
    preference = config.str_("CAMERA_DEVICE").strip()
    if preference and preference.lower() != "auto":
        # An explicit setting is honoured or reported, never quietly ignored.
        return [preference] if Path(preference).exists() else []

    wanted = [word.lower() for word in config.list_("POLY_USB_MATCH") if word]
    matched: list[str] = []
    others: list[str] = []
    seen_names: set[str] = set()
    try:
        nodes = sorted(_SYSFS_VIDEO.glob("video*"))
    except OSError:
        return []
    for node in nodes:
        try:
            name = (node / "name").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        # A UVC camera registers a capture node at index 0 and metadata nodes
        # after it; the metadata nodes carry payload headers, not pixels.
        try:
            index = (node / "index").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            index = ""
        if index not in ("", "0"):
            continue
        if name in seen_names:
            continue
        seen_names.add(name)
        path = f"/dev/{node.name}"
        lowered = name.lower()
        if any(word in lowered for word in wanted):
            matched.append(path)
        else:
            others.append(path)
    return matched + others


def _device_in_use(path: str) -> bool:
    """Has some other process got this camera open?

    Chromium runs as the same user as the appliance, so its open file
    descriptors are readable without any privilege. This is belt and braces —
    OpenCV would fail to open the device anyway — but it turns a wall of driver
    errors on the console into a sentence, and it costs about ten milliseconds.
    """
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    for fd_dir in glob.glob("/proc/[0-9]*/fd"):
        try:
            entries = list(os.scandir(fd_dir))
        except OSError:
            continue
        for entry in entries:
            try:
                if os.readlink(entry.path) == real:
                    return True
            except OSError:
                continue
    return False


def _no_camera_reason(config: ConfigManager) -> str:
    preference = config.str_("CAMERA_DEVICE").strip()
    if preference and preference.lower() != "auto":
        return (
            f"The camera is set to “{preference}”, which does not exist. Set "
            "“Camera” back to “auto” on the Settings page, or correct the path."
        )
    return (
        "No camera was found, so there is nothing to look at the room with. "
        "Check that the conference bar is plugged in."
    )


# ---------------------------------------------------------------------------
# The models
# ---------------------------------------------------------------------------


def _load_models(cv2: Any) -> tuple[Any, Any, str]:
    """Build the detector and the recogniser, or say why not.

    Loaded on every call and dropped afterwards. Between sweeps this feature
    should cost nothing, and the two graphs are about 65 MB of resident memory
    against roughly fifty milliseconds to load — a bad trade for something that
    runs for ten seconds every couple of minutes.
    """
    detector_path = _detector_file(_major_version(cv2))
    recogniser_path = _model_file(_SFACE_GLOB)
    if detector_path is None or recogniser_path is None:
        return None, None, _models_missing()
    try:
        detector = cv2.FaceDetectorYN_create(
            str(detector_path),
            "",
            (CAPTURE_WIDTH, CAPTURE_HEIGHT),
            _DETECT_SCORE,
            _DETECT_NMS,
            _DETECT_TOP_K,
        )
        recogniser = cv2.FaceRecognizerSF_create(str(recogniser_path), "")
    except Exception as exc:
        log_event(log, logging.WARNING, "minutes.faces.model_load_failed", error=str(exc))
        return None, None, (
            "The face models are present but OpenCV would not load them. They "
            "may be truncated — re-download them and check the file sizes "
            "against the Settings page."
        )
    return detector, recogniser, ""


def _models_missing() -> str:
    """A sentence naming the model files that are not there, or ``""``."""
    gaps = [spec for spec in _MODELS if _model_file(str(spec["pattern"])) is None]
    if not gaps:
        return ""
    names = " and ".join(f"“{spec['file']}”" for spec in gaps)
    return (
        f"{names} {'is' if len(gaps) == 1 else 'are'} not in "
        f"{paths.MODELS_DIR}, so faces cannot be recognised. Download the "
        "file from the OpenCV Zoo (github.com/opencv/opencv_zoo) and copy it "
        "there — the appliance will not fetch it by itself."
    )


def _model_file(pattern: str) -> Path | None:
    """The newest file in the models directory matching ``pattern``."""
    try:
        found = sorted(path for path in paths.MODELS_DIR.glob(pattern) if path.is_file())
    except OSError:
        return None
    return found[-1] if found else None


def _detector_file(major: int) -> Path | None:
    """Which YuNet file to load.

    Two are published. ``2023mar`` has a fixed input shape and was built for
    OpenCV 4; ``2026may`` has a dynamic one and was built for OpenCV 5. Either
    loads under 5, only the older one is safe under 4, and the names sort in
    date order — so when both are present the OpenCV version picks.
    """
    try:
        candidates = sorted(
            path for path in paths.MODELS_DIR.glob(_YUNET_GLOB) if path.is_file()
        )
    except OSError:
        return None
    if not candidates:
        return None
    if major and major < 5:
        older = [path for path in candidates if "2023" in path.name]
        if older:
            return older[0]
    return candidates[-1]


def _major_version(cv2: Any) -> int:
    try:
        return int(str(getattr(cv2, "__version__", "")).split(".", 1)[0])
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Talking to OpenCV
# ---------------------------------------------------------------------------


def _detect(detector: Any, frame: Any) -> list[Any]:
    """Every face in ``frame`` as a row of ``x, y, w, h, five landmarks, score``.

    ``detect`` returns ``(retval, faces)`` and ``faces`` is ``None`` rather
    than an empty array when it finds nothing, which is the single easiest way
    to get a ``TypeError`` out of this API.
    """
    try:
        height, width = _shape(frame)
        if not width or not height:
            return []
        detector.setInputSize((width, height))
        _retval, found = detector.detect(frame)
    except Exception as exc:
        log_event(log, logging.DEBUG, "minutes.faces.detect_failed", error=str(exc))
        return []
    if found is None:
        return []
    try:
        return list(found)
    except TypeError:  # pragma: no cover - defensive
        return []


def _feature(recogniser: Any, frame: Any, row: Any) -> list[float]:
    """SFace's raw embedding for one detected face.

    ``alignCrop`` needs the whole detection row, not just the box: it uses the
    five landmarks to warp the face to 112×112 the way the model was trained.
    """
    try:
        crop = recogniser.alignCrop(frame, row)
        return _floats(recogniser.feature(crop))
    except Exception as exc:
        log_event(log, logging.DEBUG, "minutes.faces.feature_failed", error=str(exc))
        return []


def _floats(feature: Any) -> list[float]:
    """Flatten SFace's ``(1, 128)`` result into plain Python floats.

    Written without numpy so that the vector handed to ``people.py`` is an
    ordinary list — that store does its arithmetic in pure Python and should
    not have to know what an array is.
    """
    values: list[float] = []
    try:
        rows = list(feature)
    except TypeError:
        return []
    for row in rows:
        try:
            values.extend(float(value) for value in row)
        except TypeError:
            try:
                values.append(float(row))
            except (TypeError, ValueError):
                return []
        except ValueError:
            return []
    return values


def _face_width(row: Any) -> float:
    """The width of a detection box, in pixels of the frame it came from."""
    try:
        return float(row[2])
    except (IndexError, TypeError, ValueError):
        return 0.0


def _shape(frame: Any) -> tuple[int, int]:
    """``(height, width)`` of a frame, or ``(0, 0)`` if it does not have one."""
    shape = getattr(frame, "shape", None)
    if not shape or len(shape) < 2:
        return 0, 0
    try:
        return int(shape[0]), int(shape[1])
    except (TypeError, ValueError):
        return 0, 0


def _shrink(cv2: Any, frame: Any) -> tuple[Any, str]:
    """Bring a decoded photo down to a size worth spending inference on."""
    height, width = _shape(frame)
    if not width or not height:
        return frame, "That file could not be decoded as an image."
    if width * height > MAX_PHOTO_PIXELS:
        return frame, "That image is far too large to be a photograph of a person."
    longest = max(width, height)
    if longest <= MAX_PHOTO_SIDE:
        return frame, ""
    scale = MAX_PHOTO_SIDE / float(longest)
    try:
        return cv2.resize(frame, (int(width * scale), int(height * scale))), ""
    except Exception:  # pragma: no cover - a resize failure is not worth refusing over
        return frame, ""


def _photo_refused(data: bytes) -> str:
    """Why this upload is not a photograph we will look at, or ``""``."""
    if not data:
        return "That photo was empty."
    if len(data) > MAX_PHOTO_BYTES:
        megabytes = MAX_PHOTO_BYTES // (1024 * 1024)
        return f"That photo is bigger than {megabytes} MB. Use a smaller one."
    if not any(data.startswith(magic) for magic, _label in _SIGNATURES):
        return "That file is not a JPEG or a PNG."
    return ""


# ---------------------------------------------------------------------------
# Odds and ends
# ---------------------------------------------------------------------------


def _threshold(config: ConfigManager) -> float:
    """How similar a face has to be, kept inside sane bounds.

    OpenCV documents 0.363 as “same person” for a one-to-one check on curated
    photographs. A room camera is neither curated nor one-to-one, so the
    shipped default is a little stricter at 0.40.
    """
    return max(0.05, min(0.95, config.float_("MINUTES_FACE_THRESHOLD")))


def _profile(config: ConfigManager) -> str:
    """The performance profile, or ``""`` if the hardware cannot be read."""
    try:
        return str(config.performance().profile)
    except Exception:  # pragma: no cover - detection failing is not an error
        return ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
