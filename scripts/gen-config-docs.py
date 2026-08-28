#!/usr/bin/env python3
"""Regenerate the documented example config and the README options table.

Both are derived from ``app/config_schema.py``, so they can never drift out of
date. Run it after adding or changing a setting:

    .venv/bin/python scripts/gen-config-docs.py

    --check     exit non-zero if the files are out of date (used by tests/CI)
    --quiet     write without printing anything
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import render_yaml  # noqa: E402
from app.config_schema import GROUPS, FIELDS, defaults  # noqa: E402

EXAMPLE_PATH = ROOT / "config" / "config.example.yaml"
OPTIONS_PATH = ROOT / "docs" / "configuration.md"


def build_example() -> str:
    header = [
        "# Example configuration for the meeting-room appliance.",
        "#",
        "# GENERATED FILE — do not edit. Run scripts/gen-config-docs.py to refresh.",
        "#",
        "# You do not normally need this file: scripts/install.sh writes a real",
        "# config/config.yaml for you, and everything in it can be changed from the",
        "# Settings page in a browser. It is here so you can see every option and",
        "# its default in one place.",
        "#",
        "# To use it as a starting point:",
        "#     cp config/config.example.yaml config/config.yaml",
        "#     chmod 600 config/config.yaml",
        "",
    ]
    body = render_yaml(defaults(), comment_header=False)
    return "\n".join(header) + body


def _escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _render_default(value: object) -> str:
    if isinstance(value, bool):
        return "`true`" if value else "`false`"
    if isinstance(value, list):
        if not value:
            return "empty"
        shown = ", ".join(str(item) for item in value[:3])
        more = f" … (+{len(value) - 3})" if len(value) > 3 else ""
        return f"`{_escape(shown)}{more}`"
    if value == "":
        return "empty"
    return f"`{_escape(str(value))}`"


def build_options_doc() -> str:
    lines = [
        "# Configuration reference",
        "",
        "<!-- GENERATED FILE — run scripts/gen-config-docs.py to refresh. -->",
        "",
        "Every option can be changed from **Settings** in a browser, from",
        "`./scripts/roomctl set KEY VALUE`, or by editing `config/config.yaml` and",
        "restarting (`./scripts/roomctl restart backend`).",
        "",
        "An environment variable of the same name — in `.env` or the real",
        "environment — overrides both, and the Settings page shows such an option as",
        "read-only so the two can never disagree.",
        "",
        f"There are {len(FIELDS)} options. All have working defaults; a fresh install",
        "needs only a calendar link.",
        "",
    ]

    group_titles = dict(GROUPS)
    for group_id, title in GROUPS:
        members = [f for f in FIELDS if f.group == group_id]
        if not members:
            continue
        lines += [f"## {title}", ""]
        lines += [
            "| Option | Default | What it does |",
            "| --- | --- | --- |",
        ]
        for field in members:
            notes = _escape(field.help)
            if field.choices:
                notes = (notes + " " if notes else "") + f"One of: {', '.join(field.choices)}."
            if field.minimum is not None or field.maximum is not None:
                bounds = []
                if field.minimum is not None:
                    bounds.append(f"min {field.minimum:g}")
                if field.maximum is not None:
                    bounds.append(f"max {field.maximum:g}")
                notes = (notes + " " if notes else "") + f"({', '.join(bounds)})"
            if field.advanced:
                notes = (notes + " " if notes else "") + "_Advanced._"
            if field.secret:
                notes = (notes + " " if notes else "") + "**Secret — never logged.**"
            if field.restart_units:
                units = ", ".join(u.replace(".service", "").replace(".timer", "")
                                  for u in field.restart_units)
                notes = (notes + " " if notes else "") + f"Restarts: {units}."
            lines.append(
                f"| `{field.key}` | {_render_default(field.default)} | {field.label}. {notes} |"
            )
        lines.append("")

    lines += [
        "## Where the values come from",
        "",
        "Lowest priority first — a later layer wins:",
        "",
        "1. **Built-in defaults** (`app/config_schema.py`) — a complete, working set.",
        "2. **`config/config.yaml`** — written by the installer and the Settings page.",
        "3. **`.env` / environment variables** — for secrets and development.",
        "",
        "A value that fails validation is replaced by its default and reported as a",
        "warning on the dashboard, rather than stopping the appliance from starting.",
        "If `config.yaml` itself is unreadable, the previous version",
        "(`config.yaml.bak`) is used; if that fails too, the defaults are. The room",
        "always comes up.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="only verify freshness")
    parser.add_argument("--quiet", action="store_true", help="write silently")
    args = parser.parse_args()

    wanted = {EXAMPLE_PATH: build_example(), OPTIONS_PATH: build_options_doc()}

    if args.check:
        stale = [
            path.relative_to(ROOT)
            for path, content in wanted.items()
            if not path.exists() or path.read_text(encoding="utf-8") != content
        ]
        if stale:
            print("Out of date: " + ", ".join(str(path) for path in stale))
            print("Run: .venv/bin/python scripts/gen-config-docs.py")
            return 1
        if not args.quiet:
            print("Generated configuration docs are up to date.")
        return 0

    for path, content in wanted.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if not args.quiet:
            print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
