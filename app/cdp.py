"""A very small Chrome DevTools Protocol client.

Only what the appliance needs: find the kiosk tab, navigate it, run a snippet of
JavaScript and pre-grant camera/microphone permissions. Playwright would do the
same job but pulls in its own browser download and a much larger dependency
tree; on a Raspberry Pi that is a poor trade for four commands.

Everything here is defensive. Chromium may be starting, restarting or wedged, so
every method has a timeout and returns a value rather than raising, and the
websocket is reconnected transparently.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import requests

from .logging_setup import get_logger, log_event

log = get_logger("cdp")

DEFAULT_TIMEOUT = 5.0


class CDPError(RuntimeError):
    """A DevTools command could not be completed."""


class ChromeDevTools:
    """Talks to one Chromium instance over its debugging port on localhost."""

    def __init__(self, port: int = 9222, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = port
        self._lock = threading.RLock()
        self._ws: Any = None
        self._ws_url: str = ""
        self._next_id = 0

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
        self, expression: str, *, timeout: float = 10.0, user_gesture: bool = True
    ) -> Any:
        """Run JavaScript in the page and return the (JSON-able) result.

        ``user_gesture`` tells the page a person did this, which is what makes a
        meeting page accept a click on a control it gates behind one. It is the
        default because the join automation needs it. Pass ``False`` for a
        script that only reads: claiming a user gesture several times a minute
        keeps a page permanently convinced somebody is interacting with it.
        """
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                # Meeting pages gate some actions on a user gesture.
                "userGesture": bool(user_gesture),
                "timeout": int(timeout * 1000),
            },
            timeout=timeout + 2,
        )
        details = result.get("exceptionDetails")
        if details:
            message = (
                (details.get("exception") or {}).get("description")
                or details.get("text")
                or "JavaScript error"
            )
            raise CDPError(str(message).splitlines()[0][:200])
        return (result.get("result") or {}).get("value")

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
