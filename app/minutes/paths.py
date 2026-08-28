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

from .. import paths as base

#: Root of everything this feature owns.
MINUTES_DIR = base.VAR_DIR / "minutes"

#: One directory per recorded meeting.
SESSIONS_DIR = MINUTES_DIR / "sessions"

#: Enrolled colleagues: the profile index and their reference photos.
PEOPLE_DIR = MINUTES_DIR / "people"
PEOPLE_FILE = PEOPLE_DIR / "people.json"
PHOTOS_DIR = PEOPLE_DIR / "photos"

#: Downloaded speech-to-text models (a whisper.cpp ``.bin``, a vosk directory).
MODELS_DIR = MINUTES_DIR / "models"

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
    for path in (MINUTES_DIR, SESSIONS_DIR, PEOPLE_DIR, PHOTOS_DIR, MODELS_DIR):
        try:
            path.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        except OSError:
            pass


def session_dir(session_id: str) -> Path | None:
    """The directory for ``session_id``, or None if the id is not well formed."""
    if not SESSION_ID_RE.match(session_id or ""):
        return None
    return SESSIONS_DIR / session_id


def list_session_ids() -> list[str]:
    """Every recorded session on disk, newest first.

    The id starts with a sortable timestamp, so sorting the names sorts by
    time without opening a single file.
    """
    try:
        names = [p.name for p in SESSIONS_DIR.iterdir() if p.is_dir()]
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
    return PHOTOS_DIR / f"{person_id}-{index:02d}{suffix}"


def find_photo(person_id: str, index: int) -> Path | None:
    """The photo stored at ``index``, whatever format it turned out to be."""
    if not PERSON_ID_RE.match(person_id or "") or not 0 <= index < 100:
        return None
    for suffix in (".jpg", ".png"):
        candidate = PHOTOS_DIR / f"{person_id}-{index:02d}{suffix}"
        if candidate.is_file():
            return candidate
    return None
