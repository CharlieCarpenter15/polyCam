"""Screen sharing from a Windows PC (or anything with a browser).

AirPlay solves mirroring for Apple devices and nothing else. This is the other
half: a laptop opens a page, presses one button, picks a window, and it is on
the TV. No install, no dongle, no driver — which is the only way this is ever
going to be used by a visitor who has five minutes before their meeting starts.

**How it works.** Both ends are browsers. The sender is the laptop's browser
capturing its screen with ``getDisplayMedia``; the receiver is the Chromium
kiosk already showing the dashboard on the TV, which puts the incoming video in
a full-screen element. They talk directly over WebRTC on the room's network,
and this service is only the letterbox they use to find each other: the sender
leaves an offer, the receiver leaves an answer, both leave ICE candidates.

**No media passes through Python.** The Pi neither decodes nor re-encodes
anything, which is what makes this affordable on a Raspberry Pi — Chromium
decodes the stream with the same hardware path it uses for a video call.

Signalling is polled over plain HTTP rather than pushed over a websocket. It is
a handful of small messages during the two seconds it takes to connect, the
dashboard already polls, and there is no second protocol to debug at 3am.

Sessions are the same shape as the AirPlay ones on purpose: one at a time, a
heartbeat while sharing, and a hard expiry so the dashboard can never be stuck
showing a screen that stopped being sent.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from datetime import datetime, timezone

from .config import ConfigManager
from .logging_setup import get_logger, log_event
from .models import FAIL, OFF, OK, UNKNOWN, WARN

log = get_logger("cast")

#: A sender that stops asking for messages for this long has closed its laptop.
#: WebRTC itself notices a dead peer sooner; this is the backstop for the case
#: where the browser was killed before it could say goodbye.
SENDER_TIMEOUT_SECONDS = 25.0

#: How long a session may sit in "connecting" before it is written off. Long
#: enough for a slow laptop and the screen-picker dialogue, short enough that a
#: failed attempt does not block the next person for a whole meeting.
CONNECT_TIMEOUT_SECONDS = 90.0

#: A connected session with no heartbeat for this long is over.
SESSION_TIMEOUT_SECONDS = 60.0

#: Signalling messages waiting to be collected, per session. A cap so a peer
#: that never polls cannot grow this without limit; ICE for a LAN-only
#: connection is a dozen candidates at most.
MAX_QUEUED_MESSAGES = 64

#: Longest acceptable SDP / ICE payload. Real ones are a few kilobytes.
MAX_SIGNAL_BYTES = 64 * 1024

#: Session states.
CONNECTING = "connecting"
LIVE = "live"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed() -> float:
    """Monotonic seconds, as a function so the tests can move the clock.

    Timeouts here are measured monotonically on purpose: an NTP correction on a
    Raspberry Pi that has just found the network — which is exactly when a room
    boots — must not be read as "this laptop has been quiet for an hour".
    """
    return time.monotonic()


class _Session:
    """One attempt by one laptop to put its screen on the TV."""

    def __init__(self, *, client: str) -> None:
        self.id = secrets.token_urlsafe(12)
        self.client = client[:60]
        self.state = CONNECTING
        self.started = _now()
        self.connected_at: datetime | None = None
        self.offer: dict | None = None
        self.answer: dict | None = None
        #: Queues are one-directional: what the sender must read, and what the
        #: receiver must read. Each side drains its own.
        self.to_sender: list[dict] = []
        self.to_receiver: list[dict] = []
        self.sender_seen = _elapsed()
        self.receiver_seen = 0.0

    @property
    def live(self) -> bool:
        return self.state == LIVE

    def touch_sender(self) -> None:
        self.sender_seen = _elapsed()

    def touch_receiver(self) -> None:
        self.receiver_seen = _elapsed()

    def expiry_reason(self) -> str:
        """Why this session should be dropped, or "" to keep it."""
        idle = _elapsed() - self.sender_seen
        if idle > SENDER_TIMEOUT_SECONDS:
            return "the sharing device stopped responding"
        if self.state == CONNECTING:
            age = (_now() - self.started).total_seconds()
            if age > CONNECT_TIMEOUT_SECONDS:
                return "the connection was never established"
        elif self.receiver_seen and (
            _elapsed() - self.receiver_seen
        ) > SESSION_TIMEOUT_SECONDS:
            return "the room screen stopped collecting the stream"
        return ""

class CastService:
    """Tracks who is sharing from a browser, and relays their signalling."""

    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._session: _Session | None = None
        self._listeners: list = []
        self._sessions_total = 0
        self._last_error = ""
        # What the listeners in cast_web.py are doing. Reported in here so that
        # one status dict answers "can somebody share right now?" — the
        # dashboard, the health report and the diagnostics page all read this
        # and none of them should have to know there is a server object too.
        # ``None`` means nothing has reported yet, which is not a fault.
        self._listening: bool | None = None
        self._listener_error = ""
        # Why recent sessions ended, so the laptop that was sharing learns the
        # reason on its next poll instead of just losing the picture. "The TV
        # is in a meeting, share inside the meeting instead" is the single most
        # useful thing this feature ever says; dropping it would be a waste.
        self._ended: dict[str, str] = {}

    # -- listeners -------------------------------------------------------
    def on_change(self, callback) -> None:
        """Register ``callback(sharing: bool)``, called when sharing starts/stops."""
        with self._lock:
            self._listeners.append(callback)

    def _notify(self, sharing: bool) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(sharing)
            except Exception:  # pragma: no cover - a listener must not break sharing
                log.exception("cast.listener_failed")

    def _note_ended(self, session_id: str, reason: str) -> None:
        """Record an ending. Caller holds the lock."""
        self._ended[session_id] = reason
        if len(self._ended) > 8:
            # Only the most recent matter: a sender polls within seconds of
            # losing its session, or never comes back at all.
            for stale in list(self._ended)[:-8]:
                del self._ended[stale]

    def why_ended(self, session_id: str) -> str:
        """Why that session is gone, in words, or "". Read once."""
        with self._lock:
            return self._ended.pop(str(session_id or ""), "")

    # -- what the listeners are doing ------------------------------------
    def note_listeners(self, *, running: bool, error: str = "") -> None:
        """Told by :class:`~app.cast_web.CastServer` when it starts or stops."""
        with self._lock:
            self._listening = bool(running)
            self._listener_error = error or ""

    # -- the gate --------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.config.bool_("CAST_ENABLED")

    def check_pin(self, submitted: str) -> bool:
        """True when the room's sharing PIN is unset or matches."""
        import hmac

        expected = self.config.str_("CAST_PIN")
        if not expected:
            return True
        return hmac.compare_digest(str(submitted or "").strip(), expected)

    # -- sessions --------------------------------------------------------
    def _expire_locked(self) -> bool:
        """Drop the session if it has gone quiet. Returns True if sharing ended."""
        session = self._session
        if session is None:
            return False
        reason = session.expiry_reason()
        if not reason:
            return False
        was_live = session.live
        self._session = None
        self._note_ended(session.id, reason)
        log_event(
            log,
            logging.INFO if was_live else logging.WARNING,
            "cast.session_expired",
            reason=reason,
            client=session.client or "unknown",
        )
        if not was_live:
            self._last_error = reason
        return was_live

    def _sweep(self) -> None:
        """Expire outside the lock's notification, so listeners run unlocked."""
        with self._lock:
            ended = self._expire_locked()
        if ended:
            self._notify(False)

    @property
    def sharing(self) -> bool:
        """True while a browser's screen is actually on the TV."""
        self._sweep()
        with self._lock:
            return self._session is not None and self._session.live

    @property
    def pending(self) -> bool:
        """True while a laptop is trying to connect but is not on screen yet."""
        self._sweep()
        with self._lock:
            return self._session is not None and not self._session.live

    def start_session(self, *, client: str = "") -> dict[str, object]:
        """Claim the screen for a new sender.

        One room, one screen: a second person pressing Share takes over, the
        same way ``uxplay -nohold`` lets the next person take over AirPlay.
        Refusing would be worse — the previous session may be a closed laptop
        that has not timed out yet, and nobody in the room can tell.
        """
        self._sweep()
        displaced: _Session | None = None
        with self._lock:
            if self._session is not None:
                displaced = self._session
            session = _Session(client=client)
            self._session = session
            self._sessions_total += 1
            self._last_error = ""

        if displaced is not None:
            log_event(
                log, logging.INFO, "cast.session_replaced",
                previous=displaced.client or "unknown", was_live=displaced.live,
            )
            with self._lock:
                self._note_ended(displaced.id, "somebody else started sharing")

        log_event(log, logging.INFO, "cast.session_requested", client=session.client or "unknown")
        # A replaced *live* session means the TV is briefly showing a stream
        # nobody is feeding; the state machine only cares that sharing is on,
        # which it still is, so no notification is due here.
        return {"session": session.id, "state": session.state}

    def _require(self, session_id: str) -> _Session | None:
        session = self._session
        if session is None or session.id != str(session_id or ""):
            return None
        return session

    def stop_session(self, session_id: str, *, reason: str = "stopped by the sender") -> bool:
        """End a session at the sender's request (or the room's)."""
        with self._lock:
            session = self._require(session_id)
            if session is None:
                return False
            was_live = session.live
            self._session = None
            self._note_ended(session.id, reason)
        log_event(log, logging.INFO, "cast.session_stopped", reason=reason,
                  client=session.client or "unknown")
        if was_live:
            self._notify(False)
        return True

    def end_current(self, *, reason: str) -> bool:
        """Drop whatever is sharing now (a meeting is starting, or an admin said so)."""
        with self._lock:
            session = self._session
            if session is None:
                return False
            was_live = session.live
            self._session = None
            self._note_ended(session.id, reason)
        log_event(log, logging.INFO, "cast.session_interrupted", reason=reason)
        if was_live:
            self._notify(False)
        return True

    # -- signalling ------------------------------------------------------
    def submit_offer(self, session_id: str, offer: dict) -> bool:
        """The sender's SDP offer, for the receiver to collect."""
        with self._lock:
            session = self._require(session_id)
            if session is None:
                return False
            session.touch_sender()
            session.offer = offer
            session.to_receiver.append({"type": "offer", "sdp": offer})
            self._trim(session.to_receiver)
        return True

    def submit_answer(self, session_id: str, answer: dict) -> bool:
        """The receiver's SDP answer. Sharing counts as live from here."""
        started = False
        with self._lock:
            session = self._require(session_id)
            if session is None:
                return False
            session.touch_receiver()
            session.answer = answer
            session.to_sender.append({"type": "answer", "sdp": answer})
            self._trim(session.to_sender)
            if not session.live:
                session.state = LIVE
                session.connected_at = _now()
                started = True
        if started:
            log_event(log, logging.INFO, "cast.sharing_started",
                      client=session.client or "unknown")
            self._notify(True)
        return True

    def submit_candidate(self, session_id: str, candidate: dict, *, from_sender: bool) -> bool:
        """One ICE candidate, queued for the other side."""
        with self._lock:
            session = self._require(session_id)
            if session is None:
                return False
            if from_sender:
                session.touch_sender()
                queue = session.to_receiver
            else:
                session.touch_receiver()
                queue = session.to_sender
            queue.append({"type": "candidate", "candidate": candidate})
            self._trim(queue)
        return True

    def poll_sender(self, session_id: str) -> dict[str, object] | None:
        """Messages for the sender, and whether it still holds the screen."""
        self._sweep()
        with self._lock:
            session = self._require(session_id)
            if session is None:
                return None
            session.touch_sender()
            messages = session.to_sender
            session.to_sender = []
            return {
                "session": session.id,
                "state": session.state,
                "messages": messages,
                # The receiver has to be polling for anything to happen; saying
                # so lets the sender page explain a blank TV instead of just
                # spinning.
                "receiver_ready": bool(session.receiver_seen),
            }

    def poll_receiver(self) -> dict[str, object]:
        """Messages for the TV. Also how the receiver announces it is alive."""
        self._sweep()
        with self._lock:
            session = self._session
            if session is None:
                return {"session": "", "state": "", "messages": []}
            session.touch_receiver()
            messages = session.to_receiver
            session.to_receiver = []
            return {
                "session": session.id,
                "state": session.state,
                "client": session.client,
                "messages": messages,
            }

    def request_renegotiation(self, session_id: str) -> bool:
        """Ask the sender to offer again, for a room screen that has reloaded.

        The kiosk page can reload at any time and takes its half of the
        connection with it, while the laptop still holds a live capture. Rather
        than making somebody walk back to their seat and press Share again, the
        new page asks for a fresh offer — the laptop already has the screen, so
        nothing is asked of the person sharing and no picker appears.
        """
        with self._lock:
            session = self._require(session_id)
            if session is None:
                return False
            if session.offer is None:
                # The sender has not offered yet, so there is nothing to redo —
                # and asking would make it tear down the connection it is in
                # the middle of building.
                return False
            session.touch_receiver()
            session.answer = None
            # One request is enough; a queue of them would have the sender
            # rebuilding its connection over and over.
            session.to_sender = [
                message for message in session.to_sender
                if message.get("type") != "renegotiate"
            ]
            session.to_sender.append({"type": "renegotiate"})
        log_event(log, logging.INFO, "cast.renegotiation_requested",
                  client=session.client or "unknown")
        return True

    def receiver_failed(self, session_id: str, *, reason: str = "") -> bool:
        """The TV could not play the stream; do not leave it looking sharing."""
        with self._lock:
            session = self._require(session_id)
            if session is None:
                return False
            was_live = session.live
            self._session = None
            self._last_error = reason or "the room screen could not play the stream"
            self._note_ended(session.id, self._last_error)
        log_event(log, logging.WARNING, "cast.receiver_failed", reason=reason or "unknown")
        if was_live:
            self._notify(False)
        return True

    @staticmethod
    def _trim(queue: list[dict]) -> None:
        if len(queue) > MAX_QUEUED_MESSAGES:
            del queue[: len(queue) - MAX_QUEUED_MESSAGES]

    # -- state -----------------------------------------------------------
    def status(self) -> dict[str, object]:
        """What the dashboard, the health report and the panel all read."""
        if not self.enabled:
            return {"enabled": False, "status": OFF, "sharing": False}

        self._sweep()
        with self._lock:
            session = self._session
            total = self._sessions_total
            error = self._last_error
            listening = self._listening
            listener_error = self._listener_error
            sharing = session is not None and session.live
            pending = session is not None and not session.live
            client = session.client if session else ""
            since = (
                (session.connected_at or session.started).isoformat() if session else None
            )

        secure = self.tls_ready()
        if listening is None:
            # Nothing has tried to listen yet — during the second between the
            # services starting and the listeners opening. Reporting a fault
            # here would flash a red light on the TV at every boot; not knowing
            # is exactly what "unknown" is for.
            status = UNKNOWN
        elif not listening or not secure:
            # Switched on but unable to work — the port would not open, or the
            # certificate has gone — is a fault, not "off". Saying so is the
            # difference between a room that reports the problem and a laptop
            # that mysteriously never appears on the TV.
            status = FAIL
        elif error and not sharing:
            status = WARN
        else:
            status = OK

        return {
            "enabled": True,
            "status": status,
            "sharing": sharing,
            "pending": pending,
            "client": client,
            "since": since if sharing else None,
            "sessions": total,
            "last_error": error,
            "secure": secure,
            # ``None`` until the listeners have had their say, so a service
            # built on its own does not look broken.
            "listening": listening,
            "listener_error": listener_error,
            "pin_required": bool(self.config.str_("CAST_PIN")),
        }

    # -- HTTPS -----------------------------------------------------------
    def tls_ready(self) -> bool:
        """True when the certificate the sender page needs exists.

        ``getDisplayMedia`` is only offered to a secure context, so the sender
        page has to be served over HTTPS even on a private network. The
        certificate is generated locally on first run.
        """
        from .tls import certificate_present

        return certificate_present()

