#!/usr/bin/env python3
"""Write the current injected JavaScript to disk for the Node DOM tests.

Every file written here is named ``clicker_*.js`` or ``incall.js`` so the
repository's .gitignore keeps the generated code out of git.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.join_flows import (
    build_click_script,
    build_in_call_script,
    build_mute_script,
    ordered_button_texts,
)

HERE = Path(__file__).resolve().parent
CONFIGURED = [
    "Continue on this browser",
    "Join now",
    "Join meeting",
    "Ask to join",
    "Dismiss",
]

#: The page the repeat guard in ``clicker_guarded.js`` was armed on. The Node
#: test uses the same URL to check that the guard holds, and a different one to
#: check that moving to a new page releases it. Keep the two in step.
GUARD_URL = "https://meet.google.com/guarded"

for provider, filename in (("teams", "clicker_teams.js"), ("meet", "clicker_meet.js")):
    (HERE / filename).write_text(
        build_click_script(
            ordered_button_texts(provider, CONFIGURED),
            display_name="Meeting Room",
            fill_name=True,
        )
    )

# The same Meet clicker, told that it pressed "Join now" on GUARD_URL a moment
# ago and must not press it again while the page has not moved on.
(HERE / "clicker_guarded.js").write_text(
    build_click_script(
        ordered_button_texts("meet", CONFIGURED),
        display_name="Meeting Room",
        fill_name=True,
        guarded_clicks=[("join now", GUARD_URL)],
    )
)

# The one-way mute pass used for JOIN_MUTE_ON_ENTRY.
(HERE / "clicker_mute.js").write_text(build_mute_script())

(HERE / "incall.js").write_text(build_in_call_script())
print(f"wrote clicker scripts to {HERE}")
