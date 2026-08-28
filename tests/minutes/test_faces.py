"""Recognising faces, on a machine with no camera and no OpenCV.

The appliance's own test machine has neither, and neither does anybody's
laptop, so ``cv2`` and ``numpy`` are replaced by stubs that answer with the
shapes the real libraries use — including the two that are easy to get wrong:
``detect()`` returns ``(retval, faces)`` and hands back ``None`` rather than an
empty array when it finds nothing, and ``feature()`` returns a ``(1, 128)``
result whose length is about 6.17, not 1.

The point of most of these tests is not that the wiring works but that the
module refuses to guess: a face seen once is not a person, and a face that
resembles two colleagues equally is nobody.
"""

from __future__ import annotations

import math
import sys

import pytest

from app.minutes import deps, faces
from app.minutes.people import KIND_FACE, PeopleStore

#: SFace embeddings are not unit vectors; the stub imitates that so a test can
#: prove the raw vector is handed on untouched.
SFACE_NORM = 6.169

JPEG = b"\xff\xd8\xff" + b"\x00" * 512
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 512


# ---------------------------------------------------------------------------
# Vectors
# ---------------------------------------------------------------------------


def enrolled(index: int) -> list[float]:
    """One stored profile: a unit vector along a single axis."""
    values = [0.0] * faces.EMBEDDING_DIMS
    values[index] = 1.0
    return values


def probe(best: float, second: float = 0.0) -> list[float]:
    """A raw SFace-shaped vector.

    Scores exactly ``best`` against ``enrolled(0)`` and ``second`` against
    ``enrolled(1)`` once the profile store has normalised it.
    """
    rest = math.sqrt(max(0.0, 1.0 - best * best - second * second))
    values = [best, second, rest] + [0.0] * (faces.EMBEDDING_DIMS - 3)
    return [value * SFACE_NORM for value in values]


# ---------------------------------------------------------------------------
# Stubs shaped like OpenCV
# ---------------------------------------------------------------------------


class Frame:
    """A captured frame: all this module ever asks of one is its shape."""

    def __init__(self, width: int = 1920, height: int = 1080) -> None:
        self.shape = (height, width, 3)


class Row(list):
    """One YuNet detection: x, y, w, h, five landmarks, and a score."""

    def __init__(self, vector: list[float], width: float = 140.0) -> None:
        super().__init__([8.0, 8.0, float(width), float(width) * 1.3] + [0.0] * 10 + [0.99])
        self.vector = vector


class FakeDetector:
    def __init__(self, per_frame: list[list[Row]]) -> None:
        self._per_frame = per_frame
        self.sizes: list[tuple[int, int]] = []
        self.calls = 0

    def setInputSize(self, size):  # noqa: N802 - OpenCV's spelling
        self.sizes.append(size)

    def detect(self, frame):
        rows = self._per_frame[self.calls] if self.calls < len(self._per_frame) else []
        self.calls += 1
        # YuNet hands back None, not an empty array, when it finds nothing.
        return 1, (list(rows) if rows else None)


class FakeRecogniser:
    def __init__(self) -> None:
        self.crops = 0

    def alignCrop(self, frame, row):  # noqa: N802 - OpenCV's spelling
        self.crops += 1
        return row

    def feature(self, crop):
        return [list(crop.vector)]  # SFace returns (1, 128)


class FakeCapture:
    def __init__(self, *, opened: bool = True, blind: bool = False) -> None:
        self._opened = opened
        self._blind = blind
        self.settings: list[tuple[int, float]] = []
        self.reads = 0
        self.released = False

    def isOpened(self):  # noqa: N802 - OpenCV's spelling
        return self._opened

    def set(self, prop, value):
        self.settings.append((prop, value))
        return True

    def read(self):
        if self._blind:
            return False, None
        self.reads += 1
        return True, Frame()

    def release(self):
        self.released = True


class FakeCv2:
    """Just enough of OpenCV 5 for this module."""

    __version__ = "5.0.0"
    CAP_V4L2 = 200
    CAP_PROP_FRAME_WIDTH = 3
    CAP_PROP_FRAME_HEIGHT = 4
    CAP_PROP_FOURCC = 6
    CAP_PROP_BUFFERSIZE = 38
    IMREAD_COLOR = 1

    def __init__(
        self,
        *,
        detector: FakeDetector | None = None,
        recogniser: FakeRecogniser | None = None,
        captures: list[FakeCapture] | None = None,
        decoded: Frame | None = None,
    ) -> None:
        self.detector = detector or FakeDetector([])
        self.recogniser = recogniser or FakeRecogniser()
        self.captures = list(captures or [])
        self.decoded = decoded
        self.opened_paths: list[str] = []
        self.detector_path = ""
        self.recogniser_path = ""

    def FaceDetectorYN_create(self, path, config, size, score, nms, top_k):  # noqa: N802
        self.detector_path = path
        return self.detector

    def FaceRecognizerSF_create(self, path, config):  # noqa: N802
        self.recogniser_path = path
        return self.recogniser

    def VideoCapture(self, path, api):  # noqa: N802
        self.opened_paths.append(path)
        return self.captures.pop(0) if self.captures else FakeCapture(opened=False)

    def imdecode(self, buffer, flag):
        return self.decoded

    def resize(self, frame, size):
        return Frame(width=size[0], height=size[1])


class FakeNumpy:
    """``frombuffer`` is the only thing this module asks numpy for.

    ``ndarray`` and ``isscalar`` are here for a different reason: ``approx``
    looks in ``sys.modules`` for numpy and asks it whether a value is an array,
    so a stub that does not answer breaks every comparison in the file.
    """

    uint8 = "uint8"

    class ndarray:  # noqa: N801 - numpy's spelling
        pass

    @staticmethod
    def frombuffer(data, dtype):
        return data

    @staticmethod
    def isscalar(value):
        return isinstance(value, (int, float, complex, str, bytes))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def minutes_home(tmp_path, monkeypatch):
    """Point the package's directories at a throw-away tree."""
    from app.minutes import paths as minutes_paths

    root = tmp_path / "minutes"
    places = {
        "MINUTES_DIR": root,
        "SESSIONS_DIR": root / "sessions",
        "PEOPLE_DIR": root / "people",
        "PEOPLE_FILE": root / "people" / "people.json",
        "PHOTOS_DIR": root / "people" / "photos",
        "MODELS_DIR": root / "models",
    }
    for name, path in places.items():
        monkeypatch.setattr(minutes_paths, name, path)
    minutes_paths.ensure_dirs()
    return places


@pytest.fixture()
def models(minutes_home):
    """Both ONNX files, present and the right kind of size."""
    directory = minutes_home["MODELS_DIR"]
    (directory / "face_detection_yunet_2023mar.onnx").write_bytes(b"y" * 2048)
    (directory / "face_recognition_sface_2021dec.onnx").write_bytes(b"s" * 4096)
    return directory


@pytest.fixture()
def installed():
    """Pretend opencv and numpy are installed, and forget it afterwards."""
    deps.set_probe_for_tests("opencv", True)
    deps.set_probe_for_tests("numpy", True)
    yield
    deps.refresh()


@pytest.fixture()
def sysfs(tmp_path, monkeypatch):
    """A fake ``/sys/class/video4linux`` holding a bar and a metadata node."""
    root = tmp_path / "video4linux"
    for name, device, index in (
        ("video0", "Poly Studio: Poly Studio", "0"),
        ("video1", "Poly Studio: Poly Studio", "1"),
    ):
        node = root / name
        node.mkdir(parents=True)
        (node / "name").write_text(device, encoding="utf-8")
        (node / "index").write_text(index, encoding="utf-8")
    monkeypatch.setattr(faces, "_SYSFS_VIDEO", root)
    return root


@pytest.fixture()
def quick(monkeypatch):
    """No waiting between frames, and no walk of /proc."""
    monkeypatch.setattr(faces, "_FRAME_GAP_SECONDS", 0.0)
    monkeypatch.setattr(faces, "_device_in_use", lambda path: False)


@pytest.fixture()
def face_config(config):
    """Face recognition on, on hardware that can take it."""
    changed, errors = config.update(
        {"MINUTES_IDENTIFY_FACES": True, "PERFORMANCE_PROFILE": "balanced"}
    )
    assert not errors, errors
    return config


@pytest.fixture()
def store(minutes_home):
    """A profile store holding Charlie and Priya, one face vector each."""
    people = PeopleStore(minutes_home["PEOPLE_FILE"])
    charlie, error = people.add("Charlie", email="charlie@example.com")
    assert not error, error
    priya, error = people.add("Priya", email="priya@example.com")
    assert not error, error
    people.add_vector(charlie.id, KIND_FACE, faces.MODEL_NAME, enrolled(0))
    people.add_vector(priya.id, KIND_FACE, faces.MODEL_NAME, enrolled(1))
    return people


def install(monkeypatch, cv2):
    """Put the stubs where a lazy ``import cv2`` will find them."""
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    monkeypatch.setitem(sys.modules, "numpy", FakeNumpy())
    return cv2


def sweep(rows_per_frame):
    """A stub OpenCV that will hand back five frames with these detections."""
    return FakeCv2(
        detector=FakeDetector(rows_per_frame),
        recogniser=FakeRecogniser(),
        captures=[FakeCapture()],
    )


# ---------------------------------------------------------------------------
# available()
# ---------------------------------------------------------------------------


class TestAvailable:
    def test_switched_off_says_so(self, config):
        ok, why = faces.available(config)
        assert not ok
        assert "switched off" in why

    def test_development_mode_is_named(self, face_config):
        face_config.update({"DEV_MODE": True})
        ok, why = faces.available(face_config)
        assert not ok
        assert "Development mode" in why

    def test_a_low_powered_machine_is_skipped(self, config):
        config.update({"MINUTES_IDENTIFY_FACES": True, "PERFORMANCE_PROFILE": "low"})
        ok, why = faces.available(config)
        assert not ok
        assert "low" in why and "skipped" in why

    def test_missing_opencv_names_the_package(self, face_config, models):
        deps.set_probe_for_tests("numpy", True)
        deps.set_probe_for_tests("opencv", False, "“opencv-python-headless” is not installed.")
        try:
            ok, why = faces.available(face_config)
        finally:
            deps.refresh()
        assert not ok
        assert "opencv-python-headless" in why

    def test_missing_numpy_names_the_package(self, face_config, models):
        deps.set_probe_for_tests("opencv", True)
        deps.set_probe_for_tests("numpy", False, "“numpy” is not installed.")
        try:
            ok, why = faces.available(face_config)
        finally:
            deps.refresh()
        assert not ok
        assert "numpy" in why
        assert "opencv" not in why, "the message should name only what is missing"

    def test_missing_models_name_the_files_and_the_directory(
        self, face_config, minutes_home, installed
    ):
        ok, why = faces.available(face_config)
        assert not ok
        assert "face_detection_yunet_2023mar.onnx" in why
        assert "face_recognition_sface_2021dec.onnx" in why
        assert str(minutes_home["MODELS_DIR"]) in why
        assert "opencv_zoo" in why
        assert "will not fetch it by itself" in why

    def test_one_missing_model_is_named_on_its_own(
        self, face_config, models, installed
    ):
        (models / "face_recognition_sface_2021dec.onnx").unlink()
        ok, why = faces.available(face_config)
        assert not ok
        assert "face_recognition_sface_2021dec.onnx" in why
        assert "face_detection_yunet" not in why

    def test_no_camera_is_its_own_message(
        self, face_config, models, installed, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(faces, "_SYSFS_VIDEO", tmp_path / "nothing-here")
        ok, why = faces.available(face_config)
        assert not ok
        assert "No camera was found" in why

    def test_a_configured_camera_that_does_not_exist_is_reported(
        self, face_config, models, installed, sysfs
    ):
        face_config.update({"CAMERA_DEVICE": "/dev/video42"})
        ok, why = faces.available(face_config)
        assert not ok
        assert "/dev/video42" in why

    def test_everything_present_is_available(self, face_config, models, installed, sysfs):
        ok, note = faces.available(face_config)
        assert ok is True
        assert note == faces.ACCURACY_NOTE

    def test_a_working_feature_still_says_what_it_cannot_do(self):
        """The admin page has to carry the caveat, not just a green tick."""
        note = faces.ACCURACY_NOTE
        assert "60–80%" in note
        assert "four metres" in note
        assert "after the meeting has started" in note

    def test_every_reason_is_different(self, config, minutes_home, tmp_path, monkeypatch):
        """Each failure has to be actionable, which means each has to differ."""
        monkeypatch.setattr(faces, "_SYSFS_VIDEO", tmp_path / "nothing-here")
        seen = []
        seen.append(faces.available(config)[1])
        config.update({"MINUTES_IDENTIFY_FACES": True, "PERFORMANCE_PROFILE": "low"})
        seen.append(faces.available(config)[1])
        config.update({"PERFORMANCE_PROFILE": "balanced"})
        deps.set_probe_for_tests("opencv", False, "no opencv")
        deps.set_probe_for_tests("numpy", True)
        try:
            seen.append(faces.available(config)[1])
            deps.set_probe_for_tests("opencv", True)
            seen.append(faces.available(config)[1])
        finally:
            deps.refresh()
        assert len(set(seen)) == len(seen), seen


# ---------------------------------------------------------------------------
# models_report()
# ---------------------------------------------------------------------------


class TestModelsReport:
    def test_it_says_what_is_missing_and_where_to_get_it(self, minutes_home):
        report = faces.models_report()
        assert [row["role"] for row in report] == ["detector", "recogniser"]
        for row in report:
            assert row["present"] is False
            assert row["bytes"] == 0
            assert row["expected_bytes"] > 0
            assert row["url"].startswith("https://")
            assert "opencv_zoo" in row["url"]
            assert row["directory"] == str(minutes_home["MODELS_DIR"])

    def test_it_reports_the_files_that_are_there(self, models):
        report = faces.models_report()
        assert all(row["present"] for row in report)
        assert report[0]["bytes"] == 2048
        assert report[1]["bytes"] == 4096
        assert report[0]["path"].endswith("face_detection_yunet_2023mar.onnx")

    def test_it_survives_a_missing_directory(self, minutes_home):
        report = faces.models_report()
        assert len(report) == 2


# ---------------------------------------------------------------------------
# look_at_room()
# ---------------------------------------------------------------------------


class TestLookAtRoom:
    def test_switched_off_is_not_an_error_but_is_not_ok(self, config, store):
        look = faces.look_at_room(config, store)
        assert look.ok is False
        assert "switched off" in look.error
        assert look.people == []

    def test_development_mode_succeeds_with_an_empty_room(self, face_config, store):
        face_config.update({"DEV_MODE": True})
        look = faces.look_at_room(face_config, store)
        assert look.ok is True
        assert look.error == ""
        assert look.people == []
        assert look.frames == 0
        assert look.at, "a look always carries the time it was taken"

    def test_a_low_powered_machine_never_opens_the_camera(
        self, config, store, models, installed, sysfs, quick, monkeypatch
    ):
        config.update({"MINUTES_IDENTIFY_FACES": True, "PERFORMANCE_PROFILE": "low"})
        cv2 = install(monkeypatch, sweep([[]] * 5))
        look = faces.look_at_room(config, store)
        assert look.ok is False
        assert "low" in look.error
        assert cv2.opened_paths == [], "the camera must not be touched at all"

    def test_a_missing_model_stops_the_sweep(
        self, face_config, store, minutes_home, installed, sysfs, quick, monkeypatch
    ):
        cv2 = install(monkeypatch, sweep([[]] * 5))
        look = faces.look_at_room(face_config, store)
        assert look.ok is False
        assert "face_detection_yunet_2023mar.onnx" in look.error
        assert cv2.opened_paths == []

    def test_a_busy_camera_is_reported_calmly(
        self, face_config, store, models, installed, sysfs, monkeypatch
    ):
        monkeypatch.setattr(faces, "_FRAME_GAP_SECONDS", 0.0)
        monkeypatch.setattr(faces, "_device_in_use", lambda path: True)
        cv2 = install(monkeypatch, sweep([[]] * 5))
        look = faces.look_at_room(face_config, store)
        assert look.ok is False
        assert "in use" in look.error
        assert "/dev/video0" in look.error
        assert cv2.opened_paths == [], "a busy device is never opened"

    def test_a_camera_that_will_not_open_is_reported(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        cv2 = FakeCv2(captures=[FakeCapture(opened=False)])
        install(monkeypatch, cv2)
        look = faces.look_at_room(face_config, store)
        assert look.ok is False
        assert "could not be opened" in look.error
        assert cv2.opened_paths == ["/dev/video0"]

    def test_a_camera_that_sends_nothing_is_reported(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        capture = FakeCapture(blind=True)
        install(monkeypatch, FakeCv2(captures=[capture]))
        look = faces.look_at_room(face_config, store)
        assert look.ok is False
        assert "sent no pictures" in look.error
        assert capture.released is True, "the handle must be let go of even so"

    def test_two_sightings_out_of_five_names_a_person(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        charlie = [Row(probe(0.9))]
        install(monkeypatch, sweep([charlie, charlie, [], [], []]))
        look = faces.look_at_room(face_config, store)
        assert look.ok is True
        assert look.frames == 5
        assert [person["name"] for person in look.people] == ["Charlie"]
        assert look.people[0]["email"] == "charlie@example.com"
        assert look.people[0]["person_id"]
        assert look.people[0]["score"] == pytest.approx(0.9, abs=0.01)

    def test_one_sighting_out_of_five_names_nobody(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        install(monkeypatch, sweep([[Row(probe(0.9))], [], [], [], []]))
        look = faces.look_at_room(face_config, store)
        assert look.ok is True
        assert look.people == [], "one blurred frame is not a person being present"
        assert look.faces_seen == 1

    def test_a_face_matching_two_people_equally_is_nobody(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        """0.42 against Charlie and 0.40 against Priya is a coin toss, not a name."""
        ambiguous = [Row(probe(0.42, 0.40))]
        install(monkeypatch, sweep([ambiguous] * 5))
        look = faces.look_at_room(face_config, store)
        assert look.ok is True
        assert look.people == []
        assert look.faces_seen == 1, "the face is still counted, just not named"
        assert look.to_dict()["unrecognised"] == 1

    def test_a_clear_winner_is_named_despite_a_rival(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        clear = [Row(probe(0.60, 0.10))]
        install(monkeypatch, sweep([clear] * 5))
        look = faces.look_at_room(face_config, store)
        assert [person["name"] for person in look.people] == ["Charlie"]

    def test_a_face_below_the_threshold_is_not_named(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        install(monkeypatch, sweep([[Row(probe(0.30))]] * 5))
        look = faces.look_at_room(face_config, store)
        assert look.people == []
        assert look.faces_seen == 1

    def test_the_threshold_setting_is_obeyed(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        face_config.update({"MINUTES_FACE_THRESHOLD": 0.80})
        install(monkeypatch, sweep([[Row(probe(0.55))]] * 5))
        assert faces.look_at_room(face_config, store).people == []

    def test_a_face_too_small_to_trust_is_never_embedded(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        tiny = [Row(probe(0.99), width=20.0)]
        cv2 = sweep([tiny] * 5)
        install(monkeypatch, cv2)
        look = faces.look_at_room(face_config, store)
        assert look.people == []
        assert look.faces_seen == 0, "a 20 pixel face is not evidence of anybody"
        assert cv2.recogniser.crops == 0

    def test_the_headcount_is_the_fullest_frame_not_the_total(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        two = [Row(probe(0.9)), Row(probe(0.1))]
        install(monkeypatch, sweep([two] * 5))
        look = faces.look_at_room(face_config, store)
        assert look.faces_seen == 2, "the same people sitting still are not ten people"
        assert look.to_dict()["unrecognised"] == 1

    def test_the_same_person_twice_in_one_frame_is_one_sighting(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        """A reflection or a photograph on the wall must not vote twice."""
        doubled = [Row(probe(0.9)), Row(probe(0.9))]
        install(monkeypatch, sweep([doubled, [], [], [], []]))
        look = faces.look_at_room(face_config, store)
        assert look.people == []

    def test_the_camera_is_always_released(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        capture = FakeCapture()
        install(
            monkeypatch,
            FakeCv2(
                detector=FakeDetector([[]] * 5),
                recogniser=FakeRecogniser(),
                captures=[capture],
            ),
        )
        faces.look_at_room(face_config, store)
        assert capture.released is True

    def test_it_captures_at_1080p_and_asks_for_mjpeg_first(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        capture = FakeCapture()
        cv2 = FakeCv2(
            detector=FakeDetector([[]] * 5),
            recogniser=FakeRecogniser(),
            captures=[capture],
        )
        install(monkeypatch, cv2)
        faces.look_at_room(face_config, store)
        properties = [prop for prop, _value in capture.settings]
        assert properties[0] == FakeCv2.CAP_PROP_FOURCC, (
            "the pixel format has to be settled before the size is asked for"
        )
        assert (FakeCv2.CAP_PROP_FRAME_WIDTH, 1920) in capture.settings
        assert (FakeCv2.CAP_PROP_FRAME_HEIGHT, 1080) in capture.settings
        assert capture.reads >= faces.FRAMES_PER_SWEEP + faces._WARMUP_FRAMES

    def test_the_detector_is_told_the_real_frame_size(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        cv2 = sweep([[]] * 5)
        install(monkeypatch, cv2)
        faces.look_at_room(face_config, store)
        assert cv2.detector.sizes == [(1920, 1080)] * 5

    def test_a_second_node_is_tried_when_the_first_gives_nothing(
        self, face_config, store, models, installed, quick, tmp_path, monkeypatch
    ):
        """The kernel does not say which node carries pixels, so try them all."""
        root = tmp_path / "v4l"
        for name in ("video0", "video1"):
            node = root / name
            node.mkdir(parents=True)
            (node / "name").write_text(f"Camera {name}", encoding="utf-8")
            (node / "index").write_text("0", encoding="utf-8")
        monkeypatch.setattr(faces, "_SYSFS_VIDEO", root)
        working = FakeCapture()
        cv2 = FakeCv2(
            detector=FakeDetector([[Row(probe(0.9))]] * 5),
            recogniser=FakeRecogniser(),
            captures=[FakeCapture(blind=True), working],
        )
        install(monkeypatch, cv2)
        look = faces.look_at_room(face_config, store)
        assert cv2.opened_paths == ["/dev/video0", "/dev/video1"]
        assert look.ok is True
        assert [person["name"] for person in look.people] == ["Charlie"]

    def test_the_newer_yunet_is_chosen_on_opencv_5(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        (models / "face_detection_yunet_2026may.onnx").write_bytes(b"y" * 2048)
        cv2 = sweep([[]] * 5)
        install(monkeypatch, cv2)
        faces.look_at_room(face_config, store)
        assert cv2.detector_path.endswith("face_detection_yunet_2026may.onnx")

    def test_the_older_yunet_is_chosen_on_opencv_4(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        (models / "face_detection_yunet_2026may.onnx").write_bytes(b"y" * 2048)
        cv2 = sweep([[]] * 5)
        cv2.__version__ = "4.14.0"
        install(monkeypatch, cv2)
        faces.look_at_room(face_config, store)
        assert cv2.detector_path.endswith("face_detection_yunet_2023mar.onnx")

    def test_a_model_that_will_not_load_is_not_a_crash(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        class Broken(FakeCv2):
            def FaceDetectorYN_create(self, *args):  # noqa: N802
                raise RuntimeError("truncated file")

        install(monkeypatch, Broken())
        look = faces.look_at_room(face_config, store)
        assert look.ok is False
        assert "would not load them" in look.error

    def test_an_exploding_camera_is_not_a_crash(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        class Exploding(FakeCv2):
            def VideoCapture(self, path, api):  # noqa: N802
                raise OSError("the USB bus went away")

        install(
            monkeypatch,
            Exploding(detector=FakeDetector([[]] * 5), recogniser=FakeRecogniser()),
        )
        look = faces.look_at_room(face_config, store)
        assert look.ok is False
        assert look.error

    def test_the_dictionary_carries_everything_the_page_needs(
        self, face_config, store, models, installed, sysfs, quick, monkeypatch
    ):
        install(monkeypatch, sweep([[Row(probe(0.9))]] * 5))
        payload = faces.look_at_room(face_config, store).to_dict()
        assert set(payload) == {
            "at", "people", "faces_seen", "unrecognised", "frames", "error", "ok"
        }
        assert set(payload["people"][0]) == {"person_id", "name", "email", "score"}


# ---------------------------------------------------------------------------
# embed_image()
# ---------------------------------------------------------------------------


def one_face(vector, *, width: float = 200.0) -> FakeCv2:
    return FakeCv2(
        detector=FakeDetector([[Row(vector, width=width)]]),
        recogniser=FakeRecogniser(),
        decoded=Frame(width=1200, height=1600),
    )


class TestEmbedImage:
    def test_a_photo_becomes_a_raw_128_number_vector(
        self, face_config, models, installed, monkeypatch
    ):
        raw = probe(0.9)
        install(monkeypatch, one_face(raw))
        values, model, error = faces.embed_image(JPEG, face_config)
        assert error == ""
        assert model == faces.MODEL_NAME == "sface-1"
        assert len(values) == 128
        length = math.sqrt(sum(value * value for value in values))
        assert length == pytest.approx(SFACE_NORM, abs=0.01), (
            "the vector must be handed on raw — the profile store normalises it"
        )
        assert values == pytest.approx(raw)

    def test_a_png_is_accepted_too(self, face_config, models, installed, monkeypatch):
        install(monkeypatch, one_face(probe(0.9)))
        _values, _model, error = faces.embed_image(PNG, face_config)
        assert error == ""

    def test_something_that_is_not_an_image_is_refused(
        self, face_config, models, installed, monkeypatch
    ):
        install(monkeypatch, one_face(probe(0.9)))
        values, model, error = faces.embed_image(b"<html>not a photo</html>", face_config)
        assert values == [] and model == ""
        assert "not a JPEG or a PNG" in error

    def test_an_empty_upload_is_refused(self, face_config, models, installed, monkeypatch):
        install(monkeypatch, one_face(probe(0.9)))
        _values, _model, error = faces.embed_image(b"", face_config)
        assert "empty" in error

    def test_an_enormous_upload_is_refused_before_it_is_decoded(
        self, face_config, models, installed, monkeypatch
    ):
        cv2 = one_face(probe(0.9))
        install(monkeypatch, cv2)
        huge = JPEG + b"\x00" * faces.MAX_PHOTO_BYTES
        _values, _model, error = faces.embed_image(huge, face_config)
        assert "bigger than" in error
        assert cv2.recogniser.crops == 0

    def test_a_file_opencv_cannot_decode_is_refused(
        self, face_config, models, installed, monkeypatch
    ):
        install(monkeypatch, FakeCv2(decoded=None))
        _values, _model, error = faces.embed_image(JPEG, face_config)
        assert "could not be decoded" in error

    def test_a_photo_with_no_face_is_refused(
        self, face_config, models, installed, monkeypatch
    ):
        install(
            monkeypatch,
            FakeCv2(
                detector=FakeDetector([[]]),
                recogniser=FakeRecogniser(),
                decoded=Frame(800, 600),
            ),
        )
        values, model, error = faces.embed_image(JPEG, face_config)
        assert values == [] and model == ""
        assert "No face was found" in error

    def test_a_photo_with_two_faces_is_refused(
        self, face_config, models, installed, monkeypatch
    ):
        install(
            monkeypatch,
            FakeCv2(
                detector=FakeDetector([[Row(probe(0.9)), Row(probe(0.5))]]),
                recogniser=FakeRecogniser(),
                decoded=Frame(800, 600),
            ),
        )
        values, _model, error = faces.embed_image(JPEG, face_config)
        assert values == []
        assert "2 faces" in error
        assert "one person at a time" in error

    def test_a_tiny_face_is_refused(self, face_config, models, installed, monkeypatch):
        install(monkeypatch, one_face(probe(0.9), width=40.0))
        _values, _model, error = faces.embed_image(JPEG, face_config)
        assert "too small" in error

    def test_an_enormous_photograph_is_shrunk_before_detection(
        self, face_config, models, installed, monkeypatch
    ):
        cv2 = one_face(probe(0.9))
        cv2.decoded = Frame(width=6000, height=4000)
        install(monkeypatch, cv2)
        _values, _model, error = faces.embed_image(JPEG, face_config)
        assert error == ""
        assert cv2.detector.sizes == [(faces.MAX_PHOTO_SIDE, 1706)]

    def test_a_decompression_bomb_is_refused(
        self, face_config, models, installed, monkeypatch
    ):
        cv2 = one_face(probe(0.9))
        cv2.decoded = Frame(width=40000, height=40000)
        install(monkeypatch, cv2)
        _values, _model, error = faces.embed_image(JPEG, face_config)
        assert "far too large" in error

    def test_development_mode_refuses_to_invent_a_vector(
        self, face_config, models, installed, monkeypatch
    ):
        face_config.update({"DEV_MODE": True})
        install(monkeypatch, one_face(probe(0.9)))
        values, _model, error = faces.embed_image(JPEG, face_config)
        assert values == []
        assert "Development mode" in error

    def test_missing_opencv_is_explained(self, face_config, models, monkeypatch):
        deps.set_probe_for_tests("numpy", True)
        deps.set_probe_for_tests("opencv", False, "“opencv-python-headless” is not installed.")
        try:
            _values, _model, error = faces.embed_image(JPEG, face_config)
        finally:
            deps.refresh()
        assert "opencv-python-headless" in error

    def test_missing_models_are_explained(
        self, face_config, minutes_home, installed, monkeypatch
    ):
        install(monkeypatch, one_face(probe(0.9)))
        _values, _model, error = faces.embed_image(JPEG, face_config)
        assert "face_detection_yunet_2023mar.onnx" in error

    def test_enrolment_does_not_need_the_switch_to_be_on(
        self, config, models, installed, monkeypatch
    ):
        """An administrator enrols people first and switches the feature on after."""
        install(monkeypatch, one_face(probe(0.9)))
        _values, _model, error = faces.embed_image(JPEG, config)
        assert error == ""

    def test_a_wrong_shaped_model_is_caught(
        self, face_config, models, installed, monkeypatch
    ):
        install(monkeypatch, one_face([0.1, 0.2, 0.3]))
        values, _model, error = faces.embed_image(JPEG, face_config)
        assert values == []
        assert "unexpected result" in error

    def test_a_vector_can_be_stored_and_found_again(
        self, face_config, models, installed, store, monkeypatch
    ):
        """The whole point: enrol from a photo, then be recognised in the room."""
        sam, _error = store.add("Sam")
        install(monkeypatch, one_face(probe(0.0, 0.0)))
        values, model, error = faces.embed_image(JPEG, face_config)
        assert error == ""
        added, why = store.add_vector(sam.id, KIND_FACE, model, values)
        assert added, why
        match = store.match(KIND_FACE, model, values, threshold=0.9)
        assert match.ok and match.name == "Sam"


# ---------------------------------------------------------------------------
# Finding the camera
# ---------------------------------------------------------------------------


class TestCameraPaths:
    def test_metadata_nodes_are_dropped(self, config, sysfs):
        assert faces._camera_paths(config) == ["/dev/video0"]

    def test_the_conference_bar_is_preferred_over_a_webcam(
        self, config, tmp_path, monkeypatch
    ):
        root = tmp_path / "v4l"
        for name, device in (("video0", "Integrated Webcam"), ("video1", "Poly Studio")):
            node = root / name
            node.mkdir(parents=True)
            (node / "name").write_text(device, encoding="utf-8")
            (node / "index").write_text("0", encoding="utf-8")
        monkeypatch.setattr(faces, "_SYSFS_VIDEO", root)
        assert faces._camera_paths(config) == ["/dev/video1", "/dev/video0"]

    def test_a_configured_path_wins_outright(self, config, sysfs, tmp_path):
        node = tmp_path / "video9"
        node.write_text("", encoding="utf-8")
        config.update({"CAMERA_DEVICE": str(node)})
        assert faces._camera_paths(config) == [str(node)]

    def test_a_configured_path_that_is_gone_finds_nothing(self, config, sysfs):
        config.update({"CAMERA_DEVICE": "/dev/video42"})
        assert faces._camera_paths(config) == []

    def test_no_sysfs_at_all_is_not_an_error(self, config, tmp_path, monkeypatch):
        monkeypatch.setattr(faces, "_SYSFS_VIDEO", tmp_path / "missing")
        assert faces._camera_paths(config) == []
