"""Checks for scripts/install-minutes.sh.

The installer is the one part of the appliance that is *meant* to reach out to
the internet, so the interesting behaviour is all in what it does with what
comes back: a truncated file, a login page where a model should be, a checksum
that does not match, a machine too slow to use the thing being installed. None
of that can be tested against the real internet, and a test that downloaded
400 MB of models would be useless anyway.

So the script is driven as a subprocess against a throw-away tree with a stub
``curl`` (and, where it matters, a stub ``df`` and a stub virtual environment)
earlier on ``PATH``. The stub is told what to "download" by a small JSON spec:
how the file should start and how big it should claim to be. The files it
writes are sparse, so a 38 MB model costs nothing.

The one thing that is checked against the real modules rather than a fixture is
the *names*: tests/test_install_minutes.py asks ``app.minutes.faces`` and
``app.minutes.voiceprint`` what they glob for, so renaming a model on either
side breaks this file rather than silently producing an appliance that
downloads two files it will never look at.

Note that ``TestShellScripts.test_help_output_works_without_a_configuration``
in test_project.py names four scripts by hand and does not include this one, so
the ``--help`` guarantee is kept here instead.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "install-minutes.sh"
LIB = ROOT / "scripts" / "lib-room.sh"

# The four model files a full run installs, with the sizes the script accepts.
YUNET_5 = "face_detection_yunet_2026may.onnx"
YUNET_4 = "face_detection_yunet_2023mar.onnx"
SFACE = "face_recognition_sface_2021dec.onnx"
TITANET = "nemo_en_titanet_small.onnx"
GGML_BASE = "ggml-base.en.bin"
GGML_TINY = "ggml-tiny.en.bin"

#: A real ONNX file starts with protobuf field 1 (ir_version) as a varint, and
#: the OpenCV Zoo's models then name the exporter. Taken from the real files.
ONNX_HEAD = "0806120770797463726368"
#: whisper.cpp's GGML_FILE_MAGIC, 0x67676d6c, little-endian on disk.
GGML_HEAD = "6c6d6767"

#: What each file really weighs, near enough for a size check to pass.
REAL_SIZES = {
    YUNET_5: 229_738,
    YUNET_4: 232_589,
    SFACE: 38_696_353,
    TITANET: 40_257_283,
    GGML_BASE: 147_964_211,
    GGML_TINY: 77_691_713,
}


# ---------------------------------------------------------------------------
# The stubs
# ---------------------------------------------------------------------------

CURL_STUB = r'''#!/usr/bin/env python3
"""Stands in for curl. Serves whatever CURL_SPEC says, and logs every URL."""
import json
import os
import sys

spec = json.load(open(os.environ["CURL_SPEC"], encoding="utf-8"))
argv = sys.argv[1:]
url = argv[-1] if argv else ""
out = ""
for index, item in enumerate(argv):
    if item in ("-o", "--output") and index + 1 < len(argv):
        out = argv[index + 1]

with open(os.environ["CURL_LOG"], "a", encoding="utf-8") as log:
    log.write(url + "\n")

entry = spec.get(url.rsplit("/", 1)[-1])
if entry is None or "fail" in entry:
    code = 22 if entry is None else int(entry["fail"])
    sys.stderr.write("curl: (%d) The requested URL returned error: 404\n" % code)
    sys.exit(code)

head = bytes.fromhex(entry.get("head", ""))
size = int(entry.get("size", len(head)))
if out:
    with open(out, "wb") as handle:
        handle.write(head)
        if size > len(head):
            handle.truncate(size)  # sparse: a 38 MB model costs no disk
else:
    sys.stdout.write(entry.get("body", ""))
'''

# The virtual environment's python. It answers the two questions the installer
# asks: what kind of machine is this, and which OpenCV is installed.
PYTHON_STUB = r'''#!/usr/bin/env python3
import os
import sys

if "-c" in sys.argv[1:]:
    major = os.environ.get("STUB_CV_MAJOR", "")
    if not major:
        sys.exit(1)
    print(major)
    sys.exit(0)

sys.stdin.read()
if os.environ.get("STUB_HARDWARE_FAILS"):
    sys.exit(1)
print("profile=%s" % os.environ.get("STUB_PROFILE", "balanced"))
print("generation=%s" % os.environ.get("STUB_PI_GENERATION", "5"))
print("description=%s" % os.environ.get("STUB_MACHINE", "Raspberry Pi 5 Model B"))
'''

PIP_STUB = r'''#!/usr/bin/env python3
"""Stands in for pip. Logs each install, and fails for PIP_FAILS_FOR."""
import os
import sys

argv = sys.argv[1:]
with open(os.environ["PIP_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(argv) + "\n")

doomed = os.environ.get("PIP_FAILS_FOR", "")
if doomed and any(doomed in item for item in argv):
    sys.stderr.write("ERROR: could not build a wheel for %s\n" % doomed)
    sys.exit(1)
'''

DF_STUB = r'''#!/bin/sh
# Stands in for df, reporting DF_FREE_KB free on every filesystem.
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "/dev/stub 100000000 1 ${DF_FREE_KB:-99000000} 1% /"
'''

WHISPER_STUB = "#!/bin/sh\necho 'usage: whisper-cli [options] file0'\n"


def _write_executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


class Appliance:
    """A throw-away appliance the installer can be pointed at."""

    def __init__(self, tmp_path: Path, script: Path = SCRIPT):
        # Under a subdirectory of its own: tests that also use the suite's
        # room_dirs fixture share tmp_path with it.
        base = tmp_path / "install"
        base.mkdir(parents=True, exist_ok=True)
        self.script = script
        self.home = base / "home"
        self.var = base / "var"
        self.stub_bin = base / "stub-bin"
        self.venv = base / "venv"
        self.spec_file = base / "curl-spec.json"
        self.curl_log = base / "curl.log"
        self.pip_log = base / "pip.log"
        for directory in (self.home, self.var, self.stub_bin):
            directory.mkdir(parents=True, exist_ok=True)

        _write_executable(self.stub_bin / "curl", CURL_STUB)
        # Already-installed whisper.cpp: without it the script would try to
        # build the real thing, which is not a unit test.
        _write_executable(self.stub_bin / "whisper-cli", WHISPER_STUB)
        _write_executable(self.venv / "bin" / "python3", PYTHON_STUB)
        _write_executable(self.venv / "bin" / "pip", PIP_STUB)

        self.spec: dict[str, dict] = {}
        self.env_overrides: dict[str, str] = {}

    # -- the fixtures the stub curl serves --------------------------------

    def serve(self, name: str, *, head: str = ONNX_HEAD, size: int | None = None):
        if size is None:
            size = REAL_SIZES.get(name, 1024)
        self.spec[name] = {"head": head, "size": size}

    def serve_all(self, *, yunet: str = YUNET_5, whisper: str = GGML_BASE):
        self.serve(yunet)
        self.serve(SFACE)
        self.serve(TITANET)
        self.serve(whisper, head=GGML_HEAD)

    def refuse(self, name: str, code: int = 22):
        self.spec[name] = {"fail": code}

    # -- running it -------------------------------------------------------

    @property
    def models(self) -> Path:
        return self.var / "minutes" / "models"

    def run(self, *args: str, **overrides: str) -> subprocess.CompletedProcess:
        self.spec_file.write_text(json.dumps(self.spec), encoding="utf-8")
        env = {
            "PATH": f"{self.stub_bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.home),
            "ROOM_APPLIANCE_VAR": str(self.var),
            "CURL_SPEC": str(self.spec_file),
            "CURL_LOG": str(self.curl_log),
            "PIP_LOG": str(self.pip_log),
            "LANG": "C.UTF-8",
        }
        env.update(self.env_overrides)
        env.update(overrides)
        return subprocess.run(
            [str(self.script), "--venv", str(self.venv), *args],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(ROOT),
            env=env,
        )

    # -- what happened ----------------------------------------------------

    def urls(self) -> list[str]:
        if not self.curl_log.exists():
            return []
        return [u for u in self.curl_log.read_text(encoding="utf-8").splitlines() if u]

    def downloaded(self) -> list[str]:
        """Just the model URLs, so the release check does not count."""
        return [u.rsplit("/", 1)[-1] for u in self.urls() if u.rsplit("/", 1)[-1] in self.spec]

    def forget_downloads(self) -> None:
        self.curl_log.write_text("", encoding="utf-8")

    def pip_installs(self) -> list[str]:
        if not self.pip_log.exists():
            return []
        return [line for line in self.pip_log.read_text(encoding="utf-8").splitlines() if line]

    def files(self) -> list[str]:
        if not self.models.is_dir():
            return []
        return sorted(p.name for p in self.models.iterdir())


@pytest.fixture()
def appliance(tmp_path):
    return Appliance(tmp_path)


# ---------------------------------------------------------------------------
# --help
# ---------------------------------------------------------------------------


class TestHelp:
    def test_it_works_with_no_configuration_venv_network_or_hardware(self, tmp_path):
        """The same guarantee test_project.py makes for the other scripts."""
        bare = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
        }
        result = subprocess.run(
            [str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(tmp_path),
            env=bare,
        )
        assert result.returncode == 0, result.stderr
        assert len(result.stdout) > 80
        for flag in ("--whisper", "--models-only", "--pip-only", "--venv", "--quiet"):
            assert flag in result.stdout, f"--help does not mention {flag}"

    def test_short_form_works_too(self, tmp_path):
        result = subprocess.run(
            [str(SCRIPT), "-h"], capture_output=True, text=True, timeout=60,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
        )
        assert result.returncode == 0
        assert "install-minutes.sh" in result.stdout

    def test_the_options_it_documents_are_the_options_it_accepts(self):
        """A flag in the help block that the option loop rejects is a lie."""
        text = SCRIPT.read_text(encoding="utf-8")
        header = text.split("set -uo pipefail", 1)[0]
        loop = text.split("while [ $# -gt 0 ]", 1)[1].split("done", 1)[0]
        for flag in sorted(set(re.findall(r"^#\s+(--[a-z-]+)", header, re.M))):
            assert flag in loop, f"{flag} is documented but not accepted"

    def test_bad_options_are_refused(self, appliance):
        result = appliance.run("--wat")
        assert result.returncode != 0
        assert "Unknown option" in result.stdout

    def test_an_impossible_whisper_size_is_refused(self, appliance):
        result = appliance.run("--whisper", "enormous.en")
        assert result.returncode != 0
        assert "tiny.en" in result.stdout and "base.en" in result.stdout

    def test_the_two_only_flags_are_mutually_exclusive(self, appliance):
        result = appliance.run("--models-only", "--pip-only")
        assert result.returncode != 0
        assert "cannot both be used" in result.stdout


# ---------------------------------------------------------------------------
# The names have to be the ones the appliance looks for
# ---------------------------------------------------------------------------


class TestTheFilenamesMatchWhatTheAppLooksFor:
    """Asserted with the app's own constants, not with copies of them."""

    def test_the_face_models_are_found_by_faces_py(self, appliance, room_dirs):
        from app.minutes import faces

        appliance.var = room_dirs["var"]
        appliance.serve_all()
        result = appliance.run("--models-only", STUB_CV_MAJOR="5")
        assert result.returncode == 0, result.stdout + result.stderr

        report = {row["role"]: row for row in faces.models_report()}
        assert report["detector"]["present"], report["detector"]
        assert report["recogniser"]["present"], report["recogniser"]
        assert fnmatch.fnmatch(report["detector"]["file"], faces._YUNET_GLOB)
        assert fnmatch.fnmatch(report["recogniser"]["file"], faces._SFACE_GLOB)

    def test_the_speaker_model_is_found_by_voiceprint_py(self, appliance, room_dirs):
        from app.minutes import deps, voiceprint

        appliance.var = room_dirs["var"]
        appliance.serve_all()
        assert appliance.run("--models-only", STUB_CV_MAJOR="5").returncode == 0

        deps.set_probe_for_tests("sherpa_onnx", True)
        try:
            found = voiceprint._titanet_model()
        finally:
            deps.refresh()
        assert found is not None, appliance.files()
        assert found.name == TITANET

    def test_the_speech_model_is_found_by_transcribe_py(self, appliance, room_dirs):
        from app.minutes import transcribe

        appliance.var = room_dirs["var"]
        appliance.serve_all()
        assert appliance.run("--models-only", STUB_CV_MAJOR="5").returncode == 0

        found = transcribe.whisper_model(_NoSettings())
        assert found is not None, appliance.files()
        assert found.name == GGML_BASE

    def test_the_yunet_file_follows_the_installed_opencv(self, appliance):
        """faces.py picks a different YuNet per OpenCV major; so does this."""
        appliance.serve_all(yunet=YUNET_4)
        assert appliance.run("--models-only", STUB_CV_MAJOR="4").returncode == 0
        assert YUNET_4 in appliance.files()
        assert YUNET_5 not in appliance.files()

    def test_the_whisper_binary_names_are_the_ones_deps_probes_for(self):
        from app.minutes import deps

        text = SCRIPT.read_text(encoding="utf-8")
        listed = re.search(r"^WHISPER_ALIASES=\(([^)]*)\)", text, re.M)
        assert listed, "the script no longer declares WHISPER_ALIASES"
        assert tuple(listed.group(1).split()) == deps._BINARY_ALIASES["whisper-cpp"]


class _NoSettings:
    """The bare minimum transcribe.whisper_model needs to be asked a question."""

    def str_(self, key):  # noqa: D102 - trivial
        return ""

    def get(self, key, fallback=None):  # noqa: D102 - trivial
        return ""


# ---------------------------------------------------------------------------
# Downloading, and refusing to
# ---------------------------------------------------------------------------


class TestDownloads:
    def test_a_full_models_run_writes_the_four_files(self, appliance):
        appliance.serve_all()
        result = appliance.run("--models-only", STUB_CV_MAJOR="5")
        assert result.returncode == 0, result.stdout + result.stderr
        assert set(appliance.files()) == {
            YUNET_5, SFACE, TITANET, GGML_BASE, "manifest.sha256",
        }

    def test_everything_is_private_in_a_private_directory(self, appliance):
        appliance.serve_all()
        appliance.run("--models-only", STUB_CV_MAJOR="5")
        assert stat.S_IMODE(appliance.models.stat().st_mode) == 0o700
        assert stat.S_IMODE((appliance.models.parent).stat().st_mode) == 0o700
        for name in appliance.files():
            mode = stat.S_IMODE((appliance.models / name).stat().st_mode)
            assert mode == 0o600, f"{name} is mode {mode:o}"

    def test_a_second_run_downloads_nothing(self, appliance):
        appliance.serve_all()
        assert appliance.run("--models-only", STUB_CV_MAJOR="5").returncode == 0
        before = {name: (appliance.models / name).stat().st_mtime_ns
                  for name in appliance.files()}

        appliance.forget_downloads()
        again = appliance.run("--models-only", STUB_CV_MAJOR="5")
        assert again.returncode == 0, again.stdout
        assert appliance.downloaded() == [], "it downloaded something twice"
        assert again.stdout.count("is already here") == 4
        assert "left alone" in again.stdout
        after = {name: (appliance.models / name).stat().st_mtime_ns
                 for name in appliance.files()}
        assert before == after, "a file was rewritten on the second run"

    def test_force_replaces_what_is_already_there(self, appliance):
        appliance.serve_all()
        appliance.run("--models-only", STUB_CV_MAJOR="5")
        appliance.forget_downloads()

        result = appliance.run("--models-only", "--force", STUB_CV_MAJOR="5")
        assert result.returncode == 0, result.stdout
        assert sorted(appliance.downloaded()) == sorted([YUNET_5, SFACE, TITANET, GGML_BASE])

    def test_a_download_that_fails_leaves_the_rest_installed(self, appliance):
        appliance.serve_all()
        appliance.refuse(TITANET)
        result = appliance.run("--models-only", STUB_CV_MAJOR="5")
        assert result.returncode == 1
        assert TITANET not in appliance.files()
        assert SFACE in appliance.files()
        assert GGML_BASE in appliance.files()
        assert "Could not download" in result.stdout
        # Every failure names the next thing to try.
        assert "run this" in result.stdout


class TestRejectedDownloads:
    """Nothing half-formed, and nothing that is not what it claims to be."""

    @staticmethod
    def _leftovers(appliance) -> list[str]:
        return [name for name in appliance.files() if name.startswith(".")]

    def test_an_html_error_page_is_rejected(self, appliance):
        appliance.serve_all()
        # A captive portal or a login page, exactly the size a model would be.
        appliance.spec[YUNET_5] = {
            "head": b"<!DOCTYPE html><html><title>Sign in</title>".hex(),
            "size": REAL_SIZES[YUNET_5],
        }
        result = appliance.run("--models-only", STUB_CV_MAJOR="5")
        assert result.returncode == 1
        assert "web page" in result.stdout
        assert YUNET_5 not in appliance.files()
        assert self._leftovers(appliance) == []

    def test_a_truncated_download_is_rejected(self, appliance):
        appliance.serve_all()
        appliance.spec[SFACE] = {"head": ONNX_HEAD, "size": 4096}
        result = appliance.run("--models-only", STUB_CV_MAJOR="5")
        assert result.returncode == 1
        assert "should be between" in result.stdout
        assert SFACE not in appliance.files()
        assert self._leftovers(appliance) == []

    def test_a_file_that_is_far_too_big_is_rejected(self, appliance):
        appliance.serve_all()
        appliance.spec[TITANET] = {"head": ONNX_HEAD, "size": 900_000_000}
        result = appliance.run("--models-only", STUB_CV_MAJOR="5")
        assert result.returncode == 1
        assert "should be between" in result.stdout
        assert TITANET not in appliance.files()

    def test_something_that_is_not_an_onnx_model_is_rejected(self, appliance):
        appliance.serve_all()
        # Right size, right name, wrong bytes: a tarball, say.
        appliance.spec[SFACE] = {"head": "1f8b0800", "size": REAL_SIZES[SFACE]}
        result = appliance.run("--models-only", STUB_CV_MAJOR="5")
        assert result.returncode == 1
        assert "does not start like" in result.stdout
        assert "ONNX" in result.stdout
        assert SFACE not in appliance.files()
        assert self._leftovers(appliance) == []

    def test_something_that_is_not_a_ggml_model_is_rejected(self, appliance):
        appliance.serve_all()
        appliance.spec[GGML_BASE] = {"head": ONNX_HEAD, "size": REAL_SIZES[GGML_BASE]}
        result = appliance.run("--models-only", STUB_CV_MAJOR="5")
        assert result.returncode == 1
        assert "ggml" in result.stdout
        assert GGML_BASE not in appliance.files()

    def test_a_gguf_model_is_accepted(self, appliance):
        """whisper.cpp reads both magics; so does the check."""
        appliance.serve_all()
        appliance.spec[GGML_BASE] = {"head": "47475546", "size": REAL_SIZES[GGML_BASE]}
        assert appliance.run("--models-only", STUB_CV_MAJOR="5").returncode == 0
        assert GGML_BASE in appliance.files()

    def test_a_broken_file_that_is_already_there_is_not_deleted(self, appliance):
        """It may be the only copy; say so and let the administrator decide."""
        appliance.models.mkdir(parents=True)
        rubbish = appliance.models / SFACE
        rubbish.write_bytes(b"not a model")
        appliance.serve_all()

        result = appliance.run("--models-only", STUB_CV_MAJOR="5")
        assert result.returncode == 1
        assert rubbish.read_bytes() == b"not a model"
        assert "--force" in result.stdout
        assert SFACE not in appliance.downloaded()


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------


def _sha_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class TestChecksums:
    def test_a_manifest_records_what_arrived(self, appliance):
        appliance.serve_all()
        assert appliance.run("--models-only", STUB_CV_MAJOR="5").returncode == 0

        manifest = appliance.models / "manifest.sha256"
        recorded = dict(
            reversed(line.split(None, 1))  # "<sha>  <name>" -> name, sha
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        recorded = {name.strip(): sha for name, sha in recorded.items()}
        assert set(recorded) == {YUNET_5, SFACE, TITANET, GGML_BASE}
        for name, sha in recorded.items():
            assert sha == _sha_of(appliance.models / name), name

    def test_the_manifest_can_be_checked_with_sha256sum(self, appliance):
        appliance.serve_all()
        appliance.run("--models-only", STUB_CV_MAJOR="5")
        if not shutil.which("sha256sum"):
            pytest.skip("sha256sum is not installed")
        checked = subprocess.run(
            ["sha256sum", "-c", "manifest.sha256"],
            cwd=str(appliance.models), capture_output=True, text=True, timeout=120,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr

    def test_a_model_that_changed_underneath_us_is_reported(self, appliance):
        appliance.serve_all()
        appliance.run("--models-only", STUB_CV_MAJOR="5")

        # Same size, same magic, different bytes: the case a size check misses.
        swapped = appliance.models / TITANET
        with open(swapped, "r+b") as handle:
            handle.seek(1024)
            handle.write(b"tampered")

        appliance.forget_downloads()
        result = appliance.run("--models-only", STUB_CV_MAJOR="5")
        assert "has changed since it was installed" in result.stdout
        assert appliance.downloaded() == [], "it should not replace it on its own"
        assert "--force" in result.stdout


class TestPinnedChecksums:
    """scripts/minutes-models.sha256 is optional; when it exists it is law."""

    @staticmethod
    def _relocate(tmp_path: Path) -> Appliance:
        """A copy of the script, so a pins file can be created beside it."""
        scripts = tmp_path / "appliance" / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(SCRIPT, scripts / SCRIPT.name)
        shutil.copy2(LIB, scripts / LIB.name)
        shutil.copy2(ROOT / "requirements-minutes.txt", tmp_path / "appliance")
        return Appliance(tmp_path, script=scripts / SCRIPT.name)

    def test_a_matching_pin_is_accepted(self, tmp_path):
        appliance = self._relocate(tmp_path)
        appliance.serve_all()
        assert appliance.run("--models-only", STUB_CV_MAJOR="5").returncode == 0
        real = _sha_of(appliance.models / SFACE)

        # Pin it, throw the file away, and fetch it again.
        pins = appliance.script.parent / "minutes-models.sha256"
        pins.write_text(f"# pinned by hand\n{real}  {SFACE}\n", encoding="utf-8")
        (appliance.models / SFACE).unlink()

        result = appliance.run("--models-only", STUB_CV_MAJOR="5")
        assert result.returncode == 0, result.stdout
        assert SFACE in appliance.files()

    def test_a_mismatched_pin_is_refused(self, tmp_path):
        appliance = self._relocate(tmp_path)
        appliance.serve_all()
        pins = appliance.script.parent / "minutes-models.sha256"
        pins.write_text(f"{'0' * 64}  {SFACE}\n", encoding="utf-8")

        result = appliance.run("--models-only", STUB_CV_MAJOR="5")
        assert result.returncode == 1
        assert "checksum" in result.stdout
        assert SFACE not in appliance.files(), "a mismatched file must not be kept"
        # The files that are not pinned still arrive.
        assert TITANET in appliance.files()

    def test_the_repository_ships_no_invented_pins(self):
        """A wrong pin rejects every genuine download, so there is no file
        until somebody computes one from an appliance they trust."""
        pins = ROOT / "scripts" / "minutes-models.sha256"
        if not pins.exists():
            return
        for line in pins.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                assert re.match(r"^[0-9a-f]{64}\s", line), f"not a checksum: {line}"


# ---------------------------------------------------------------------------
# Hardware, and knowing when not to install something
# ---------------------------------------------------------------------------


class TestHardware:
    def test_a_pi_5_gets_base_en(self, appliance):
        appliance.serve_all(whisper=GGML_BASE)
        result = appliance.run("--models-only", STUB_PI_GENERATION="5")
        assert "base.en" in result.stdout
        assert GGML_BASE in appliance.files()

    def test_a_pi_4_gets_tiny_en(self, appliance):
        appliance.serve_all(whisper=GGML_TINY)
        result = appliance.run(
            "--models-only", STUB_PI_GENERATION="4", STUB_MACHINE="Raspberry Pi 4 Model B"
        )
        assert "tiny.en" in result.stdout, result.stdout
        assert GGML_TINY in appliance.files()

    def test_whisper_can_be_asked_for_by_hand(self, appliance):
        appliance.serve_all(whisper="ggml-small.en.bin")
        appliance.serve("ggml-small.en.bin", head=GGML_HEAD, size=487_601_967)
        result = appliance.run(
            "--models-only", "--whisper", "small.en", STUB_PI_GENERATION="4"
        )
        assert result.returncode == 0, result.stdout
        assert "ggml-small.en.bin" in appliance.files()

    def test_a_low_tier_machine_gets_no_speech_engine_at_all(self, appliance):
        appliance.serve_all()
        result = appliance.run(
            "--models-only",
            STUB_PROFILE="low",
            STUB_PI_GENERATION="3",
            STUB_MACHINE="Raspberry Pi 3 Model B · 4 cores · 0.9 GB",
        )
        assert result.returncode == 0, result.stdout
        # It says why, in terms of this machine.
        assert "Raspberry Pi 3" in result.stdout
        assert "most of a working day" in result.stdout
        # No speech model, and no whisper.cpp.
        assert not any(name.startswith("ggml-") for name in appliance.files())
        assert not any(url.endswith(".bin") for url in appliance.urls())
        # Everything else is still installed.
        assert SFACE in appliance.files()
        assert TITANET in appliance.files()

    def test_the_low_tier_refusal_stands_even_when_a_size_was_asked_for(self, appliance):
        appliance.serve_all()
        result = appliance.run("--models-only", "--whisper", "tiny.en", STUB_PROFILE="low")
        assert result.returncode == 0
        assert not any(name.startswith("ggml-") for name in appliance.files())
        assert "performance profile in Settings" in result.stdout

    def test_a_low_tier_machine_skips_the_python_speech_engines(self, appliance):
        appliance.serve_all()
        appliance.run("--pip-only", STUB_PROFILE="low")
        installed = " ".join(appliance.pip_installs())
        assert "anthropic" in installed
        assert "faster-whisper" not in installed
        assert "vosk" not in installed

    def test_unreadable_hardware_is_cautious_rather_than_fatal(self, appliance):
        appliance.serve_all(whisper=GGML_TINY)
        result = appliance.run("--models-only", STUB_HARDWARE_FAILS="1")
        assert result.returncode == 0, result.stdout
        assert "Could not ask the appliance" in result.stdout
        assert GGML_TINY in appliance.files(), "it should fall back to the small model"


# ---------------------------------------------------------------------------
# Doing only part of the job
# ---------------------------------------------------------------------------


class TestPartialInstalls:
    def test_models_only_installs_no_packages(self, appliance):
        appliance.serve_all()
        result = appliance.run("--models-only", STUB_CV_MAJOR="5")
        assert result.returncode == 0
        assert appliance.pip_installs() == []
        assert SFACE in appliance.files()

    def test_pip_only_downloads_nothing(self, appliance):
        appliance.serve_all()
        result = appliance.run("--pip-only")
        assert result.returncode == 0, result.stdout
        assert appliance.downloaded() == []
        assert not appliance.models.exists()
        assert appliance.pip_installs()

    def test_the_packages_are_installed_one_group_at_a_time(self, appliance):
        appliance.run("--pip-only")
        groups = appliance.pip_installs()
        assert len(groups) >= 5, groups
        joined = " ".join(groups)
        for package in (
            "anthropic", "numpy", "opencv-python-headless",
            "sherpa-onnx", "webrtcvad-wheels", "faster-whisper", "vosk",
        ):
            assert package in joined, f"{package} was never installed"

    def test_the_versions_come_from_requirements_minutes_txt(self, appliance):
        appliance.run("--pip-only")
        joined = " ".join(appliance.pip_installs())
        wanted = [
            line.split("#")[0].strip()
            for line in (ROOT / "requirements-minutes.txt").read_text().splitlines()
            if line.split("#")[0].strip()
        ]
        assert wanted, "requirements-minutes.txt has no requirements in it"
        for requirement in wanted:
            assert requirement in joined, f"{requirement} was not installed as pinned"

    def test_one_broken_wheel_does_not_lose_the_others(self, appliance):
        """anthropic alone makes summaries work; opencv must not take it down."""
        result = appliance.run("--pip-only", PIP_FAILS_FOR="opencv-python-headless")
        installed = " ".join(appliance.pip_installs())
        assert "anthropic" in installed
        assert "sherpa-onnx" in installed
        assert "face recognition could not be installed" in result.stdout
        assert "To retry" in result.stdout
        assert result.returncode == 0, "one group failing is not a failed install"

    def test_a_missing_virtualenv_says_which_script_to_run_first(self, appliance, tmp_path):
        result = appliance.run("--pip-only", "--venv", str(tmp_path / "nowhere"))
        assert result.returncode != 0
        assert "install.sh" in result.stdout


# ---------------------------------------------------------------------------
# Disk space
# ---------------------------------------------------------------------------


class TestDiskSpace:
    def test_it_refuses_before_downloading_anything(self, appliance):
        _write_executable(appliance.stub_bin / "df", DF_STUB)
        appliance.serve_all()
        result = appliance.run("--models-only", DF_FREE_KB="20000", STUB_CV_MAJOR="5")
        assert result.returncode == 1
        assert "not enough disk space" in result.stdout
        assert "MB free" in result.stdout
        assert appliance.downloaded() == [], "it downloaded despite having no room"
        assert [f for f in appliance.files() if f != "manifest.sha256"] == []

    def test_plenty_of_room_is_not_refused(self, appliance):
        _write_executable(appliance.stub_bin / "df", DF_STUB)
        appliance.serve_all()
        result = appliance.run("--models-only", DF_FREE_KB="80000000", STUB_CV_MAJOR="5")
        assert result.returncode == 0, result.stdout
        assert SFACE in appliance.files()

    def test_a_df_that_says_nothing_useful_does_not_block_the_install(self, appliance):
        _write_executable(appliance.stub_bin / "df", "#!/bin/sh\nexit 1\n")
        appliance.serve_all()
        result = appliance.run("--models-only", STUB_CV_MAJOR="5")
        assert result.returncode == 0, result.stdout
        assert SFACE in appliance.files()


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class TestOutput:
    def test_quiet_says_nothing_when_all_is_well(self, appliance):
        """install.sh --with-minutes runs it this way, inside its own step."""
        appliance.serve_all()
        result = appliance.run("--models-only", "--quiet", STUB_CV_MAJOR="5")
        assert result.returncode == 0, result.stdout
        # The suite runs as whoever runs it; the root notice is a real warning.
        noise = [
            line for line in result.stdout.splitlines()
            if line.strip() and "Running as root" not in line
        ]
        assert noise == [], noise

    def test_quiet_still_reports_problems(self, appliance):
        appliance.serve_all()
        appliance.refuse(SFACE)
        result = appliance.run("--models-only", "--quiet", STUB_CV_MAJOR="5")
        assert result.returncode == 1
        assert "Could not download" in result.stdout

    def test_install_sh_calls_it_the_way_it_expects_to_be_called(self):
        """install.sh --with-minutes is the only caller; keep them agreed."""
        installer = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        assert '"$HERE/install-minutes.sh" --quiet' in installer
        assert "--with-minutes" in installer
