"""Whole-project checks: shell scripts, unit files, and generated docs.

These catch the mistakes that unit tests cannot: a shell script with a syntax
error, a systemd unit that names a script which does not exist, a JavaScript
file that will not parse in the browser, or documentation that has drifted from
the configuration schema.
"""

from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHELL_SCRIPTS = sorted(
    [p for p in (ROOT / "scripts").iterdir() if p.is_file() and p.suffix in ("", ".sh")]
    + [ROOT / "install.sh"]
)
UNIT_FILES = sorted((ROOT / "systemd").glob("room-*"))
JS_FILES = sorted((ROOT / "app" / "static").glob("*.js"))
PY_FILES = sorted((ROOT / "app").rglob("*.py")) + sorted((ROOT / "scripts").glob("*.py"))


class TestShellScripts:
    def test_scripts_were_found(self):
        assert len(SHELL_SCRIPTS) >= 8, "the shell scripts are not being discovered"

    @pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
    def test_syntax_is_valid(self, script):
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, f"{script.name}:\n{result.stderr}"

    @pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
    def test_scripts_are_executable(self, script):
        assert script.stat().st_mode & 0o111, f"{script.name} is not executable"

    @pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
    def test_scripts_have_a_shebang(self, script):
        first = script.read_text(encoding="utf-8").splitlines()[0]
        assert first.startswith("#!"), f"{script.name} has no shebang"

    @pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
    def test_scripts_set_safe_options(self, script):
        """`set -u` catches the unbound-variable bugs that break a script at 3am."""
        text = script.read_text(encoding="utf-8")
        if "lib-room.sh" in script.name:
            return  # a sourced library must not change the caller's options
        assert "set -uo pipefail" in text or "set -euo pipefail" in text, script.name

    @pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
    def test_shellcheck_is_clean_where_available(self, script):
        if not shutil.which("shellcheck"):
            pytest.skip("shellcheck is not installed")
        result = subprocess.run(
            # -x follows the sourced lib-room.sh; style is the strictest level
            # that is still all signal.
            ["shellcheck", "-x", "--severity=style", "--shell=bash", str(script)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, f"{script.name}:\n{result.stdout}"

    def test_help_output_works_without_a_configuration(self):
        """`--help` must never need a config file, a venv or hardware."""
        for name in (
            "install.sh", "dev-run.sh", "uninstall.sh", "diagnose-remote.sh",
            "install-minutes.sh",
        ):
            script = ROOT / "scripts" / name
            result = subprocess.run(
                [str(script), "--help"], capture_output=True, text=True, timeout=60
            )
            assert result.returncode == 0, f"{name} --help failed: {result.stderr}"
            assert len(result.stdout) > 80, f"{name} --help printed almost nothing"


class TestSystemdUnits:
    def test_units_were_found(self):
        assert len(UNIT_FILES) == 8, [p.name for p in UNIT_FILES]

    @pytest.mark.parametrize("unit", UNIT_FILES, ids=lambda p: p.name)
    def test_units_parse(self, unit):
        parser = configparser.ConfigParser(strict=False)
        parser.read(unit, encoding="utf-8")
        assert "Unit" in parser.sections(), unit.name

    @pytest.mark.parametrize(
        "unit", [u for u in UNIT_FILES if u.suffix == ".service"], ids=lambda p: p.name
    )
    def test_services_have_a_start_command(self, unit):
        parser = configparser.ConfigParser(strict=False)
        parser.read(unit, encoding="utf-8")
        assert parser.get("Service", "ExecStart", fallback="")

    @pytest.mark.parametrize(
        "unit", [u for u in UNIT_FILES if u.suffix == ".service"], ids=lambda p: p.name
    )
    def test_the_start_command_exists_after_substitution(self, unit):
        parser = configparser.ConfigParser(strict=False)
        parser.read(unit, encoding="utf-8")
        exec_start = parser.get("Service", "ExecStart", fallback="")
        target = exec_start.split()[0].replace("__ROOM_DIR__", str(ROOT))
        if "/.venv/" in target:
            return  # created by the installer, not present in the repository
        assert Path(target).exists(), f"{unit.name} starts {target}, which is missing"

    @staticmethod
    def _is_oneshot(unit) -> bool:
        parser = configparser.ConfigParser(strict=False)
        parser.read(unit, encoding="utf-8")
        return parser.get("Service", "Type", fallback="") == "oneshot"

    @pytest.mark.parametrize(
        "unit", [u for u in UNIT_FILES if u.suffix == ".service"], ids=lambda p: p.name
    )
    def test_long_running_services_restart_forever(self, unit):
        """systemd's default start limit would give up and leave the room dark.

        One-shot units (the watchdog check, the boot-time update) are the
        exception: they are *meant* to run once and exit.
        """
        if self._is_oneshot(unit):
            pytest.skip("one-shot units run to completion rather than staying up")
        parser = configparser.ConfigParser(strict=False)
        parser.read(unit, encoding="utf-8")
        assert parser.get("Service", "Restart", fallback="") == "always", unit.name
        assert parser.get("Unit", "StartLimitIntervalSec", fallback="") == "0", (
            f"{unit.name} must set StartLimitIntervalSec=0 so it never stops retrying"
        )

    @pytest.mark.parametrize("unit", UNIT_FILES, ids=lambda p: p.name)
    def test_placeholders_are_the_only_absolute_paths(self, unit):
        """Hard-coded paths would break any install outside one directory."""
        text = unit.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith(("ExecStart", "WorkingDirectory", "EnvironmentFile")):
                assert "__ROOM_DIR__" in line, f"{unit.name}: {line}"

    def test_the_watchdog_timer_names_its_service(self):
        parser = configparser.ConfigParser(strict=False)
        parser.read(ROOT / "systemd" / "room-watchdog.timer", encoding="utf-8")
        assert parser.get("Timer", "Unit", fallback="") == "room-watchdog.service"

    def test_every_managed_unit_is_shipped(self):
        from app.system_service import MANAGED_UNITS

        shipped = {u.name for u in UNIT_FILES}
        assert set(MANAGED_UNITS) <= shipped, set(MANAGED_UNITS) - shipped


class TestJavaScript:
    def test_js_files_were_found(self):
        assert len(JS_FILES) >= 4, [p.name for p in JS_FILES]

    @pytest.mark.parametrize("script", JS_FILES, ids=lambda p: p.name)
    def test_syntax_is_valid(self, script):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            pytest.skip("Node is not installed (it is not needed on the appliance)")
        result = subprocess.run(
            [node, "--check", str(script)], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, f"{script.name}:\n{result.stderr}"

    @pytest.mark.parametrize("script", JS_FILES, ids=lambda p: p.name)
    def test_no_stray_debugging_left_behind(self, script):
        text = script.read_text(encoding="utf-8")
        assert "debugger" not in text, f"{script.name} contains a debugger statement"
        assert "console.log(" not in text, f"{script.name} logs to the console"


class TestFrontendRegressions:
    """Guards for two bugs that only showed up in a real browser."""

    def test_hidden_beats_author_display_rules(self):
        """`hidden` must win, or a toggled overlay stays on screen.

        `.overlay { display: flex }` is an author rule and outranks the
        browser's built-in `[hidden] { display: none }`, so setting the
        attribute had no visible effect: the full-screen setup overlay sat
        permanently on top of the dashboard.
        """
        for name in ("styles.css", "admin.css"):
            css = (ROOT / "app" / "static" / name).read_text(encoding="utf-8")
            assert "[hidden] { display: none !important; }" in css, (
                f"{name} must force `hidden` to override author display rules"
            )

    def test_elements_toggled_with_hidden_are_covered(self):
        """Every id the JavaScript hides must exist in the templates."""
        import re

        templates = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "app" / "templates").glob("*.html")
        )
        scripts = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "app" / "static").glob("*.js")
        )
        for match in re.finditer(r'show\(\$\("([a-z0-9-]+)"\)', scripts):
            element_id = match.group(1)
            assert f'id="{element_id}"' in templates, (
                f"app.js hides #{element_id}, which no template defines"
            )

    def test_the_settings_form_skips_native_validation(self):
        """Otherwise the browser blocks the submit and the real errors never show.

        With native constraint checking on, a number outside its `min`/`max`
        stopped the form silently: the backend's inline, per-field messages
        (and every cross-field rule) were unreachable.
        """
        html = (ROOT / "app" / "templates" / "settings.html").read_text(encoding="utf-8")
        assert "novalidate" in html, (
            "settings.html must set novalidate so server-side messages are shown"
        )


class TestPython:
    @pytest.mark.parametrize("module", PY_FILES, ids=lambda p: p.name)
    def test_modules_compile(self, module):
        source = module.read_text(encoding="utf-8")
        try:
            compile(source, str(module), "exec")
        except SyntaxError as exc:
            pytest.fail(f"{module.name}:{exc.lineno}: {exc.msg}")

    @pytest.mark.parametrize("module", PY_FILES, ids=lambda p: p.name)
    def test_modules_are_documented(self, module):
        """Every module says what it is for; this is a maintenance appliance."""
        import ast

        tree = ast.parse(module.read_text(encoding="utf-8"))
        assert ast.get_docstring(tree), f"{module.name} has no module docstring"


class TestGeneratedDocs:
    def test_the_example_config_and_reference_are_current(self):
        result = subprocess.run(
            ["python3", str(ROOT / "scripts" / "gen-config-docs.py"), "--check"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(ROOT),
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_the_example_config_is_loadable(self):
        import yaml

        from app.config_schema import FIELDS

        raw = yaml.safe_load((ROOT / "config" / "config.example.yaml").read_text())
        assert set(raw) == {f.key for f in FIELDS}

    def test_the_readme_documents_the_required_topics(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
        for topic in (
            "install", "settings", "troubleshoot", "architecture",
            "airplay", "teams", "google meet", "poly", "calendar",
            "control panel", "sign in",
        ):
            assert topic in readme, f"README does not cover: {topic}"

    def test_every_setting_is_reachable_from_the_reference(self):
        from app.config_schema import FIELDS

        reference = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
        for field in FIELDS:
            assert f"`{field.key}`" in reference, f"{field.key} is undocumented"


class TestStyles:
    """Guards for the CSS mistakes that fail silently in a browser."""

    def test_every_custom_property_used_is_defined(self):
        """A `var(--typo)` does not error — it just quietly does nothing.

        A colour falls back to whatever was inherited and a border vanishes
        altogether, so the page looks *nearly* right and nobody notices for
        months. Cheaper to catch here.
        """
        import re

        sheets = sorted((ROOT / "app" / "static").glob("*.css"))
        assert sheets, "no stylesheets were found"

        defined = set()
        for sheet in sheets:
            defined |= set(
                re.findall(r"^\s*(--[a-z0-9-]+)\s*:", sheet.read_text(encoding="utf-8"), re.M)
            )

        for sheet in sheets:
            used = set(re.findall(r"var\((--[a-z0-9-]+)", sheet.read_text(encoding="utf-8")))
            undefined = sorted(used - defined)
            assert not undefined, f"{sheet.name} uses undefined properties: {undefined}"


class TestRequirements:
    """Guards for the pins. A version that does not exist is a broken install."""

    REQUIREMENTS = ("requirements.txt", "requirements-optional.txt",
                    "requirements-minutes.txt", "requirements-dev.txt")

    def _pins(self, name):
        import re

        text = (ROOT / name).read_text(encoding="utf-8")
        return re.findall(r"^([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.\-]+)\s*$", text, re.M)

    @pytest.mark.parametrize("name", REQUIREMENTS)
    def test_every_dependency_is_pinned_exactly(self, name):
        """A range would let an appliance installed next year get something else."""
        import re

        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-r"):
                continue
            assert re.match(r"^[A-Za-z0-9_.\-]+==[A-Za-z0-9_.\-]+$", line), (
                f"{name}: “{line}” is not an exact pin"
            )

    def test_nothing_is_pinned_twice_across_the_files(self):
        """Two files disagreeing about a version is a coin toss at install time."""
        seen = {}
        for name in self.REQUIREMENTS:
            for package, version in self._pins(name):
                key = package.lower()
                if key in seen:
                    other_name, other_version = seen[key]
                    assert other_version == version, (
                        f"{package} is {version} in {name} but "
                        f"{other_version} in {other_name}"
                    )
                seen[key] = (name, version)

    @pytest.mark.parametrize("name", REQUIREMENTS)
    def test_the_pinned_versions_exist_on_pypi(self, name):
        """The bug this caught: a plausible-looking version nobody had published.

        Network-dependent, so it skips rather than failing when there is none —
        an appliance is built on a bench, not always with the internet.
        """
        import json
        import urllib.error
        import urllib.request

        for package, version in self._pins(name):
            url = f"https://pypi.org/pypi/{package}/json"
            try:
                with urllib.request.urlopen(url, timeout=20) as response:
                    payload = json.load(response)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                pytest.skip("PyPI is not reachable from here")
            assert payload["releases"].get(version), (
                f"{name} pins {package}=={version}, which is not published. "
                f"The latest is {payload['info']['version']}."
            )
