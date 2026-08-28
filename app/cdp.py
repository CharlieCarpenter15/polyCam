"""A very small Chrome DevTools Protocol client.

Only what the appliance needs: find the kiosk tab, navigate it, run a snippet of
JavaScript and pre-grant camera/microphone permissions. Playwright would do the
same job but pulls in its own browser download and a much larger dependency
tree; on a Raspberry Pi that is a poor trade for four commands.

Everything here is defensive. Chromium may be starting, restarting or wedged, so
every method has a timeout and returns a value rather than raising, and the
websocket is reconnected transparently.

The fifth thing it does is reach into a frame the page will not let JavaScript
reach by itself — see :meth:`ChromeDevTools.frames`. Nothing in the appliance
needed that while every meeting stage was same-origin with the page around it,
and the plain top-frame :meth:`ChromeDevTools.evaluate` is still what almost
every caller wants.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

from .logging_setup import get_logger, log_event

log = get_logger("cdp")

DEFAULT_TIMEOUT = 5.0

#: What the isolated worlds this client makes are called. It is visible in
#: DevTools to anybody who goes looking, so it says who left it there.
ISOLATED_WORLD_NAME = "room-appliance"

#: How many frames are worth walking. A meeting page has a handful — the stage,
#: perhaps a chat panel, a couple of empty utility frames. A page with more
#: than this is one we do not understand, and walking it during a live meeting
#: costs the Raspberry Pi more than it can return.
MAX_FRAMES = 12


class CDPError(RuntimeError):
    """A DevTools command could not be completed."""


@dataclass(frozen=True)
class PageFrame:
    """One frame of the page, flattened out of ``Page.getFrameTree``."""

    frame_id: str
    url: str = ""
    name: str = ""
    parent_id: str = ""

    @property
    def is_top(self) -> bool:
        """True for the page's own frame, which needs no isolated world."""
        return not self.parent_id


def looks_useful(value: Any) -> bool:
    """The default "did that answer anything?" test for a frame walk.

    Deliberately dull: ``None``, ``False``, an empty string or an empty
    collection are nothing, and so is the string ``"false"``, because the
    in-call probe answers in strings. Anything else is an answer. A caller who
    knows better — and for a reply with a shape, the caller always does —
    passes its own predicate instead.
    """
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "null", "undefined")
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _accepts(predicate: Callable[[Any], bool], value: Any) -> bool:
    """Put ``value`` to the caller's predicate; a predicate that throws is a no.

    The predicate belongs to whoever asked for the frame walk, and this runs in
    a meeting. A bug in it should cost the room a few names, not the join.
    """
    try:
        return bool(predicate(value))
    except Exception:
        return False


def _flatten_frames(node: Any, parent_id: str, out: list[PageFrame]) -> None:
    """Walk ``Page.getFrameTree``'s nesting into a flat list, parents first."""
    if not isinstance(node, dict) or len(out) >= MAX_FRAMES:
        return
    frame = node.get("frame")
    frame = frame if isinstance(frame, dict) else {}
    frame_id = str(frame.get("id") or "")
    if frame_id:
        out.append(
            PageFrame(
                frame_id=frame_id,
                url=str(frame.get("url") or ""),
                name=str(frame.get("name") or ""),
                # The top frame is the one with no parent, and that is the only
                # thing this field is asked.
                parent_id=parent_id or str(frame.get("parentId") or ""),
            )
        )
    children = node.get("childFrames")
    if isinstance(children, list):
        for child in children:
            _flatten_frames(child, frame_id, out)


class ChromeDevTools:
    """Talks to one Chromium instance over its debugging port on localhost."""

    def __init__(self, port: int = 9222, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = port
        self._lock = threading.RLock()
        self._ws: Any = None
        self._ws_url: str = ""
        self._next_id = 0
        #: The page URL the last command was actually sent to. The frame cache
        #: is filed under it, so that a page which navigates while its own
        #: frame tree is being read is not cached under the address it left.
        self._target_url: str = ""
        #: The page's frames and one isolated world per frame, both cached
        #: until that URL changes. ``None`` means "not looked yet"; an empty
        #: list means "asked, and this browser will not say".
        self._frames: list[PageFrame] | None = None
        self._frames_url: str = ""
        self._worlds: dict[str, int | None] = {}

    # -- HTTP endpoints --------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _http_get(self, path: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
        response = requests.get(f"{self.base_url}{path}", timeout=timeout)
        response.raise_for_status()
        return response.json()

    def version(self, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any] | None:
        """Browser version info, or ``None`` if Chromium is not answering."""
        try:
            data = self._http_get("/json/version", timeout=timeout)
            return data if isinstance(data, dict) else None
        except (requests.RequestException, ValueError):
            return None

    def is_alive(self, timeout: float = 2.0) -> bool:
        return self.version(timeout=timeout) is not None

    def targets(self, timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
        try:
            data = self._http_get("/json/list", timeout=timeout)
            return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []
        except (requests.RequestException, ValueError):
            return []

    def page_target(self) -> dict[str, Any] | None:
        """The kiosk page. Prefers a visible page over a background target."""
        pages = [t for t in self.targets() if t.get("type") == "page"]
        if not pages:
            return None
        # devtools:// and chrome-extension:// targets are not the room UI.
        real = [
            t
            for t in pages
            if not str(t.get("url", "")).startswith(("devtools://", "chrome-extension://"))
        ]
        return (real or pages)[0]

    def current_url(self) -> str:
        target = self.page_target()
        return str(target.get("url", "")) if target else ""

    # -- websocket session ----------------------------------------------
    def _connect(self, timeout: float) -> Any:
        """Return a live websocket to the page target, reconnecting if needed."""
        try:
            import websocket  # provided by websocket-client
        except ImportError as exc:  # pragma: no cover - dependency is required
            raise CDPError("websocket-client is not installed") from exc

        target = self.page_target()
        if not target:
            raise CDPError("Chromium has no page open")
        # Noted, not acted on: every command already pays for this lookup, so
        # the frame cache gets the page's current URL for nothing.
        self._target_url = str(target.get("url") or "")
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise CDPError("Chromium did not offer a debugging socket")

        if self._ws is not None and self._ws_url == ws_url:
            try:
                if self._ws.connected:
                    self._ws.settimeout(timeout)
                    return self._ws
            except Exception:
                pass
        self.close()

        try:
            self._ws = websocket.create_connection(
                ws_url, timeout=timeout, max_size=8 * 1024 * 1024
            )
        except Exception as exc:
            raise CDPError(f"Cannot open a debugging socket: {exc.__class__.__name__}") from exc
        self._ws_url = ws_url
        return self._ws

    def close(self) -> None:
        with self._lock:
            if self._ws is not None:
                try:
                    self._ws.close()
                except Exception:
                    pass
            self._ws = None
            self._ws_url = ""
            # Frame ids and execution contexts belong to the page this socket
            # was open on. Whatever we reconnect to, they are not worth keeping.
            self._forget_frames_locked()

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        """Run a DevTools command and return its result.

        Raises :class:`CDPError` on transport failure or a protocol error.
        """
        with self._lock:
            ws = self._connect(timeout)
            self._next_id += 1
            message_id = self._next_id
            payload = {"id": message_id, "method": method, "params": params or {}}
            try:
                ws.send(json.dumps(payload))
            except Exception as exc:
                self.close()
                raise CDPError(f"Send failed: {exc.__class__.__name__}") from exc

            # Skip protocol events until our reply arrives.
            for _ in range(200):
                try:
                    raw = ws.recv()
                except Exception as exc:
                    self.close()
                    raise CDPError(f"No reply: {exc.__class__.__name__}") from exc
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if message.get("id") != message_id:
                    continue
                if "error" in message:
                    error = message["error"] or {}
                    raise CDPError(str(error.get("message") or "DevTools error"))
                result = message.get("result")
                return result if isinstance(result, dict) else {}
            raise CDPError("Gave up waiting for a DevTools reply")

    # -- high-level helpers ---------------------------------------------
    def navigate(self, url: str, *, timeout: float = 10.0) -> bool:
        """Point the kiosk tab at ``url``."""
        try:
            self.send("Page.navigate", {"url": url}, timeout=timeout)
            return True
        except CDPError as exc:
            log_event(log, logging.WARNING, "cdp.navigate_failed", error=str(exc))
            return False

    def reload(self, *, ignore_cache: bool = False) -> bool:
        try:
            self.send("Page.reload", {"ignoreCache": ignore_cache})
            return True
        except CDPError:
            return False

    def evaluate(
        self,
        expression: str,
        *,
        timeout: float = 10.0,
        user_gesture: bool = True,
        context_id: int | None = None,
    ) -> Any:
        """Run JavaScript in the page and return the (JSON-able) result.

        ``user_gesture`` tells the page a person did this, which is what makes a
        meeting page accept a click on a control it gates behind one. It is the
        default because the join automation needs it. Pass ``False`` for a
        script that only reads: claiming a user gesture several times a minute
        keeps a page permanently convinced somebody is interacting with it.

        ``context_id`` runs the expression somewhere other than the page's own
        top frame — an execution context from :meth:`isolated_world`. Leaving
        it alone sends no ``contextId`` at all, which is what every caller
        before this one meant and still gets.
        """
        params: dict[str, Any] = {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            # Meeting pages gate some actions on a user gesture.
            "userGesture": bool(user_gesture),
            "timeout": int(timeout * 1000),
        }
        if context_id is not None:
            params["contextId"] = int(context_id)
        result = self.send("Runtime.evaluate", params, timeout=timeout + 2)
        details = result.get("exceptionDetails")
        if details:
            message = (
                (details.get("exception") or {}).get("description")
                or details.get("text")
                or "JavaScript error"
            )
            raise CDPError(str(message).splitlines()[0][:200])
        return (result.get("result") or {}).get("value")

    # -- frames ----------------------------------------------------------
    #
    # Everything above talks to the page's top-level frame, and until recently
    # that was enough: Teams draws its meeting stage inside an iframe, but the
    # iframe was same-origin, so a script walking the DOM could step into it
    # through ``contentDocument`` and find the roster and the buttons.
    #
    # A frame on a *different* origin is put in a different process, and then
    # ``contentDocument`` is null and no amount of JavaScript in the top frame
    # can reach it. Nothing would raise; the room would simply stop finding
    # anything. Microsoft's move of Teams to ``*.cloud.microsoft`` is the case
    # this is here for. The way in is to ask DevTools for the frame, make an
    # execution context inside it, and evaluate there instead.
    def forget_frames(self) -> None:
        """Forget the cached frame tree and the isolated worlds made in it."""
        with self._lock:
            self._forget_frames_locked()

    def _forget_frames_locked(self) -> None:
        """As :meth:`forget_frames`, for callers already holding the lock."""
        self._frames = None
        self._frames_url = ""
        self._worlds = {}

    def _forget_world(self, frame_id: str) -> None:
        """Drop one frame's cached world, so the next ask makes a fresh one."""
        with self._lock:
            self._worlds.pop(str(frame_id or ""), None)

    def frames(self, *, timeout: float = DEFAULT_TIMEOUT) -> list[PageFrame]:
        """Every frame in the page, the top frame first.

        Cached, and re-read only once the page has navigated. Enumerating costs
        two round trips and each isolated world costs another; the roster
        reader asks several times a minute for the length of a meeting, while
        the frames themselves change perhaps twice an hour, so doing this per
        poll would be paying over and over for the same answer.

        Navigation is noticed by watching the page's URL, rather than by
        subscribing to ``Page.frameNavigated``: :meth:`send` throws away every
        frame that is not the reply it is waiting for, so this client cannot
        hear an event even if it asked for one. Reading the URL costs one HTTP
        request to the debugging port, which is what every DevTools command
        already costs; handing back frame ids for a page that has gone would
        cost the room the meeting.

        Returns ``[]`` when the browser will not say — an older Chromium, a
        page that has gone. Callers then stay on the top frame, which is
        exactly what this client did before any of this existed.
        """
        url = self.current_url()
        with self._lock:
            cached = self._frames
            if cached is not None and self._frames_url == url:
                return list(cached)

        found = self._read_frame_tree(timeout=timeout)
        with self._lock:
            self._frames = found
            # Keyed on the URL the read itself saw rather than the one checked
            # a moment ago: ``_connect`` refreshes it on the way past, so a
            # page that moved on mid-read is caught by the next call instead of
            # being filed under an address it has already left.
            self._frames_url = self._target_url or url
            self._worlds = {}
        if len(found) > 1:
            log_event(log, logging.DEBUG, "cdp.frames_found", frames=len(found))
        return list(found)

    def _read_frame_tree(self, *, timeout: float) -> list[PageFrame]:
        """Ask DevTools for the frame tree. ``[]`` if it will not answer."""
        try:
            # The tree is only reliably populated once the domain is enabled,
            # and enabling a domain that is already enabled costs nothing.
            self.send("Page.enable", timeout=timeout)
            result = self.send("Page.getFrameTree", timeout=timeout)
        except CDPError as exc:
            log_event(log, logging.DEBUG, "cdp.frames_unavailable", error=str(exc))
            return []
        found: list[PageFrame] = []
        _flatten_frames(result.get("frameTree"), "", found)
        return found

    def isolated_world(
        self, frame_id: str, *, timeout: float = DEFAULT_TIMEOUT
    ) -> int | None:
        """An execution context inside ``frame_id``, or ``None``.

        An isolated world shares the frame's DOM but not its JavaScript. That
        is the only way into an out-of-process frame, and better manners than
        the page's own world in any case: nothing the appliance defines can
        collide with anything the meeting app defines.

        Cached per frame, refusals included. A frame that will not give us a
        world will not give us one on the next poll either, and asking again
        every two seconds is exactly the cost this cache exists to avoid.
        """
        frame_id = str(frame_id or "")
        if not frame_id:
            return None
        with self._lock:
            if frame_id in self._worlds:
                return self._worlds[frame_id]

        context_id: int | None = None
        try:
            result = self.send(
                "Page.createIsolatedWorld",
                {"frameId": frame_id, "worldName": ISOLATED_WORLD_NAME},
                timeout=timeout,
            )
            raw = result.get("executionContextId")
            context_id = int(raw) if isinstance(raw, (int, float)) else None
        except (CDPError, TypeError, ValueError) as exc:
            log_event(log, logging.DEBUG, "cdp.isolated_world_failed", error=str(exc))
            context_id = None
        with self._lock:
            self._worlds[frame_id] = context_id
        return context_id

    def evaluate_in_frames(
        self,
        expression: str,
        *,
        useful: Callable[[Any], bool] | None = None,
        timeout: float = 10.0,
        user_gesture: bool = True,
    ) -> Any:
        """Run ``expression`` in the top frame, then in child frames until one answers.

        ``useful`` decides what "answered" means, because only the caller can:
        for the join clicker a reply that names no candidates is nothing, for
        the roster reader a reply that names nobody is. It is asked about the
        top frame's answer first, and that answer comes back untouched when it
        passes — the ordinary case, at exactly the cost of :meth:`evaluate`.

        With no child frames, or a browser that will not enumerate them, this
        *is* :meth:`evaluate`, exception and all: the top frame's error is
        raised rather than swallowed, so a caller cannot tell the difference. A
        frame that fails on the way past is not an error but a frame that has
        gone, and the top frame's answer still stands.
        """
        wanted = useful if useful is not None else looks_useful
        top: Any = None
        top_error: CDPError | None = None
        try:
            top = self.evaluate(expression, timeout=timeout, user_gesture=user_gesture)
        except CDPError as exc:
            top_error = exc
        if top_error is None and _accepts(wanted, top):
            return top

        tried = 0
        for frame in self.frames():
            if frame.is_top:
                continue
            context_id = self.isolated_world(frame.frame_id)
            if context_id is None:
                continue
            tried += 1
            try:
                value = self.evaluate(
                    expression,
                    timeout=timeout,
                    user_gesture=user_gesture,
                    context_id=context_id,
                )
            except CDPError as exc:
                # The frame has gone, or its world went with a navigation this
                # client never saw. Drop the world so the next pass makes a
                # fresh one, and carry on: another frame may still answer.
                self._forget_world(frame.frame_id)
                log_event(
                    log, logging.DEBUG, "cdp.frame_evaluate_failed", error=str(exc)
                )
                continue
            if _accepts(wanted, value):
                log_event(log, logging.DEBUG, "cdp.frame_answered", frames=tried)
                return value

        if top_error is not None:
            raise top_error
        return top

    def grant_media_permissions(self, origin: str) -> bool:
        """Pre-grant camera and microphone for ``origin``.

        Meeting sites otherwise show a permission prompt that no one is in the
        room to click. The kiosk profile persists these grants, but doing it
        explicitly means a fresh profile works on the first meeting too.
        """
        try:
            self.send(
                "Browser.grantPermissions",
                {
                    "origin": origin,
                    "permissions": [
                        "audioCapture",
                        "videoCapture",
                        "clipboardReadWrite",
                        "notifications",
                    ],
                },
                timeout=4.0,
            )
            return True
        except CDPError as exc:
            # Older Chromium builds reject unknown permission names; retry with
            # only the two that matter.
            try:
                self.send(
                    "Browser.grantPermissions",
                    {"origin": origin, "permissions": ["audioCapture", "videoCapture"]},
                    timeout=4.0,
                )
                return True
            except CDPError:
                log_event(log, logging.DEBUG, "cdp.grant_permissions_failed", error=str(exc))
                return False

    def bring_to_front(self) -> bool:
        try:
            self.send("Page.bringToFront")
            return True
        except CDPError:
            return False
