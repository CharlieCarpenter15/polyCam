#!/usr/bin/env python3
"""Write the current meeting-window reader JavaScript to disk for the DOM tests.

The companion to ``emit_scripts.py``, for ``app/minutes/roster.py`` instead of
``app/join_flows.py``. Every file written here is named ``clicker_roster_*.js``
so the repository's existing ``tests/js/clicker_*.js`` ignore rule keeps the
generated code out of git without needing a new one.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.minutes.roster import (
    build_captions_script,
    build_drain_script,
    build_install_script,
    build_probe_script,
)

HERE = Path(__file__).resolve().parent

#: The recording token the emitted observer and drain scripts are built with.
#: ``test_roster.js`` uses the same one to drain, and a different one to check
#: that a drain belonging to another recording is refused. Keep the two in step.
RUN_TOKEN = "test-run-0001"

#: The emitted observer ticks this fast so the Node test does not have to sit
#: through the appliance's real 250 ms cadence. The Python side chooses the
#: real one; this only proves the accumulating works.
TICK_MS = 20

# One read of the page, per provider. "generic" is what an unrecognised
# meeting link gets: it tries all three families and latches onto whichever
# one answers.
for provider, filename in (
    ("teams", "clicker_roster_teams.js"),
    ("meet", "clicker_roster_meet.js"),
    ("zoom", "clicker_roster_zoom.js"),
    ("", "clicker_roster_generic.js"),
):
    (HERE / filename).write_text(build_probe_script(provider), encoding="utf-8")

# The resident observer and the two drains that go with it.
(HERE / "clicker_roster_install.js").write_text(
    build_install_script("teams", RUN_TOKEN, captions=True, tick_ms=TICK_MS),
    encoding="utf-8",
)
(HERE / "clicker_roster_install_meet.js").write_text(
    build_install_script("meet", RUN_TOKEN, captions=True, tick_ms=TICK_MS),
    encoding="utf-8",
)
(HERE / "clicker_roster_install_quiet.js").write_text(
    build_install_script("teams", RUN_TOKEN, captions=False, tick_ms=TICK_MS),
    encoding="utf-8",
)
(HERE / "clicker_roster_drain.js").write_text(
    build_drain_script(RUN_TOKEN), encoding="utf-8"
)
(HERE / "clicker_roster_drain_flush.js").write_text(
    build_drain_script(RUN_TOKEN, flush=True), encoding="utf-8"
)
(HERE / "clicker_roster_drain_other.js").write_text(
    build_drain_script("someone-else-s-recording"), encoding="utf-8"
)

# The one pass that may switch live captions on, and may never switch them off.
(HERE / "clicker_roster_captions.js").write_text(
    build_captions_script(), encoding="utf-8"
)

print(f"wrote roster scripts to {HERE}")
