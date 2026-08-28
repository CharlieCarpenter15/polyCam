"""Where the meeting-minutes feature keeps its files.

Everything lives under one directory — ``var/minutes`` — so an engineer can
find a meeting's audio, transcript and summary side by side, and so switching
the feature off and deleting one tree removes every trace of it.

A recording holds people's voices and a transcript holds what they said, so
every file written here is owner-readable only and the directories are created
with the same restriction. Nothing in this tree is served as a static file:
the web layer looks a session up by id and streams it, which is why a crafted
name cannot reach anything it should not.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import paths as base

#: Every directory below is derived from ``app.paths.VAR_DIR``, and is worked
#: out **when it is asked for** rather than when this module is imported.
#:
#: That is not fussiness. ``ROOM_APPLIANCE_VAR`` moves the whole writable tree,
#: and the test suite uses it to give every test its own throw-away directory —
#: reloading ``app.paths`` to pick the new value up. A constant computed at
#: import time would have been frozen to wherever the appliance happened to be
#: pointing the first time anything imported this, so a test would quietly write
#: its recordings into a real installation's ``var`` and read back the previous
#: test's meetings. That is exactly what happened before this was made lazy.
_DERIVED = {
    "MINUTES_DIR": ("minutes",),
    "SESSIONS_DIR": ("minutes", "sessions"),
    "PEOPLE_DIR": ("minutes", "people"),
    "PEOPLE_FILE": ("minutes", "people", "people.json"),
    "PHOTOS_DIR": ("minutes", "people", "photos"),
    "MODELS_DIR": ("minutes", "models"),
}


def __getattr__(name: str) -> Any:
    """Resolve ``MINUTES_DIR`` and friends against the current ``VAR_DIR``."""
    if name not in _DERIVED:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return _at(name)


def __dir__() -> list[str]:
    return sorted(list(globals()) + list(_DERIVED))


def _at(name: str) -> Path:
    """One of the directories above, derived from the current ``VAR_DIR``.

    Always derived, never read back off this module. That rules out the class
    of bug where two callers disagree about where the tree is: there is exactly
    one answer and it comes from one place.

    It also means ``monkeypatch.setattr`` on one of these names does not move
    anything, and should not be used. The supported way to point the writable
    tree somewhere else is the ``ROOM_APPLIANCE_VAR`` environment variable and
    a reload of ``app.paths`` — which is what the test suite's ``room_dirs``
    fixture already does for the whole appliance. Patching an attribute here
    would also outlive the test that did it: the name is resolved lazily, so
    the patch creates a real module global where there was none, and neither
    ``monkeypatch`` undoing it nor ``importlib.reload`` removes it again.
    """
    root = base.VAR_DIR
    for part in _DERIVED[name]:
        root = root / part
    return root

#: Session ids are generated, never taken from a request, but the resolver
#: checks anyway: one regex is cheaper than trusting every caller forever.
SESSION_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$")

#: Person ids are generated the same way and checked the same way.
PERSON_ID_RE = re.compile(r"^[0-9a-f]{12}$")

_DIR_MODE = 0o700


def ensure_dirs() -> None:
    """Create the directory tree if it is missing (safe to repeat).

    A read-only or full filesystem must not stop the appliance from booting,
    so a failure here is swallowed; the callers that actually need to write
    report the error at the point where it matters.
    """
    for path in (
        _at("MINUTES_DIR"),
        _at("SESSIONS_DIR"),
        _at("PEOPLE_DIR"),
        _at("PHOTOS_DIR"),
        _at("MODELS_DIR"),
    ):
        try:
            path.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        except OSError:
            pass


def session_dir(session_id: str) -> Path | None:
    """The directory for ``session_id``, or None if the id is not well formed."""
    if not SESSION_ID_RE.match(session_id or ""):
        return None
    return _at("SESSIONS_DIR") / session_id


def list_session_ids() -> list[str]:
    """Every recorded session on disk, newest first.

    The id starts with a sortable timestamp, so sorting the names sorts by
    time without opening a single file.
    """
    try:
        names = [p.name for p in _at("SESSIONS_DIR").iterdir() if p.is_dir()]
    except OSError:
        return []
    return sorted((n for n in names if SESSION_ID_RE.match(n)), reverse=True)


def photo_path(person_id: str, index: int, suffix: str = ".jpg") -> Path | None:
    """Where person ``person_id``'s reference photo number ``index`` goes.

    The suffix follows what the bytes actually are rather than what the upload
    claimed, so a PNG is never filed under a name saying it is a JPEG. Reading
    one back goes through :func:`find_photo`, which does not need to know.
    """
    if not PERSON_ID_RE.match(person_id or "") or not 0 <= index < 100:
        return None
    if suffix not in (".jpg", ".png"):
        return None
    return _at("PHOTOS_DIR") / f"{person_id}-{index:02d}{suffix}"


def find_photo(person_id: str, index: int) -> Path | None:
    """The photo stored at ``index``, whatever format it turned out to be."""
    if not PERSON_ID_RE.match(person_id or "") or not 0 <= index < 100:
        return None
    for suffix in (".jpg", ".png"):
        candidate = _at("PHOTOS_DIR") / f"{person_id}-{index:02d}{suffix}"
        if candidate.is_file():
            return candidate
    return None
