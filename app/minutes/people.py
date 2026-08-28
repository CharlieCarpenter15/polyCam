"""Profiles for the colleagues the appliance is allowed to recognise.

A profile is a name, optionally an email address, and two bags of vectors: one
of face embeddings, one of voice embeddings. Nothing here knows how either kind
of vector is produced — that is ``faces.py`` and ``voiceprint.py``'s business —
which is what lets the recognition model be swapped without a migration. Each
vector carries the name of the model that made it, and a comparison between
vectors from two different models is refused rather than attempted, because
such a comparison returns a plausible number and a meaningless answer.

Two decisions worth knowing about:

*Vectors are stored L2-normalised.* Cosine similarity is then a dot product,
which is fast enough in plain Python at this scale that recognising a face does
not drag numpy into the import path of a room screen.

*Automatic enrolment cannot quietly ruin a profile.* When the appliance adds a
vector on its own — a confirmed sighting, a labelled speaker turn — the vector
must already resemble the profile it is joining, or it is refused. Without that
rule one mislabelled speaker turn teaches "Charlie" someone else's voice, and
every later match gets worse with no visible cause. A person adding a sample by
hand is trusted and skips the check.

The file holds biometric data, so it is written owner-readable only and deleting
a person deletes their vectors and their photos in the same call.
"""

from __future__ import annotations

import logging
import math
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from ..logging_setup import get_logger, log_event
from ..store import read_json, write_json
from . import paths

log = get_logger("minutes.people")

#: The two kinds of vector a profile can hold.
KIND_FACE = "face"
KIND_VOICE = "voice"
KINDS = (KIND_FACE, KIND_VOICE)

#: Most vectors kept per person per kind. Beyond this the oldest is dropped:
#: more samples stop helping long before they stop costing, and a profile that
#: grows without limit slows every comparison in the room.
MAX_VECTORS = 12

#: Most profiles. An office, not a database.
MAX_PEOPLE = 200

#: A vector the appliance wants to add on its own must be at least this similar
#: to what the profile already holds. Deliberately permissive — it is a guard
#: against a wrong label, not a second recognition threshold.
AUTO_ENROL_FLOOR = 0.45


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(text: Any, limit: int = 120) -> str:
    return str(text or "").strip()[:limit]


def normalise(values: Sequence[float]) -> list[float]:
    """Scale to unit length. A zero vector is returned unchanged."""
    total = math.sqrt(sum(float(v) * float(v) for v in values))
    if total <= 0:
        return [float(v) for v in values]
    return [float(v) / total for v in values]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity of two already-normalised vectors, clamped to [-1, 1].

    Length-mismatched vectors score 0 rather than raising: a model change is a
    reason to re-enrol, not a reason for the room screen to fall over.
    """
    if len(left) != len(right) or not left:
        return 0.0
    total = sum(float(a) * float(b) for a, b in zip(left, right))
    return max(-1.0, min(1.0, total))


@dataclass
class Vector:
    """One embedding, and where it came from."""

    model: str
    values: list[float]
    added_at: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            # Six decimals is far below the noise floor of any of these models
            # and roughly halves the file.
            "values": [round(v, 6) for v in self.values],
            "added_at": self.added_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, row: Any) -> "Vector | None":
        if not isinstance(row, dict):
            return None
        raw = row.get("values")
        if not isinstance(raw, list) or not raw:
            return None
        try:
            values = [float(v) for v in raw]
        except (TypeError, ValueError):
            return None
        model = _clean(row.get("model"), 60)
        if not model:
            return None
        return cls(
            model=model,
            values=values,
            added_at=_clean(row.get("added_at"), 40),
            note=_clean(row.get("note"), 200),
        )


@dataclass
class Person:
    """One colleague the appliance may recognise."""

    id: str
    name: str
    email: str = ""
    created_at: str = ""
    updated_at: str = ""
    notes: str = ""
    face: list[Vector] = field(default_factory=list)
    voice: list[Vector] = field(default_factory=list)
    #: Reference photo slots in use, so the web page knows what to show.
    photos: list[int] = field(default_factory=list)

    def vectors(self, kind: str) -> list[Vector]:
        return self.face if kind == KIND_FACE else self.voice

    def knows_face(self) -> bool:
        return bool(self.face)

    def knows_voice(self) -> bool:
        return bool(self.voice)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "notes": self.notes,
            "face": [v.to_dict() for v in self.face],
            "voice": [v.to_dict() for v in self.voice],
            "photos": list(self.photos),
        }

    def to_public_dict(self) -> dict[str, Any]:
        """What the web page gets: no vectors.

        An embedding is biometric data and there is nothing a browser can do
        with one, so it never leaves the appliance.
        """
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "notes": self.notes,
            "faces": len(self.face),
            "voices": len(self.voice),
            "photos": list(self.photos),
        }

    @classmethod
    def from_dict(cls, row: Any) -> "Person | None":
        if not isinstance(row, dict):
            return None
        person_id = _clean(row.get("id"), 40)
        name = _clean(row.get("name"))
        if not person_id or not name:
            return None
        photos = row.get("photos")
        return cls(
            id=person_id,
            name=name,
            email=_clean(row.get("email"), 200),
            created_at=_clean(row.get("created_at"), 40),
            updated_at=_clean(row.get("updated_at"), 40),
            notes=_clean(row.get("notes"), 500),
            face=_vectors(row.get("face")),
            voice=_vectors(row.get("voice")),
            photos=[int(p) for p in photos if isinstance(p, int)]
            if isinstance(photos, list)
            else [],
        )


def _vectors(rows: Any) -> list[Vector]:
    if not isinstance(rows, list):
        return []
    out = [Vector.from_dict(row) for row in rows]
    return [v for v in out if v is not None]


@dataclass(frozen=True)
class Match:
    """The best profile for a vector, and how sure we are."""

    person: Person | None
    score: float = 0.0

    @property
    def ok(self) -> bool:
        return self.person is not None

    @property
    def name(self) -> str:
        return self.person.name if self.person else ""

    @property
    def person_id(self) -> str:
        return self.person.id if self.person else ""


class PeopleStore:
    """The people file, loaded once and written atomically on every change."""

    def __init__(self, path: Any = None) -> None:
        self._path = path or paths.PEOPLE_FILE
        self._lock = threading.RLock()
        self._people: dict[str, Person] = {}
        self._loaded = False

    # -- loading ---------------------------------------------------------
    def load(self) -> None:
        payload = read_json(self._path, default=None)
        people: dict[str, Person] = {}
        if isinstance(payload, dict) and isinstance(payload.get("people"), list):
            for row in payload["people"]:
                person = Person.from_dict(row)
                if person is not None:
                    people[person.id] = person
        with self._lock:
            self._people = people
            self._loaded = True

    def _ensure_loaded(self) -> None:
        with self._lock:
            loaded = self._loaded
        if not loaded:
            self.load()

    def _save(self) -> bool:
        with self._lock:
            payload = {
                "version": 1,
                "updated_at": _now(),
                "people": [p.to_dict() for p in self._people.values()],
            }
        paths.ensure_dirs()
        return write_json(self._path, payload, mode=0o600)

    # -- reading ---------------------------------------------------------
    def all(self) -> list[Person]:
        """Every profile, by name."""
        self._ensure_loaded()
        with self._lock:
            return sorted(self._people.values(), key=lambda p: p.name.lower())

    def get(self, person_id: str) -> Person | None:
        self._ensure_loaded()
        with self._lock:
            return self._people.get(_clean(person_id, 40))

    def by_name(self, name: str) -> Person | None:
        """Case-insensitive lookup, used when a transcript is labelled by hand."""
        wanted = _clean(name).lower()
        if not wanted:
            return None
        for person in self.all():
            if person.name.lower() == wanted:
                return person
        return None

    def count(self) -> int:
        self._ensure_loaded()
        with self._lock:
            return len(self._people)

    def stats(self) -> dict[str, int]:
        people = self.all()
        return {
            "people": len(people),
            "with_face": sum(1 for p in people if p.knows_face()),
            "with_voice": sum(1 for p in people if p.knows_voice()),
        }

    # -- writing ---------------------------------------------------------
    def add(self, name: str, email: str = "", notes: str = "") -> tuple[Person | None, str]:
        """Create a profile. Returns ``(person, error)``."""
        clean_name = _clean(name)
        if not clean_name:
            return None, "A name is required."
        self._ensure_loaded()
        if self.count() >= MAX_PEOPLE:
            return None, f"There are already {MAX_PEOPLE} profiles. Delete one first."
        if self.by_name(clean_name) is not None:
            return None, f"There is already a profile called “{clean_name}”."
        person = Person(
            id=secrets.token_hex(6),
            name=clean_name,
            email=_clean(email, 200),
            notes=_clean(notes, 500),
            created_at=_now(),
            updated_at=_now(),
        )
        with self._lock:
            self._people[person.id] = person
        self._save()
        log_event(log, logging.INFO, "minutes.person_added", person=person.id)
        return person, ""

    def update(self, person_id: str, **changes: Any) -> tuple[Person | None, str]:
        person = self.get(person_id)
        if person is None:
            return None, "No such person."
        if "name" in changes:
            new_name = _clean(changes["name"])
            if not new_name:
                return None, "A name is required."
            clash = self.by_name(new_name)
            if clash is not None and clash.id != person.id:
                return None, f"There is already a profile called “{new_name}”."
            person.name = new_name
        if "email" in changes:
            person.email = _clean(changes["email"], 200)
        if "notes" in changes:
            person.notes = _clean(changes["notes"], 500)
        person.updated_at = _now()
        self._save()
        return person, ""

    def delete(self, person_id: str) -> bool:
        """Remove a profile, its vectors and its photos."""
        person = self.get(person_id)
        if person is None:
            return False
        for index in list(person.photos):
            path = paths.find_photo(person.id, index)
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass
        with self._lock:
            self._people.pop(person.id, None)
        self._save()
        log_event(log, logging.INFO, "minutes.person_deleted", person=person_id)
        return True

    def add_vector(
        self,
        person_id: str,
        kind: str,
        model: str,
        values: Sequence[float],
        *,
        note: str = "",
        automatic: bool = False,
    ) -> tuple[bool, str]:
        """Teach a profile one more face or voice. Returns ``(added, error)``."""
        if kind not in KINDS:
            return False, f"Unknown kind “{kind}”."
        person = self.get(person_id)
        if person is None:
            return False, "No such person."
        if not values:
            return False, "The sample produced no data."
        vector = Vector(
            model=_clean(model, 60) or "unknown",
            values=normalise(values),
            added_at=_now(),
            note=_clean(note, 200),
        )
        bag = person.vectors(kind)
        if automatic and bag:
            best = max(
                (cosine(vector.values, existing.values)
                 for existing in bag
                 if existing.model == vector.model),
                default=0.0,
            )
            if best < AUTO_ENROL_FLOOR:
                return False, (
                    "That sample does not resemble the existing profile closely "
                    "enough to be added automatically. Add it by hand if it really "
                    "is the same person."
                )
        bag.append(vector)
        # Oldest out first: a profile should follow a person as they change.
        while len(bag) > MAX_VECTORS:
            bag.pop(0)
        person.updated_at = _now()
        self._save()
        log_event(
            log, logging.INFO, "minutes.vector_added",
            person=person_id, kind=kind, model=vector.model, automatic=automatic,
        )
        return True, ""

    def clear_vectors(self, person_id: str, kind: str) -> bool:
        person = self.get(person_id)
        if person is None or kind not in KINDS:
            return False
        if kind == KIND_FACE:
            person.face = []
        else:
            person.voice = []
        person.updated_at = _now()
        self._save()
        return True

    def record_photo(self, person_id: str, index: int) -> None:
        person = self.get(person_id)
        if person is None or index in person.photos:
            return
        person.photos.append(index)
        person.photos.sort()
        person.updated_at = _now()
        self._save()

    def next_photo_index(self, person_id: str) -> int:
        person = self.get(person_id)
        if person is None:
            return 0
        used = set(person.photos)
        for index in range(100):
            if index not in used:
                return index
        return 0

    # -- matching --------------------------------------------------------
    def match(
        self,
        kind: str,
        model: str,
        values: Sequence[float],
        *,
        threshold: float,
        candidates: Iterable[Person] | None = None,
    ) -> Match:
        """The best profile for ``values``, or an empty match below ``threshold``.

        ``candidates`` narrows the search — during a meeting the camera has
        already said who is in the room, and only comparing a voice against
        those few people is both faster and markedly more accurate than
        comparing it against everybody in the company.
        """
        if kind not in KINDS or not values:
            return Match(None, 0.0)
        probe = normalise(values)
        wanted_model = _clean(model, 60) or "unknown"
        people = list(candidates) if candidates is not None else self.all()
        best_person: Person | None = None
        best_score = 0.0
        for person in people:
            for vector in person.vectors(kind):
                if vector.model != wanted_model:
                    continue
                score = cosine(probe, vector.values)
                if score > best_score:
                    best_score = score
                    best_person = person
        if best_person is None or best_score < threshold:
            return Match(None, best_score)
        return Match(best_person, best_score)
