"""Recording meetings, working out who spoke, and writing them up.

A self-contained feature. Nothing outside this package changes behaviour when
it is switched off, and switching it off is the default. The appliance's job —
showing the room's day and joining its meetings — does not depend on a single
line in here, and the design goes out of its way to keep it that way: the only
thread that touches the rest of the appliance *reads* the room's state and never
writes to it.

What it does, in the order it happens:

1. While a meeting is on, record two separate audio tracks — the room's own
   microphone and what the room's speaker is playing. Which track a voice
   arrives on is the one piece of speaker information that cannot be wrong, and
   everything else is built on top of it.
2. Read the meeting window itself for the far end. Teams, Meet and Zoom already
   know who is on the call and, when captions are on, have already written down
   what each of them said with their name against it.
3. Transcribe the room track with a local speech-to-text engine, and identify
   the people in it from enrolled faces and voices where that is switched on.
4. Ask Claude for a summary, and email it to the people who were there.

Steps 3 and 4 are where the honest limits are, and they are stated plainly in
the settings and in ``docs/meeting-minutes.md`` rather than buried: recognising
a face across a boardroom table is hard, recognising a voice on a far-field
microphone is harder, and a summary is only ever as good as the transcript
underneath it.

Importing this module pulls in nothing heavy and touches no hardware. The
optional pieces — an ML runtime, a speech engine, the Claude SDK — are imported
inside the functions that need them, so an appliance with none of them
installed still starts, still runs, and says exactly what is missing.
"""

from __future__ import annotations

from typing import Any

__all__ = ["MinutesService"]


def __getattr__(name: str) -> Any:
    """Resolve ``MinutesService`` only when somebody actually asks for it.

    Importing it here directly would mean that ``from app.minutes import mailer``
    — or any other single module — dragged in the recorder, the speech engines,
    the camera and the Claude client along with it. Deferring the import keeps
    each module independently importable, which is what makes them independently
    testable, and it keeps the appliance's start-up cost at zero for a feature
    that is switched off by default.
    """
    if name == "MinutesService":
        from .service import MinutesService

        return MinutesService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
