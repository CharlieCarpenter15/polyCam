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
"""

from __future__ import annotations

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
_CLICKER_JS = r"""
(function () {
  var WANTED = __PATTERNS__;
  var NAME = __NAME__;
  var FILL_NAME = __FILL_NAME__;

  function norm(s) {
    return (s || "").replace(/\s+/g, " ").trim().toLowerCase();
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

  // Optionally fill a "your name" box so a guest join can proceed.
  function fillName(root, depth) {
    if (!FILL_NAME || !NAME) return false;
    var inputs;
    try {
      inputs = root.querySelectorAll('input[type="text"], input:not([type]), input[type="search"]');
    } catch (e) { return false; }
    for (var i = 0; i < inputs.length; i++) {
      var el = inputs[i];
      if (!visible(el) || el.value) continue;
      var hint = norm((el.getAttribute("aria-label") || "") + " " +
                      (el.placeholder || "") + " " + (el.name || "") + " " + (el.id || ""));
      if (hint.indexOf("name") === -1) continue;
      if (hint.indexOf("meeting") !== -1 || hint.indexOf("code") !== -1) continue;
      try {
        el.focus();
        el.value = NAME;
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
      } catch (e) { /* ignore */ }
    }
    if (depth < 4) {
      var all;
      try { all = root.querySelectorAll("*"); } catch (e) { return false; }
      for (var k = 0; k < all.length; k++) {
        if (all[k].shadowRoot && fillName(all[k].shadowRoot, depth + 1)) return true;
      }
    }
    return false;
  }

  var namedFilled = fillName(document, 0);

  var candidates = [];
  collect(document, candidates, 0);

  var best = null, bestRank = 1e9, bestText = "";
  for (var i = 0; i < candidates.length; i++) {
    var el = candidates[i];
    if (!visible(el)) continue;
    var text = label(el);
    if (!text || text.length > 80) continue;
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
    return JSON.stringify({ clicked: null, filled_name: namedFilled,
                            candidates: candidates.length, url: location.href });
  }
  try {
    best.scrollIntoView({ block: "center" });
  } catch (e) { /* ignore */ }
  try {
    best.click();
  } catch (e) {
    return JSON.stringify({ clicked: null, error: String(e), url: location.href });
  }
  return JSON.stringify({ clicked: bestText, filled_name: namedFilled,
                          candidates: candidates.length, url: location.href });
})()
"""

# A tiny probe used to decide whether the room is already in the meeting: the
# presence of a leave/hang-up control is the most reliable cross-provider signal.
_IN_CALL_JS = r"""
(function () {
  function norm(s) { return (s || "").replace(/\s+/g, " ").trim().toLowerCase(); }
  var HINTS = ["leave call", "leave meeting", "leave (", "hang up", "end call",
               "end meeting", "leave now"];
  var nodes;
  try {
    nodes = document.querySelectorAll('button, [role="button"], [aria-label]');
  } catch (e) { return "false"; }
  for (var i = 0; i < nodes.length; i++) {
    var el = nodes[i];
    var text = norm(el.innerText || el.textContent || "") + " " +
               norm(el.getAttribute && el.getAttribute("aria-label"));
    for (var h = 0; h < HINTS.length; h++) {
      if (text.indexOf(HINTS[h]) !== -1) return "true";
    }
  }
  return "false";
})()
"""


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
) -> str:
    """Render the clicker with the given button texts."""
    import json as _json

    patterns = [str(t).strip() for t in button_texts if str(t).strip()]
    return (
        _CLICKER_JS.replace("__PATTERNS__", _json.dumps(patterns))
        .replace("__NAME__", _json.dumps(display_name or ""))
        .replace("__FILL_NAME__", "true" if fill_name else "false")
    )


def build_in_call_script() -> str:
    return _IN_CALL_JS


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
