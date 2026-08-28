#!/usr/bin/env python3
"""Write the current injected JavaScript to disk for the Node DOM tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.join_flows import build_click_script, build_in_call_script, ordered_button_texts

HERE = Path(__file__).resolve().parent
CONFIGURED = [
    "Continue on this browser",
    "Join now",
    "Join meeting",
    "Ask to join",
    "Dismiss",
]

for provider, filename in (("teams", "clicker_teams.js"), ("meet", "clicker_meet.js")):
    (HERE / filename).write_text(
        build_click_script(
            ordered_button_texts(provider, CONFIGURED),
            display_name="Meeting Room",
            fill_name=True,
        )
    )
(HERE / "incall.js").write_text(build_in_call_script())
print(f"wrote clicker scripts to {HERE}")
