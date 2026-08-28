"""Best-effort browser automation for joining a meeting.

**Read this before changing anything.** Teams, Google Meet and Zoom are external
web applications. Their markup changes without notice, and no automation here can
be guaranteed to keep working. The design therefore assumes it *will* break:

* Matching is by **visible button text**, not CSS selectors or class names.
  Text like "Join now" survives redesigns far longer than ``.css-1x2y3z``.
* The list of accepted button texts lives in configuration
  (``JOIN_BUTTON_TEXTS``) and is editable from the Settings page, so an
  administrator can adapt to a rename without a code change or a redeploy.
* Per-provider extras live in :data:`PROVIDER_FLOWS` below — one small,
  self-contained entry per provider, so adding or fixing one is a local edit.
* Every automated step is optional. If all of it fails the room is left on the
  meeting's own pre-join screen, which is exactly where a human pressing the big
  JOIN button would have landed. Someone can then tap Join on the TV, on the
  phone control panel, or on the Poly remote.

The injected JavaScript searches the document, open shadow roots and same-origin
iframes, ignores hidden elements, and clicks at most one button per pass.

Three things the clicker deliberately does *not* do, each of which caused a real
room to misbehave:

* It never fills a name and clicks Join in the same pass. The Join button is
  disabled until the page has processed the name, so the click is either wasted
  on a disabled button or lands early and bounces the room back to the pre-join
  screen. Filling returns straight away; the next pass does the clicking.
* It never presses a button the caller has told it to leave alone
  (``guarded_clicks``), which is how the repeat guard stops the room pressing
  "Join now" over and over on a page that is simply slow.
* It never clicks while the page says the room is in the lobby. Waiting to be
  admitted is success in progress, not a failure to click harder.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# JavaScript payload
# ---------------------------------------------------------------------------

# Notes on the implementation below:
#  * collect() walks the DOM including open shadow roots and reachable iframes.
#  * visible() rejects zero-size, hidden and disabled elements.
#  * Buttons are ranked so a "pre-join" step (e.g. "Continue on this browser")
#    is taken before a "join" step, and destructive-looking text is never
#    matched because it is simply not in the list.
#  * Everything is ES5: this runs in whatever Chromium the Pi happens to have.
_CLICKER_JS = r"""
(function () {
  var WANTED = __PATTERNS__;
  var AVOID = __AVOID__;
  var GUARDED = __GUARDED__;
  var NAME = __NAME__;
  var FILL_NAME = __FILL_NAME__;

  function norm(s) {
    // Providers use typographic apostrophes ("you'll join when..."), so fold
    // them to ASCII before any comparison.
    return (s || "").replace(/[‘’]/g, "'")
                    .replace(/\s+/g, " ").trim().toLowerCase();
  }

  function visible(el) {
    if (!el || el.disabled) return false;
    if (el.getAttribute && el.getAttribute("aria-disabled") === "true") return false;
    var rect;
    try { rect = el.getBoundingClientRect(); } catch (e) { return false; }
    if (!rect || rect.width < 8 || rect.height < 8) return false;
    var style;
    try { style = window.getComputedStyle(el); } catch (e) { return false; }
    if (!style) return false;
    if (style.visibility === "hidden" || style.display === "none") return false;
    if (parseFloat(style.opacity || "1") < 0.15) return false;
    return true;
  }

  function label(el) {
    var text = norm(el.innerText || el.textContent);
    if (!text) {
      text = norm(el.getAttribute && (el.getAttribute("aria-label") ||
                  el.getAttribute("title") || el.value));
    }
    return text;
  }

  function dispatch(el, type) {
    try {
      el.dispatchEvent(new Event(type, { bubbles: true }));
    } catch (e) { /* ignore */ }
  }

  // Gather candidate elements from a root, following shadow DOM and iframes.
  function collect(root, out, depth) {
    if (!root || depth > 6 || out.length > 4000) return;
    var nodes;
    try {
      nodes = root.querySelectorAll(
        'button, [role="button"], a[href], input[type="submit"], input[type="button"], div[tabindex], span[role="button"]'
      );
    } catch (e) { return; }
    for (var i = 0; i < nodes.length && out.length <= 4000; i++) out.push(nodes[i]);

    var all;
    try { all = root.querySelectorAll("*"); } catch (e) { return; }
    for (var j = 0; j < all.length; j++) {
      var el = all[j];
      if (el.shadowRoot) collect(el.shadowRoot, out, depth + 1);
      if (el.tagName === "IFRAME") {
        try {
          if (el.contentDocument) collect(el.contentDocument, out, depth + 1);
        } catch (e) { /* cross-origin: nothing we can do, and that is fine */ }
      }
    }
  }

  // -- the guest name box ---------------------------------------------------
  //
  // Only "name"-ish fields, never one that already has something in it, and
  // never anything that smells like a meeting id, passcode or sign-in field:
  // typing the room name into a passcode box is how an appliance locks itself
  // out of a meeting.
  var NAME_BLOCKERS = ["meeting", "code", "passcode", "password", "email"];
  // "id" and "pin" are too short to match as substrings — "video", "hidden"
  // and "spinner" all contain one — so they are matched as whole words.
  var NAME_BLOCKER_WORDS = /(^|[^a-z])(id|pin)([^a-z]|$)/;

  function hintFor(el) {
    return norm(((el.getAttribute && el.getAttribute("aria-label")) || "") + " " +
                ((el.getAttribute && el.getAttribute("placeholder")) || "") + " " +
                ((el.getAttribute && el.getAttribute("title")) || "") + " " +
                (el.name || "") + " " + (el.id || ""));
  }

  function looksLikeNameField(hint) {
    if (!hint || hint.indexOf("name") === -1) return false;
    for (var i = 0; i < NAME_BLOCKERS.length; i++) {
      if (hint.indexOf(NAME_BLOCKERS[i]) !== -1) return false;
    }
    return !NAME_BLOCKER_WORDS.test(hint);
  }

  // Assigning to el.value is not enough on a React page (Teams, Meet): React
  // keeps its own copy of the value on the node, sees no change, and puts the
  // old empty value back — leaving Join greyed out and the room parked on the
  // pre-join screen, asking for a name every time. Writing through the
  // prototype's native setter is what makes the framework notice.
  function setInputValue(el, text) {
    var setter = null;
    try {
      var proto = el.tagName === "TEXTAREA"
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
      var descriptor = Object.getOwnPropertyDescriptor(proto, "value");
      if (descriptor && descriptor.set) setter = descriptor.set;
    } catch (e) { /* an engine without the descriptor: fall back below */ }
    try {
      if (setter) { setter.call(el, text); } else { el.value = text; }
    } catch (e) {
      try { el.value = text; } catch (e2) { return false; }
    }
    dispatch(el, "input");
    dispatch(el, "change");
    return el.value === text;
  }

  // Zoom and Webex use a contenteditable div for the name in places.
  function setEditableText(el, text) {
    try { el.textContent = text; } catch (e) { return false; }
    dispatch(el, "input");
    return norm(el.textContent) === norm(text);
  }

  // "" nothing to do, "filled" the box now really holds the name, "failed" a
  // name box was found but the page would not keep the value.
  function fillName(root, depth) {
    if (!FILL_NAME || !NAME) return "";
    var outcome = "";
    var fields;
    try {
      fields = root.querySelectorAll(
        'input[type="text"], input:not([type]), input[type="search"], textarea, [contenteditable="true"], [contenteditable=""]'
      );
    } catch (e) { fields = []; }
    for (var i = 0; i < fields.length; i++) {
      var el = fields[i];
      if (!visible(el)) continue;
      var editable = el.tagName !== "INPUT" && el.tagName !== "TEXTAREA";
      if (editable ? norm(el.textContent) : (el.value || "")) continue;
      if (!looksLikeNameField(hintFor(el))) continue;
      try { el.focus(); } catch (e) { /* not focusable: still worth writing */ }
      if (editable ? setEditableText(el, NAME) : setInputValue(el, NAME)) return "filled";
      outcome = "failed";
    }
    if (depth < 4) {
      var all;
      try { all = root.querySelectorAll("*"); } catch (e) { return outcome; }
      for (var k = 0; k < all.length; k++) {
        var host = all[k];
        var nested = "";
        if (host.shadowRoot) {
          nested = fillName(host.shadowRoot, depth + 1);
        } else if (host.tagName === "IFRAME") {
          try {
            if (host.contentDocument) nested = fillName(host.contentDocument, depth + 1);
          } catch (e) { nested = ""; }
        }
        if (nested === "filled") return "filled";
        if (nested === "failed") outcome = "failed";
      }
    }
    return outcome;
  }

  // -- the lobby ------------------------------------------------------------
  //
  // Once the room has asked to be let in, pressing anything else is noise: the
  // host has to act, and clicking "Ask to join" again can restart the wait.
  var LOBBY = [
    "asking to be let in",
    "waiting to be admitted",
    "waiting for the host",
    "wait for the host",
    "waiting for the meeting to start",
    "someone lets you in",
    "let you in soon",
    "lets you in soon",
    "in the waiting room"
  ];

  function pageText() {
    var body;
    try { body = document.body; } catch (e) { return ""; }
    if (!body) return "";
    var text = norm(body.innerText || body.textContent);
    return text.length > 20000 ? text.slice(0, 20000) : text;
  }

  function lobbyPhrase() {
    var text = pageText();
    if (!text) return "";
    for (var i = 0; i < LOBBY.length; i++) {
      if (text.indexOf(LOBBY[i]) !== -1) return LOBBY[i];
    }
    // "Please wait" alone is far too common to trust — a page still loading
    // says it too — and every pre-join screen contains the word "join", so the
    // context has to be someone letting the room in.
    if (text.indexOf("please wait") !== -1 &&
        (text.indexOf("host") !== -1 || text.indexOf("admit") !== -1 ||
         text.indexOf("let you in") !== -1 || text.indexOf("organiser") !== -1 ||
         text.indexOf("organizer") !== -1)) {
      return "please wait";
    }
    return "";
  }

  // -- what may be pressed --------------------------------------------------
  function avoided(text) {
    for (var a = 0; a < AVOID.length; a++) {
      var term = norm(AVOID[a]);
      if (term && text.indexOf(term) !== -1) return true;
    }
    return false;
  }

  function guarded(text) {
    for (var g = 0; g < GUARDED.length; g++) {
      var entry = GUARDED[g];
      if (!entry || norm(entry.text) !== text) continue;
      // A new page means a new button: the guard only holds while the room is
      // still looking at the URL it clicked on.
      if (!entry.url || entry.url === location.href) return true;
    }
    return false;
  }

  var filled = fillName(document, 0) === "filled";
  var waiting = lobbyPhrase();

  var candidates = [];
  collect(document, candidates, 0);

  // Filling the name and pressing Join in the same pass is a race the room
  // loses; so is pressing anything at all while waiting to be admitted.
  if (filled || waiting) {
    return JSON.stringify({ clicked: null, filled_name: filled, waiting: waiting,
                            candidates: candidates.length, url: location.href });
  }

  var best = null, bestRank = 1e9, bestText = "";
  for (var i = 0; i < candidates.length; i++) {
    var el = candidates[i];
    if (!visible(el)) continue;
    var text = label(el);
    if (!text || text.length > 80) continue;
    if (avoided(text) || guarded(text)) continue;
    for (var w = 0; w < WANTED.length; w++) {
      var want = norm(WANTED[w]);
      if (!want) continue;
      // Exact match beats "contains", and earlier entries in the configured
      // list beat later ones, so ordering in Settings is meaningful.
      var rank = text === want ? w : (text.indexOf(want) !== -1 ? 1000 + w : -1);
      if (rank >= 0 && rank < bestRank) {
        best = el; bestRank = rank; bestText = text;
      }
    }
  }

  if (!best) {
    return JSON.stringify({ clicked: null, filled_name: false, waiting: "",
                            candidates: candidates.length, url: location.href });
  }
  try {
    best.scrollIntoView({ block: "center" });
  } catch (e) { /* ignore */ }
  try {
    best.click();
  } catch (e) {
    return JSON.stringify({ clicked: null, filled_name: false, waiting: "",
                            candidates: candidates.length, error: String(e),
                            url: location.href });
  }
  return JSON.stringify({ clicked: bestText, filled_name: false, waiting: "",
                          candidates: candidates.length, url: location.href });
})()
"""

# A probe used to decide whether the room is already in the meeting. A
# leave/hang-up control is the most reliable cross-provider signal; the rest are
# things that only ever appear once the call is up, so automation stops promptly
# instead of clicking around inside a live meeting.
_IN_CALL_JS = r"""
(function () {
  function norm(s) {
    return (s || "").replace(/[‘’]/g, "'")
                    .replace(/\s+/g, " ").trim().toLowerCase();
  }
  var HINTS = ["leave call", "leave meeting", "leave (", "hang up", "end call",
               "end meeting", "leave now", "leave the call", "leave the meeting",
               "end the call", "end the meeting", "end meeting for all",
               "end call for all", "hang up call", "disconnect call",
               "you're the only one here", "no one else is here"];
  var nodes;
  try {
    nodes = document.querySelectorAll('button, [role="button"], [aria-label], [title]');
  } catch (e) { return "false"; }
  for (var i = 0; i < nodes.length && i < 4000; i++) {
    var el = nodes[i];
    var text = norm(el.innerText || el.textContent || "") + " " +
               norm(el.getAttribute && el.getAttribute("aria-label")) + " " +
               norm(el.getAttribute && el.getAttribute("title"));
    for (var h = 0; h < HINTS.length; h++) {
      if (text.indexOf(HINTS[h]) !== -1) return "true";
    }
  }
  return "false";
})()
"""

#: Controls that mute the microphone, most specific first.
MUTE_BUTTON_TEXTS: tuple[str, ...] = (
    "Mute microphone",
    "Turn off microphone",
    "Mute my microphone",
    "Mute mic",
    "Mute audio",
    "Mute",
)

#: ...and what must never be pressed while doing it. "Mute" is a substring of
#: "Unmute", so without this list a page that is already muted gets unmuted —
#: which is the opposite of joining quietly.
MUTE_AVOID_TEXTS: tuple[str, ...] = (
    "unmute",
    "turn on microphone",
    "turn on mic",
    "start audio",
)


@dataclass(frozen=True)
class ProviderFlow:
    """Provider-specific tweaks layered on top of the generic text clicker."""

    provider_id: str
    #: Extra button texts tried *before* the configured generic list.
    priority_texts: tuple[str, ...] = ()
    #: Appended to the meeting URL (query parameters) when opening it.
    url_extras: dict[str, str] = field(default_factory=dict)
    #: True when the provider shows a "your name" field for guests.
    asks_for_name: bool = False
    #: Seconds to wait after navigation before the first click attempt.
    settle_seconds: float = 6.0
    #: Notes shown on the Diagnostics page.
    notes: str = ""


#: One entry per provider. Edit these — not the JavaScript — to adapt a flow.
PROVIDER_FLOWS: dict[str, ProviderFlow] = {
    "teams": ProviderFlow(
        provider_id="teams",
        priority_texts=(
            # Teams first asks how to open the meeting, then shows the pre-join.
            "Continue on this browser",
            "Use the web app instead",
            "Continue in this browser",
            "Join now",
        ),
        asks_for_name=True,
        settle_seconds=8.0,
        notes=(
            "Teams shows an app-or-browser chooser first. If the room account is "
            "signed in, joining is one click; as a guest it asks for a name."
        ),
    ),
    "meet": ProviderFlow(
        provider_id="meet",
        priority_texts=("Join now", "Ask to join", "Switch here"),
        asks_for_name=True,
        settle_seconds=6.0,
        notes=(
            "Google Meet needs the room's Google account signed in, or the "
            "meeting host must admit the room after 'Ask to join'."
        ),
    ),
    "zoom": ProviderFlow(
        provider_id="zoom",
        priority_texts=(
            "Join from your browser",
            "Launch Meeting",
            "Join",
            "I Agree",
        ),
        # Ask Zoom for the web client rather than the desktop app, which does
        # not exist on Raspberry Pi OS.
        url_extras={"_x_zm_rtaid": "", "web": "1"},
        asks_for_name=True,
        settle_seconds=8.0,
        notes=(
            "Zoom in a browser is the least reliable of the three: it often "
            "insists on the desktop client. Expect to press Join by hand."
        ),
    ),
    "webex": ProviderFlow(
        provider_id="webex",
        priority_texts=("Join from your browser", "Join meeting", "Join"),
        asks_for_name=True,
        notes="Webex browser joining is supported but not extensively tested.",
    ),
}

GENERIC_FLOW = ProviderFlow(
    provider_id="other",
    priority_texts=("Join", "Join now", "Enter"),
    notes="Unknown provider: the page is opened and left for a person to finish.",
)


def flow_for(provider_id: str) -> ProviderFlow:
    return PROVIDER_FLOWS.get(provider_id or "", GENERIC_FLOW)


def build_click_script(
    button_texts: list[str],
    *,
    display_name: str = "",
    fill_name: bool = False,
    avoid_texts: Iterable[str] = (),
    guarded_clicks: Iterable[tuple[str, str]] = (),
) -> str:
    """Render the clicker with the given button texts.

    ``avoid_texts`` are never pressed, whatever they match — that is what turns
    a "Mute" match into a mute rather than a toggle. ``guarded_clicks`` are
    ``(button text, page URL)`` pairs the room pressed a moment ago: each is
    skipped while the page is still on that URL, so a slow meeting page is not
    pressed over and over.
    """
    patterns = [str(t).strip() for t in button_texts if str(t).strip()]
    avoid = [str(t).strip() for t in avoid_texts if str(t).strip()]
    guarded = [
        {"text": str(text), "url": str(url or "")}
        for text, url in guarded_clicks
        if str(text).strip()
    ]
    return (
        _CLICKER_JS.replace("__PATTERNS__", json.dumps(patterns))
        .replace("__AVOID__", json.dumps(avoid))
        .replace("__GUARDED__", json.dumps(guarded))
        .replace("__NAME__", json.dumps(display_name or ""))
        .replace("__FILL_NAME__", "true" if fill_name else "false")
    )


def build_in_call_script() -> str:
    return _IN_CALL_JS


def build_mute_script() -> str:
    """A click pass that can only ever mute, never unmute."""
    return build_click_script(list(MUTE_BUTTON_TEXTS), avoid_texts=MUTE_AVOID_TEXTS)


def ordered_button_texts(provider_id: str, configured: list[str]) -> list[str]:
    """Provider priority texts first, then the administrator's list."""
    flow = flow_for(provider_id)
    out: list[str] = []
    for text in list(flow.priority_texts) + list(configured):
        cleaned = str(text).strip()
        if cleaned and cleaned.lower() not in {o.lower() for o in out}:
            out.append(cleaned)
    return out


def prepare_url(provider_id: str, url: str) -> str:
    """Apply provider URL tweaks (e.g. asking Zoom for its web client)."""
    flow = flow_for(provider_id)
    extras = {k: v for k, v in flow.url_extras.items() if v}
    if not extras or not url:
        return url

    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in extras.items():
        query.setdefault(key, value)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )
