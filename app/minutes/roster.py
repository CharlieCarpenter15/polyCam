"""Reading the meeting window: who is on the call, and who is talking.

Teams, Google Meet and Zoom already know exactly who has dialled in and which
of them currently has the floor, and they draw both on the screen. Reading that
back out of the page is far more accurate than inferring it from a microphone,
and it costs the Raspberry Pi almost nothing. This module is that reader.

**Two honest limits, stated here rather than buried.**

*It can only ever name the people on the far end.* Everybody physically in the
room shares one seat in the meeting — the appliance's own — so to Teams they
are a single participant called whatever the room is called. Telling apart the
voices around the table is a different problem, solved elsewhere with the room
microphone, the camera and voice profiles. Nothing here can help with it, and
nothing here should pretend to.

*It reads other people's markup, so it will break.* Every selector below is
somebody else's private implementation detail. Microsoft, Google and Zoom
change theirs without notice and without apology: the Teams speaking ring, once
the standard way to do this, is inert on the current client and has been
measured producing zero transitions across a whole meeting. **Assume this file
needs revisiting once or twice a year.** Three things are done to make that
survivable rather than mysterious:

* Selectors are tried in ordered families and the reply says *which* one
  answered, so a rotted selector is visible in the diagnostics.
* When a provider surface is found but no speaking signal appears for an entire
  meeting, one ``minutes.speaker_signal_absent`` event is logged. That single
  line is what says "a vendor shipped a breaking update", months before anybody
  in a meeting room notices the transcript got vaguer.
* Nothing here is load-bearing. Failure means a transcript with fewer names in
  it, which is exactly the transcript there would have been anyway.

**Captions first, active speaker second.** Where a human has switched live
captions on, Teams and Meet write down each remote sentence with the speaker's
name already attached. That is a speaker-attributed transcript handed over for
free — no diarisation, no timeline alignment, no second-guessing. So captions
are the primary route, the active-speaker highlight is the fallback, and Zoom
(which renders no captions in the browser) has the fallback only.

**Why a resident observer rather than a poll.** The interesting signals are
*edges*: a voice bar animating, a caption line appearing and being rewritten as
it is recognised. A Python-side poll every couple of seconds sees almost none
of them. So one small script is installed in the page once per meeting; it
samples at 250 ms, accumulates, and Python drains the accumulation every two
seconds. It has to be drained rather than subscribed to, because ``cdp.py``
discards every protocol frame that is not the reply it is waiting for, so a
page-resident script has no way to push.

The script never clicks, never opens a panel and never types. The page is on a
television in front of people; the appliance reads it, it does not operate it.
There are two exceptions, ``MINUTES_TURN_ON_CAPTIONS`` and
``MINUTES_OPEN_ROSTER``, both off by default and for the same reason: switching
captions on, or opening the participant list, is a visible change to
everybody's screen and ought to be somebody's decision. Each is one attempt,
once, at the start of a meeting.

Names are never logged. This module handles the names of everyone in every
meeting the room hosts and, through captions, what they said; only counts and
selector names reach the journal.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..join_flows import build_click_script
from ..logging_setup import get_logger, log_event
from .transcript import SOURCE_ROSTER, TRACK_FAR_END, Segment

log = get_logger("minutes.roster")

# ---------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------

#: How often the in-page observer looks at the DOM. 250 ms is the coarsest rate
#: that still resolves the boundary between one person stopping and the next
#: starting; below about 150 ms it is sampling animation frames for no gain.
TICK_MS = 250

#: How often a still-unchanged speaking state is re-recorded. Samples are
#: written on every *change*, which is what makes turn boundaries sharp, plus
#: this heartbeat so that a long uninterrupted turn is not stored as a single
#: instant. It matches the drain interval, so a drain never comes back empty
#: while somebody is talking.
HEARTBEAT_MS = 2000

#: How long a caption line must sit unchanged before it is handed to Python.
#: These interfaces rewrite a line repeatedly as the sentence is recognised
#: ("so", "so the", "so the plan is"); waiting a moment means the transcript
#: gets the sentence rather than five drafts of it.
CAPTION_SETTLE_MS = 1200

#: The normal drain interval, matching the join loop's own pass interval.
POLL_SECONDS = 2.0
#: ...after a run of failures, and after a longer run. Backing off is how a
#: reader that has lost its footing stops being a load on the meeting page.
SLOW_POLL_SECONDS = 5.0
SLOWEST_POLL_SECONDS = 10.0
SLOW_AFTER_FAILURES = 3
SLOWEST_AFTER_FAILURES = 10

#: The meeting page may not be on screen yet when a recording starts — the room
#: can still be working through a pre-join screen. Before the observer has ever
#: been installed, "no meeting on screen" is therefore treated as "not yet"
#: for this long. Once it *has* been installed, the same answer means the
#: meeting has ended and sampling stops immediately.
INSTALL_GRACE_SECONDS = 45.0

#: A drained sample claiming to be older than this means the page clock and the
#: appliance clock have diverged (an NTP step mid-meeting). Such a sample is
#: anchored to the drain instead, which loses a little precision and cannot
#: produce the thousand-second spans a raw clock difference would.
MAX_SAMPLE_AGE_SECONDS = 300.0

#: Ceilings on what is held in memory and in the page, so neither a very long
#: meeting nor a backend that has stopped draining can grow without bound.
MAX_MEMORY_SAMPLES = 20000

# ---------------------------------------------------------------------------
# Turning caption lines into transcript segments
# ---------------------------------------------------------------------------

#: Below this many caption-derived segments, the far end is *not* considered
#: covered and its audio is transcribed here as usual.
#:
#: A segment is roughly one speaking turn, so a dozen of them is a minute or
#: two of real conversation. The number has to be this high because of what the
#: answer is used for: ``service.py`` skips transcribing the far-end audio
#: track entirely when captions are judged to cover it. Somebody switching
#: captions on for twenty seconds and off again would otherwise throw away the
#: whole remote half of the meeting, which is a far worse outcome than
#: transcribing it twice.
MIN_USEFUL_CAPTIONS = 12

#: Consecutive caption lines from one speaker closer together than this are one
#: segment. Caption lines arrive a sentence at a time, so the gap between two
#: lines of the same turn is a breath, not a handover.
CAPTION_MERGE_GAP_SECONDS = 3.0

#: ...but never merge past this, or one stuck attribution swallows a meeting.
CAPTION_MAX_SEGMENT_SECONDS = 120.0

#: Used to guess how long the last line of a segment took to say, so the
#: segment has a plausible end rather than a zero-length one. 150 words a
#: minute is ordinary meeting speech.
CAPTION_SECONDS_PER_WORD = 0.4
CAPTION_MIN_SECONDS = 1.0
CAPTION_MAX_SECONDS = 20.0

#: How much to trust a caption's own attribution. High, but not certain: the
#: meeting app named the speaker itself, and it is only ever wrong when its own
#: audio routing is.
CAPTION_CONFIDENCE = 0.9

#: File names inside a session directory.
ROSTER_FILE = "roster.jsonl"
CAPTIONS_FILE = "captions.jsonl"

# ---------------------------------------------------------------------------
# Switching captions on, when an administrator has asked for that
# ---------------------------------------------------------------------------

#: Matched by visible text, the same way ``join_flows`` matches everything —
#: class names rot, the words on a button do not.
CAPTION_BUTTON_TEXTS: tuple[str, ...] = (
    "Turn on live captions",
    "Turn on captions",
    "Turn on subtitles",
    "Turn captions on",
    "Live captions",
    "Captions",
)

#: ...and what must never be pressed while looking for them. "Captions" is a
#: substring of "Turn off captions", so without this list the one pass allowed
#: could switch captions *off* for a room that already had them on.
CAPTION_AVOID_TEXTS: tuple[str, ...] = (
    "turn off",
    "hide caption",
    "hide subtitle",
    "caption settings",
    "captions settings",
    "subtitle settings",
    "language",
    "disable",
    "stop",
)

# ---------------------------------------------------------------------------
# Opening the participant list, when an administrator has asked for that
# ---------------------------------------------------------------------------
#
# Some tenants name almost nobody until the panel is open: the tiles carry a
# stream id rather than a person, and the meeting is a wall of initials. This
# is the cure, and it is off by default because the cure is visible — the panel
# lands on the television and shrinks everybody's video.

#: What "the panel is already open" looks like, per provider. These are the
#: same subtrees the probes read names out of, minus anything that could match
#: the button rather than the panel: mistaking the control for the panel would
#: mean deciding it is open and never pressing it.
ROSTER_PANEL_SELECTORS: dict[str, tuple[str, ...]] = {
    "teams": (
        '[data-tid="roster"]',
        '[data-tid="people-pane"]',
        '[data-tid="roster-section"]',
        '[data-tid*="participant-list"]',
        "#roster-container",
        ".ts-calling-roster",
        '[role="tree"][aria-label*="articipant"]',
    ),
    "meet": (
        'div[aria-label="Participants"][role="list"]',
        '[aria-label*="articipant"][role="list"]',
        '[aria-label*="veryone"][role="list"]',
    ),
    "zoom": (
        "#participants-ul",
        ".participants-section-container",
        '[class*="participants-item__display-name"]',
        '[class*="participants-li"]',
    ),
}

#: Matched by visible text, the same way everything else here is matched —
#: class names rot, the words on a button do not. Most specific first, because
#: an exact match beats a "contains" one.
ROSTER_BUTTON_TEXTS: dict[str, tuple[str, ...]] = {
    "teams": ("Show participants", "Show people", "People", "Participants"),
    "meet": ("Show everyone", "People", "Participants"),
    "zoom": ("Open the participants list pane", "Manage participants", "Participants"),
}

#: ...and what must never be pressed while looking for one. Two kinds of entry
#: are here: the ones that would close a panel somebody already opened, and the
#: ones that would do something to the meeting nobody asked for. A control that
#: mentions leaving the call is the reason this list is not optional.
ROSTER_AVOID_TEXTS: tuple[str, ...] = (
    "hide",
    "close",
    "collapse",
    "leave",
    "end call",
    "end meeting",
    "hang up",
    "invite",
    "add people",
    "add someone",
    "remove",
    "mute all",
    "unmute",
    "chat",
    "settings",
    "search",
    "copy",
    "report",
    "record",
    "share",
)

#: Strings a probe can legitimately produce that are diagnostics, not people.
#: An unknown speaker stays unknown; a machine token must never be printed in
#: a transcript as though somebody said the words.
_NOT_A_NAME = re.compile(
    r"^(?:exception:|no-provider-surface$|no-speaking-signal$|roster-closed$|"
    r"no-participants$|not-installed$|unknown$)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# The JavaScript
# ---------------------------------------------------------------------------
#
# Everything below is ES5 — ``var``, no arrow functions, no template literals —
# to match the constraint the clicker in ``join_flows.py`` works under: this
# runs in whatever Chromium the Pi happens to have.
#
# Rules every probe obeys:
#   * It never throws. Every DOM access sits inside try/catch and the outer
#     function has a catch that returns ``ok: false, reason: "exception:…"``.
#   * ``ok: false`` means "I could not find the meeting". ``ok: true`` with an
#     empty ``speaking`` list means "I found it and nobody is talking" — a
#     completely different fact, and the difference is the whole point.
#   * It never clicks, never opens a panel and never writes to the page.
#   * It leaks nothing but names: no markup, participants capped at 200, each
#     name capped at 120 characters.

_PRELUDE_JS = r"""
  function norm(s) {
    return (s || "").replace(/[‘’]/g, "'").replace(/\s+/g, " ").trim();
  }

  // Every document that may legitimately be read: this one, plus same-origin
  // iframes. Teams renders the meeting stage inside an iframe, so without this
  // there is nothing to find on Teams at all. Cross-origin frames throw on
  // contentDocument, which is expected and fine — join_flows.py does exactly
  // the same thing and says so.
  function docs() {
    var out = [];
    try { if (document) out.push(document); } catch (e) { return out; }
    var frames;
    try { frames = document.querySelectorAll("iframe"); } catch (e) { return out; }
    for (var i = 0; i < frames.length && out.length < 8; i++) {
      try {
        var d = frames[i].contentDocument;
        if (d && d.body) out.push(d);
      } catch (e) { /* cross-origin: nothing we can do, and that is fine */ }
    }
    return out;
  }

  // Query every readable document and every OPEN shadow root beneath them.
  // A closed shadow root is unreachable from injected script at all; none of
  // the three providers uses one for the meeting surface today, so this is
  // insurance rather than a foundation. Bounded hard: a Teams page is around
  // ten thousand nodes and this runs on a Raspberry Pi.
  function deepQuery(selector, limit) {
    var out = [];
    var cap = limit || 400;
    var roots = docs();
    var seenHosts = 0;
    for (var r = 0; r < roots.length && out.length < cap; r++) {
      collectFrom(roots[r], selector, out, cap, 0);
    }
    return out;

    function collectFrom(root, sel, acc, max, depth) {
      if (!root || depth > 4 || acc.length >= max) return;
      var nodes;
      try { nodes = root.querySelectorAll(sel); } catch (e) { return; }
      for (var i = 0; i < nodes.length && acc.length < max; i++) acc.push(nodes[i]);
      // Only descend into shadow roots when the flat query found nothing: the
      // "*" sweep is the expensive part of this function by an order of
      // magnitude, and it is almost never needed.
      if (acc.length > 0 || seenHosts > 2000) return;
      var all;
      try { all = root.querySelectorAll("*"); } catch (e) { return; }
      for (var j = 0; j < all.length && acc.length < max; j++) {
        seenHosts++;
        if (seenHosts > 2000) return;
        if (all[j].shadowRoot) collectFrom(all[j].shadowRoot, sel, acc, max, depth + 1);
      }
    }
  }

  // Try a family of selectors in order and report which one answered. Knowing
  // *which* matched is what turns a silent rot into a visible one.
  function firstMatch(selectors, limit) {
    for (var i = 0; i < selectors.length; i++) {
      var found = deepQuery(selectors[i], limit);
      if (found.length) return { selector: selectors[i], nodes: found };
    }
    return { selector: "", nodes: [] };
  }

  function textOf(el) {
    if (!el) return "";
    var t = "";
    try { t = norm(el.innerText || el.textContent || ""); } catch (e) { t = ""; }
    return t;
  }

  function attr(el, name) {
    try { return norm((el && el.getAttribute && el.getAttribute(name)) || ""); }
    catch (e) { return ""; }
  }

  function pick(el, selectors) {
    if (!el) return null;
    for (var i = 0; i < selectors.length; i++) {
      try {
        var found = el.querySelector(selectors[i]);
        if (found) return found;
      } catch (e) { /* an unsupported selector must not stop the rest */ }
    }
    return null;
  }

  // A display name has to look like one. This rejects Material icon ligatures
  // (Meet renders "more_vert" as text), clock readings, machine tokens, and
  // email addresses — some Teams builds put the address in the attribute the
  // name usually lives in, and an address is personal data, not a name.
  var ICON_WORDS = " more_vert mic mic_off videocam videocam_off present_to_all keep " +
                   "pin_drop devices speaker microphone camera share chat ";
  function plausibleName(raw) {
    var n = norm(raw);
    if (!n || n.length > 120) return "";
    if (n.indexOf("@") !== -1) return "";
    if (/^\d{1,2}:\d{2}/.test(n)) return "";                        // "10:42 AM"
    if (ICON_WORDS.indexOf(" " + n.toLowerCase() + " ") !== -1) return "";
    if (/^(?:[a-z0-9]+(?:[-_][a-z0-9]+)+|\d+)$/.test(n)) return ""; // video-stream-2
    return n;
  }

  // The apps decorate names: "Alice Ng, muted", "Bob (Guest)", "Cara, video is on".
  function cleanName(raw) {
    var n = norm(raw);
    var cuts = [", video is on", ", muted", ", Context menu is available",
                ", Muted", " (Unverified)", " left the meeting", " Leaving..."];
    for (var i = 0; i < cuts.length; i++) {
      var at = n.indexOf(cuts[i]);
      if (at > 0) n = n.slice(0, at);
    }
    n = n.split(",")[0];
    n = n.replace(/\s*\((Guest|Unverified|You|Me|Host|Co-host|Presenter|Attendee)\)\s*$/i, "");
    return plausibleName(n);
  }

  function roleFor(label) {
    var l = (label || "").toLowerCase();
    if (l.indexOf("(you)") !== -1 || l.indexOf(" you)") !== -1) return "self";
    if (l.indexOf("organizer") !== -1 || l.indexOf("organiser") !== -1 ||
        l.indexOf("host") !== -1) return "host";
    if (l.indexOf("presenter") !== -1) return "presenter";
    if (l.indexOf("attendee") !== -1) return "attendee";
    if (l.indexOf("guest") !== -1) return "guest";
    return "participant";
  }

  function pushUnique(list, seen, name, role) {
    var key = (name || "").toLowerCase();
    if (!name || seen[key]) return;
    seen[key] = true;
    if (list.length < 200) list.push({ name: name, role: role || "participant" });
  }

  function reply(provider, participants, speaking, source, health, reason, captions) {
    var ok = participants.length > 0 || speaking.length > 0;
    return {
      provider: provider,
      participants: participants,
      speaking: speaking,
      captions: captions || [],
      ok: ok,
      reason: ok ? "" : (reason || "no-provider-surface"),
      source: source || "",
      at: Date.now(),
      health: health || {}
    };
  }

  function failed(provider, err) {
    return {
      provider: provider, participants: [], speaking: [], captions: [], ok: false,
      reason: "exception:" + (err && err.name ? err.name : "unknown"),
      source: "", at: Date.now(), health: {}
    };
  }

  // Caption lines are read as {node, speaker, text}. The node is carried so the
  // resident observer can tell "the same line, one word longer" from "a new
  // line" — these lists are virtualised and rewrite a row in place while the
  // sentence is still being recognised.
  function pairRows(rows, speakerSel, textSel) {
    var out = [];
    for (var i = 0; i < rows.length && out.length < 60; i++) {
      var row = rows[i];
      var speaker = textOf(pick(row, speakerSel));
      var text = textOf(pick(row, textSel));
      if (!text) continue;
      out.push({ node: row, speaker: cleanName(speaker), text: text });
    }
    return out;
  }
"""

# --- Microsoft Teams -------------------------------------------------------
#
# Three DOM generations coexist and the client silently picks one, so every
# tile family is tried in turn. The speaking ring deserves a warning: on the
# current client the element still exists but its class list never changes and
# ``data-is-speaking`` has been removed, so the class tests below fire rarely
# or never. Two independent measurements in 2025-26 put it near zero. It is
# kept because it costs nothing and still works on older tenants, but captions
# are what actually names a Teams speaker today.
_TEAMS_JS = r"""
  var TEAMS_CAP = ['[data-tid="closed-caption-v2-virtual-list-content"]',
                   '[data-tid="closed-caption-renderer-wrapper"]',
                   '[data-tid="closed-captions-renderer"]',
                   '[aria-label="Live Captions"]'];

  function teamsCaptionBox() { return firstMatch(TEAMS_CAP, 2); }

  function teamsCaptionRows() {
    var box = teamsCaptionBox();
    if (!box.nodes.length) return [];
    var rows = [];
    try { rows = box.nodes[0].querySelectorAll(".fui-ChatMessageCompact"); } catch (e) { rows = []; }
    var paired = pairRows(rows, ['[data-tid="author"]'], ['[data-tid="closed-caption-text"]']);
    if (paired.length) return paired;

    // The host view interposes an extra wrapper, which breaks the containment
    // above. Fall back to pairing authors and texts by document order, which is
    // what the rendered list actually guarantees.
    var authors = [], texts = [];
    try { authors = box.nodes[0].querySelectorAll('[data-tid="author"]'); } catch (e) { authors = []; }
    try { texts = box.nodes[0].querySelectorAll('[data-tid="closed-caption-text"]'); } catch (e) { texts = []; }
    var out = [];
    for (var i = 0; i < texts.length && i < 60; i++) {
      var text = textOf(texts[i]);
      if (!text) continue;
      out.push({
        node: texts[i],
        speaker: cleanName(i < authors.length ? textOf(authors[i]) : ""),
        text: text
      });
    }
    return out;
  }

  function teamsProbe() {
    try {
      var participants = [], seen = {}, speaking = [], speakSeen = {};
      var health = { tiles: 0, roster: 0, withSignal: 0, captions: 0 };
      var sources = [];

      // 1. Tiles. Whichever of the three generations is on screen.
      var TILE_SETS = [
        { sel: '[data-cid="calling-participant-stream"]', nameFrom: "aria-label" },
        { sel: '[data-stream-type="Video"][data-tid]',    nameFrom: "data-tid"   },
        { sel: '[data-stream-type][data-tid]',            nameFrom: "data-tid"   },
        { sel: '[data-tid="menur1j"]',                    nameFrom: "aria-label" }
      ];
      var tiles = [], tileNameFrom = "";
      for (var s = 0; s < TILE_SETS.length && !tiles.length; s++) {
        tiles = deepQuery(TILE_SETS[s].sel, 200);
        if (tiles.length) {
          tileNameFrom = TILE_SETS[s].nameFrom;
          sources.push("tiles:" + TILE_SETS[s].sel);
        }
      }
      health.tiles = tiles.length;

      for (var i = 0; i < tiles.length; i++) {
        var tile = tiles[i];
        var label = attr(tile, "aria-label");
        var raw = tileNameFrom === "data-tid"
          ? (attr(tile, "data-tid") || label)
          : (label || attr(tile, "data-tid"));
        var name = cleanName(raw);
        if (!name) continue;
        pushUnique(participants, seen, name, roleFor(label));

        // Muted is a hard veto: a muted tile cannot be the one talking.
        var muted = false;
        try {
          muted = !!tile.querySelector('[data-cid="roster-participant-muted"]') ||
                  / muted\b/i.test(label);
        } catch (e) { muted = false; }
        if (muted) continue;

        var outline = pick(tile, ['[data-tid="voice-level-stream-outline"]',
                                  '[data-tid="participant-speaker-ring"]']);
        if (!outline) continue;
        health.withSignal++;

        var isSpeaking = false;
        try {
          // (a) the legacy occlusion class, walking up to the root
          var cur = outline;
          while (cur && !isSpeaking) {
            if (cur.classList && cur.classList.contains("vdi-frame-occlusion")) isSpeaking = true;
            cur = cur.parentElement;
          }
          // (b) aria state — the only stated, non-hashed level indicator left
          if (!isSpeaking) {
            var aria = attr(outline, "aria-label") + " " + attr(outline, "aria-live") + " " +
                       attr(outline, "aria-description") + " " + attr(tile, "aria-description");
            if (/\b(speaking|talking)\b/i.test(aria) &&
                !/\bnot\s+(speaking|talking)\b|\bmuted\b/i.test(aria)) {
              isSpeaking = true;
            }
            if (attr(outline, "data-is-speaking") === "true") isSpeaking = true;
          }
          // (c) the legacy speaker ring: fully opaque means active. This is the
          // one computed-style read in the whole file and it only happens on
          // old tenants, where the ring is the only signal there is.
          if (!isSpeaking && attr(outline, "data-tid") === "participant-speaker-ring") {
            var op = 1;
            try { op = parseFloat(window.getComputedStyle(outline).opacity || "1"); }
            catch (e2) { op = 0; }
            if (op === 1) isSpeaking = true;
          }
        } catch (e) { isSpeaking = false; }

        if (isSpeaking && !speakSeen[name.toLowerCase()]) {
          speakSeen[name.toLowerCase()] = true;
          speaking.push(name);
        }
      }

      // 2. The roster panel, if a human has it open. A different subtree with a
      // different lifecycle: it survives gallery layouts that drop tiles
      // entirely. Read only — opening it would change what the room sees.
      var PANEL = ['[data-tid="roster"]', '[data-tid="people-pane"]', '[data-tid="roster-section"]',
                   '[data-tid*="participant-list"]', '#roster-container', '.ts-calling-roster',
                   '[role="tree"][aria-label*="articipant"]', '[aria-label*="Participants"]'];
      var ENTRY = ['[data-tid="roster-participant"]', '[data-tid*="roster-item"]',
                   '[data-tid*="participantRosterListItem"]', '[data-tid="calling-roster-cell"]',
                   '[role="treeitem"]', '.roster-list-item', '.ts-calling-roster-item'];
      var panel = firstMatch(PANEL, 4);
      if (panel.nodes.length) {
        for (var e = 0; e < ENTRY.length; e++) {
          var rows = [];
          try { rows = panel.nodes[0].querySelectorAll(ENTRY[e]); } catch (ex) { rows = []; }
          if (!rows.length) continue;
          sources.push("roster:" + ENTRY[e]);
          health.roster = rows.length;
          for (var r = 0; r < rows.length && r < 200; r++) {
            var rl = attr(rows[r], "aria-label") || textOf(rows[r]);
            var rn = cleanName(rl);
            if (rn) pushUnique(participants, seen, rn, roleFor(rl));
          }
          break;
        }
      }

      // 3. Live captions. On the current client this is the only per-speaker
      // signal that genuinely works, and it survives server-mixed audio.
      var captions = [];
      var capRows = teamsCaptionRows();
      var newest = "";
      if (capRows.length) {
        sources.push("captions:rows");
        health.captions = capRows.length;
        for (var c = 0; c < capRows.length; c++) {
          captions.push({ speaker: capRows[c].speaker, text: capRows[c].text });
        }
        newest = capRows[capRows.length - 1].speaker;
      } else {
        // No structured rows: fall back to the list's own text. Attribute the
        // moment to the SINGLE name appearing latest in the tail — the list
        // shows several recent entries, and marking them all turns sequential
        // speech into a room full of people talking over each other.
        var box = teamsCaptionBox();
        if (box.nodes.length) {
          var capText = textOf(box.nodes[0]);
          health.captions = capText.length;
          if (capText) {
            sources.push("captions:" + box.selector);
            var tail = capText.slice(-400);
            var newestAt = -1;
            for (var p = 0; p < participants.length; p++) {
              var found = tail.lastIndexOf(participants[p].name);
              if (found > newestAt) { newestAt = found; newest = participants[p].name; }
            }
          }
        }
      }
      if (newest) {
        pushUnique(participants, seen, newest, "participant");
        if (!speakSeen[newest.toLowerCase()]) {
          speakSeen[newest.toLowerCase()] = true;
          speaking.push(newest);
        }
      }

      var why = health.tiles || health.roster ? "no-speaking-signal" : "no-provider-surface";
      return reply("teams", participants, speaking, sources.join("|"), health, why, captions);
    } catch (err) {
      return failed("teams", err);
    }
  }
"""

# --- Google Meet -----------------------------------------------------------
#
# Meet's most durable family by far is role + aria-label: the participant
# listitem's own aria-label *is* the name. The speaking indicator, by contrast,
# is Closure build output and rotates with every release, which is why several
# class names are tried and why the reply says which one answered.
_MEET_JS = r"""
  function meetCaptionRows() {
    var region = firstMatch(['[role="region"][aria-label*="aption" i]', ".a4cQT"], 2);
    if (!region.nodes.length) return [];
    var rows = [];
    try {
      rows = region.nodes[0].querySelectorAll('div[jsname="dsyhDe"], div.CNusmb, div.TBMuR');
    } catch (e) { rows = []; }
    // jsname values are semantic ids in Google's own sources rather than build
    // hashes, so they outlive the class names beside them by a long way.
    return pairRows(
      rows,
      ['div.KcIKyf', 'div.zs7s8d', 'span[jsname="YSxPC"]'],
      ['div.bh44bd', 'span[jsname="tgaKEf"]', 'div.iTTPOb', '[data-message-text]']
    );
  }

  function meetProbe() {
    try {
      var participants = [], seen = {}, speaking = [], speakSeen = {};
      var health = { tiles: 0, roster: 0, merged: 0, captions: 0 };
      var sources = [];
      var SPEAK_SEL = '.IisKdb.Oaajhc, .IisKdb.HX2H7, .IisKdb.wEsLMd, .IisKdb.OgVli, ' +
                      '.kssMZb, [data-is-speaking="true"], [aria-label*="is speaking" i]';

      // 1. The participants panel, when a human has it open.
      var PANEL = ['div[aria-label="Participants"][role="list"]',
                   '[aria-label="Participants"]',
                   '[aria-label*="articipant"][role="list"]'];
      var panel = firstMatch(PANEL, 3);
      if (panel.nodes.length) {
        var rows = [];
        try { rows = panel.nodes[0].querySelectorAll('[role="listitem"]'); } catch (e) { rows = []; }
        health.roster = rows.length;
        if (rows.length) sources.push("roster:" + panel.selector);
        for (var i = 0; i < rows.length && i < 200; i++) {
          var row = rows[i], label = attr(row, "aria-label");

          // Adaptive audio: when Meet hears several laptops in one physical
          // room it merges their audio and groups them as one cohort. Their
          // speech cannot honestly be pinned on any individual, so the cohort
          // is recorded as itself rather than as whoever it is named after.
          var merged = false;
          try {
            merged = label === "Merged audio" ||
                     !!row.querySelector('[aria-label="Adaptive audio group"]');
          } catch (e2) { merged = false; }
          if (merged) health.merged++;

          var name = cleanName(label) || cleanName(textOf(row));
          if (!name) continue;
          pushUnique(participants, seen, name, merged ? "merged" : roleFor(label));

          var isSpeaking = false;
          try {
            if (row.querySelector(SPEAK_SEL)) isSpeaking = true;
            if (!isSpeaking) {
              // Any class on the level meter other than the silence class.
              // Written this way so a rotated "speaking" hash still registers
              // as long as the silence class keeps its meaning.
              var meter = row.querySelector(".IisKdb");
              if (meter && meter.classList && !meter.classList.contains("gjg47c") &&
                  meter.classList.length > 1) isSpeaking = true;
            }
            if (!isSpeaking && row.querySelector('img[src*="mic_unmuted"]')) isSpeaking = true;
          } catch (e3) { isSpeaking = false; }

          if (isSpeaking && !speakSeen[name.toLowerCase()]) {
            speakSeen[name.toLowerCase()] = true;
            speaking.push(name);
          }
        }
      }

      // 2. Video tiles, so names still work with the panel closed.
      var tiles = deepQuery("[data-participant-id]", 200);
      health.tiles = tiles.length;
      if (tiles.length) sources.push("tiles:[data-participant-id]");
      var NAME_SEL = ["span.notranslate", "[data-self-name]", ".zWGUib", ".cS7aqe.N2K3jd", ".XWGOtd"];
      for (var t = 0; t < tiles.length; t++) {
        var tile = tiles[t], tname = "";
        var el = pick(tile, NAME_SEL);
        if (el) tname = cleanName(textOf(el) || attr(el, "data-self-name"));
        if (!tname) continue;
        pushUnique(participants, seen, tname, "participant");
        try {
          if (tile.querySelector(SPEAK_SEL) && !speakSeen[tname.toLowerCase()]) {
            speakSeen[tname.toLowerCase()] = true;
            speaking.push(tname);
          }
        } catch (e5) { /* ignore */ }
      }

      // 3. Captions. Meet's own transcription names the speaker for us.
      var captions = [], capRows = meetCaptionRows();
      if (capRows.length) {
        sources.push("captions:rows");
        health.captions = capRows.length;
        for (var c = 0; c < capRows.length; c++) {
          captions.push({ speaker: capRows[c].speaker, text: capRows[c].text });
        }
        var cname = capRows[capRows.length - 1].speaker;
        if (cname) {
          pushUnique(participants, seen, cname, "participant");
          if (!speakSeen[cname.toLowerCase()]) {
            speakSeen[cname.toLowerCase()] = true;
            speaking.push(cname);
          }
        }
      }

      var why = health.roster || health.tiles ? "no-speaking-signal" : "no-provider-surface";
      return reply("meet", participants, speaking, sources.join("|"), health, why, captions);
    } catch (err) {
      return failed("meet", err);
    }
  }
"""

# --- Zoom ------------------------------------------------------------------
#
# The friendliest markup of the three: Zoom names its classes after what they
# are rather than hashing them, and has done for years. There are no captions
# in the browser client, so the active-speaker tile is the only signal — which
# is honest enough, because the web client only ever receives mixed audio and
# Zoom's own choice of who to show is the only opinion available.
_ZOOM_JS = r"""
  function zoomCaptionRows() { return []; }

  function zoomNameFromTile(container) {
    if (!container) return "";
    var n = "";
    try {
      var footer = container.querySelector(".video-avatar__avatar-footer");
      if (footer) {
        var span = footer.querySelector('span[role="none"]') || footer.querySelector("span");
        n = textOf(span) || textOf(footer);
      }
      if (!n) n = textOf(container.querySelector(".video-avatar__avatar-name"));
      if (!n) {
        var img = container.querySelector(".video-avatar__avatar-img");
        if (img) n = norm(img.alt || "");
      }
    } catch (e) { n = ""; }
    return cleanName(n.replace(/\s*\((host|guest|me|co-host)[^)]*\)\s*/gi, ""));
  }

  function zoomProbe() {
    try {
      var participants = [], seen = {}, speaking = [], speakSeen = {};
      var health = { tiles: 0, roster: 0 };
      var sources = [];

      // 1. The participants panel, if it happens to be open. Read only.
      var ROSTER = [".participants-item__display-name",
                    '[class*="participants-item"] [class*="display-name"]',
                    '[class*="participants-li"] [class*="display-name"]',
                    '#participants-ul [class*="display-name"]',
                    '[class*="participants-item"] [class*="name"]'];
      var roster = firstMatch(ROSTER, 250);
      if (roster.nodes.length) {
        sources.push("roster:" + roster.selector);
        health.roster = roster.nodes.length;
        for (var i = 0; i < roster.nodes.length; i++) {
          var raw = textOf(roster.nodes[i]);
          var rn = cleanName(raw.replace(/\s*\((host|guest|me|co-host)[^)]*\)\s*/gi, ""));
          if (rn) pushUnique(participants, seen, rn, roleFor(raw));
        }
      }

      // 2. Video tiles — names with the panel closed.
      var tiles = deepQuery(".video-avatar__avatar", 200);
      health.tiles = tiles.length;
      if (tiles.length) sources.push("tiles:.video-avatar__avatar");
      for (var t = 0; t < tiles.length; t++) {
        var tn = zoomNameFromTile(tiles[t]);
        if (tn) pushUnique(participants, seen, tn, "participant");
      }

      // 3. Active speaker. Order matters: the speaker bar's --active frame is
      // the real "talking now" marker, whereas the main container may be a
      // tile a human has PINNED, which never tracks the speaker at all.
      var ACTIVE = [".speaker-bar-container__video-frame--active",
                    ".gallery-video-container__video-frame--active",
                    ".speaker-active-container__video-frame",
                    ".single-main-container__video-frame",
                    ".single-suspension-container__video-frame"];
      for (var a = 0; a < ACTIVE.length; a++) {
        var found = deepQuery(ACTIVE[a], 4);
        if (!found.length) continue;
        var sname = zoomNameFromTile(found[0]);
        if (!sname) continue;
        sources.push("speaking:" + ACTIVE[a]);
        pushUnique(participants, seen, sname, "participant");
        if (!speakSeen[sname.toLowerCase()]) {
          speakSeen[sname.toLowerCase()] = true;
          speaking.push(sname);
        }
        break;   // Zoom renders exactly one active speaker, never more
      }

      var why = health.tiles || health.roster ? "no-speaking-signal" : "no-provider-surface";
      return reply("zoom", participants, speaking, sources.join("|"), health, why, []);
    } catch (err) {
      return failed("zoom", err);
    }
  }
"""

#: The per-provider shim: whichever provider was asked for becomes ``probe``.
_SHIM_JS = r"""
  function probe() { return __PROVIDER__Probe(); }
  function captionRows() { return __PROVIDER__CaptionRows(); }
"""

#: When the provider is unknown (an unrecognised link, or Webex), try all three
#: families and remember which one answered. After the first successful tick it
#: costs exactly the same as a known provider.
_GENERIC_SHIM_JS = r"""
  var LATCHED = "";
  var FAMILIES = [
    { id: "teams", probe: teamsProbe, rows: teamsCaptionRows },
    { id: "meet",  probe: meetProbe,  rows: meetCaptionRows  },
    { id: "zoom",  probe: zoomProbe,  rows: zoomCaptionRows  }
  ];

  function family() {
    for (var i = 0; i < FAMILIES.length; i++) {
      if (FAMILIES[i].id === LATCHED) return FAMILIES[i];
    }
    return null;
  }

  function probe() {
    var known = family();
    if (known) {
      var held = known.probe();
      if (held.ok) return held;
      LATCHED = "";
    }
    var best = null;
    for (var i = 0; i < FAMILIES.length; i++) {
      var out = FAMILIES[i].probe();
      if (out.ok) { LATCHED = FAMILIES[i].id; return out; }
      if (!best) best = out;
    }
    return best;
  }

  function captionRows() {
    var known = family();
    return known ? known.rows() : [];
  }
"""

#: The resident observer. Installed once per meeting, ticks at 250 ms,
#: accumulates, and is drained by Python. It cannot push: ``cdp.py`` throws
#: away every protocol frame that is not the reply it is waiting for, so
#: ``Runtime.addBinding`` and console events never arrive.
_OBSERVER_JS = r"""
  var VERSION = 2;
  var RUN = __RUN__;
  var TICK_MS = __TICK_MS__;
  var HEARTBEAT_MS = __HEARTBEAT_MS__;
  var WANT_CAPTIONS = __CAPTIONS__;

  var live = window.__pcRoster;
  if (live && live.v === VERSION && live.run === RUN && !live.gone) {
    // Already watching this same recording. Re-installing defensively must not
    // start a second timer, and must not throw away what has been collected.
    live.at = Date.now();
    return JSON.stringify({ ok: true, state: "already" });
  }
  // A leftover from an earlier recording (two meetings back to back in one
  // page) must be torn down rather than inherited, or its captions would be
  // filed against the wrong meeting.
  if (live) {
    try { clearInterval(live.stop); } catch (e) { /* ignore */ }
    try { clearInterval(live.expiry); } catch (e) { /* ignore */ }
  }

  var state = {
    v: VERSION, run: RUN, at: Date.now(), seq: 0, host: "",
    samples: [], captions: [], capSeq: 0, capMem: [], sent: [],
    lastSpeaking: null, lastRoster: null, lastEmit: 0,
    roster: [], speaking: [], source: "", health: {}, reason: "",
    ok: false, surface: false, signal: false, gone: false,
    stop: null, expiry: null
  };
  try { state.host = location.host; } catch (e) { state.host = ""; }

  function teardown() {
    state.gone = true;
    try { clearInterval(state.stop); } catch (e) { /* ignore */ }
    try { clearInterval(state.expiry); } catch (e) { /* ignore */ }
  }

  function findLine(seq) {
    for (var i = state.captions.length - 1; i >= 0; i--) {
      if (state.captions[i].seq === seq) return state.captions[i];
    }
    return null;
  }

  // "The same sentence, one word longer" — the shape an interim caption takes
  // while it is still being recognised. This is only ever asked about one
  // element that still holds the same speaker's name, which is what makes a
  // bare prefix test safe enough: two-letter first drafts ("so", "I") are
  // common, so a minimum length here would treat every sentence's opening as a
  // line of its own.
  function grew(before, after) {
    if (before === after) return true;
    if (!before || !after) return false;
    return after.indexOf(before) === 0 || before.indexOf(after) === 0;
  }

  // Has this exact line just been recorded, or just been handed to Python? A
  // virtualised list can shuffle or re-render a row that has already been
  // dealt with, and the handed-over ring is what stops that becoming a
  // duplicate sentence after a drain has emptied the buffer.
  function recentlySaid(speaker, text) {
    var from = state.captions.length - 8;
    if (from < 0) from = 0;
    for (var i = from; i < state.captions.length; i++) {
      if (state.captions[i].speaker === speaker && state.captions[i].text === text) return true;
    }
    for (var j = 0; j < state.sent.length; j++) {
      if (state.sent[j].speaker === speaker && state.sent[j].text === text) return true;
    }
    return false;
  }

  function collectCaptions(now) {
    var rows = [];
    try { rows = captionRows(); } catch (e) { return; }
    if (!rows.length) return;
    var next = [];
    for (var i = 0; i < rows.length && i < 40; i++) {
      var speaker = rows[i].speaker || "";
      var text = rows[i].text || "";
      if (text.length > 2000) text = text.slice(0, 2000);
      if (!text) continue;

      var prev = null;
      for (var m = 0; m < state.capMem.length; m++) {
        if (state.capMem[m].node === rows[i].node) { prev = state.capMem[m]; break; }
      }
      if (prev && prev.speaker === speaker && grew(prev.text, text)) {
        var best = text.length >= prev.text.length ? text : prev.text;
        var line = findLine(prev.seq);
        if (line) {
          line.text = best;
          line.updated = now;
          next.push({ node: rows[i].node, speaker: speaker, text: best, seq: prev.seq });
          continue;
        }
        if (best === prev.text) {
          next.push({ node: rows[i].node, speaker: speaker, text: best, seq: prev.seq });
          continue;
        }
        // Already handed to Python and then revised: send the fuller reading
        // as its own line. Python folds the two back together.
        text = best;
      } else if (recentlySaid(speaker, text)) {
        // Dropping the rare genuine repeat costs a "Yes."; keeping it would
        // duplicate whole sentences.
        continue;
      }

      state.capSeq++;
      state.captions.push({
        seq: state.capSeq, at: now, speaker: speaker, text: text, updated: now
      });
      if (state.captions.length > 400) state.captions.splice(0, 200);
      next.push({ node: rows[i].node, speaker: speaker, text: text, seq: state.capSeq });
    }
    state.capMem = next.length > 40 ? next.slice(next.length - 40) : next;
  }

  function tick() {
    try {
      if (state.gone) return;
      var host = state.host;
      try { host = location.host; } catch (e) { /* keep the remembered one */ }
      if (host !== state.host) {
        // The page has moved somewhere else entirely. Stop rather than sample
        // whatever is on screen now and file it against this meeting.
        teardown();
        return;
      }

      var out = probe();
      var now = Date.now();
      state.seq++;
      state.ok = !!out.ok;
      state.reason = out.reason || "";
      state.source = out.source || "";
      state.health = out.health || {};

      var names = [];
      for (var i = 0; i < out.participants.length; i++) names.push(out.participants[i].name);
      state.roster = names;
      state.speaking = out.speaking || [];
      if (out.ok || (out.health && (out.health.tiles || out.health.roster))) state.surface = true;
      if (state.speaking.length) state.signal = true;

      // A sample is recorded when who is speaking changes — that is what makes
      // a turn boundary sharp — plus a heartbeat while somebody is still
      // talking, so a long turn is a span rather than an instant. Storing all
      // four ticks a second would be four times the disk for no more truth.
      var speakingKey = state.speaking.slice().sort().join("|");
      var rosterKey = names.slice().sort().join("|");
      var changed = speakingKey !== state.lastSpeaking || rosterKey !== state.lastRoster;
      var due = state.speaking.length && (now - state.lastEmit) >= HEARTBEAT_MS;
      if (changed || due) {
        var sample = {
          at: now,
          speaking: state.speaking.slice(0, 12),
          ok: !!out.ok,
          reason: out.reason || ""
        };
        // The roster only travels when it has changed. It is the same dozen
        // names every tick otherwise, and repeating them is most of the bytes.
        if (rosterKey !== state.lastRoster) sample.participants = names.slice(0, 200);
        state.samples.push(sample);
        if (state.samples.length > 4000) state.samples.splice(0, 2000);
        state.lastSpeaking = speakingKey;
        state.lastRoster = rosterKey;
        state.lastEmit = now;
      }

      if (WANT_CAPTIONS) collectCaptions(now);
    } catch (e) { /* a sampler must never break the page it is watching */ }
  }

  state.stop = setInterval(tick, TICK_MS);
  // Self-expiry. If Python stops draining for a minute and a half the meeting
  // is over or the backend has died, and nothing should still be ticking away
  // on the television in an empty room.
  state.expiry = setInterval(function () {
    try { if (Date.now() - state.at > 90000) teardown(); } catch (e) { /* ignore */ }
  }, 10000);
  window.__pcRoster = state;
  tick();
  return JSON.stringify({ ok: true, state: "installed" });
"""

#: The drain. Microseconds of page work: hand over what has accumulated and
#: clear it, so nothing is counted twice and the page's buffers stay small.
_DRAIN_JS = r"""
(function () {
  var RUN = __RUN__;
  var FLUSH = __FLUSH__;
  var SETTLE_MS = __SETTLE_MS__;
  var now = Date.now();
  var s = window.__pcRoster;
  if (!s || s.v !== 2 || s.run !== RUN) {
    return JSON.stringify({ ok: false, installed: false, reason: "not-installed", now: now });
  }
  if (s.gone) {
    return JSON.stringify({ ok: false, installed: false, reason: "page-moved-on", now: now });
  }
  s.at = now;                       // the self-expiry timer watches this

  var samples = s.samples;
  s.samples = [];

  // A caption line is handed over once it has stopped changing, or once its
  // row has scrolled out of the window and can no longer change. Everything
  // else is still a half-recognised sentence and is worth waiting for.
  var visible = {};
  for (var m = 0; m < s.capMem.length; m++) visible[s.capMem[m].seq] = true;
  var out = [], keep = [];
  if (!s.sent) s.sent = [];
  for (var i = 0; i < s.captions.length; i++) {
    var line = s.captions[i];
    if (FLUSH || !visible[line.seq] || (now - line.updated) >= SETTLE_MS) {
      out.push({ at: line.at, speaker: line.speaker, text: line.text });
      // Remembered so that a list which re-renders wholesale cannot hand the
      // same sentence over twice once this buffer has been emptied.
      s.sent.push({ speaker: line.speaker, text: line.text });
    } else {
      keep.push(line);
    }
  }
  s.captions = keep;
  if (s.sent.length > 16) s.sent.splice(0, s.sent.length - 16);

  return JSON.stringify({
    ok: true, installed: true, now: now, seq: s.seq,
    participants: s.roster, speaking: s.speaking,
    source: s.source, health: s.health, reason: s.reason,
    surface: !!s.surface, signal: !!s.signal,
    samples: samples, captions: out
  });
})()
"""

# The one pass that opens the participant list. It looks before it presses, in
# the same turn: a room that pressed a control somebody had already used would
# close the panel on them, which is worse than never having opened it.
_PANEL_JS = r"""
  var PANEL_SELECTORS = __PANEL__;
  var PANEL_WANTED = __WANTED__;
  var PANEL_AVOID = __AVOID__;

  function panelNorm(s) { return norm(s).toLowerCase(); }

  function panelVisible(el) {
    if (!el || el.disabled) return false;
    if (panelNorm(attr(el, "aria-disabled")) === "true") return false;
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

  function panelLabel(el) {
    var text = panelNorm(textOf(el));
    if (!text) text = panelNorm(attr(el, "aria-label") || attr(el, "title"));
    return text;
  }

  function panelAvoided(text) {
    for (var i = 0; i < PANEL_AVOID.length; i++) {
      var term = panelNorm(PANEL_AVOID[i]);
      if (term && text.indexOf(term) !== -1) return true;
    }
    return false;
  }

  function panelWants(text) {
    for (var i = 0; i < PANEL_WANTED.length; i++) {
      var want = panelNorm(PANEL_WANTED[i]);
      if (want && text.indexOf(want) !== -1) return true;
    }
    return false;
  }

  // Two ways to know the panel is already up. The subtree is the reliable one;
  // a control marked expanded is the only one available while the panel is
  // still animating in, and it is what the apps use to say so out loud.
  function panelOpen() {
    var found = firstMatch(PANEL_SELECTORS, 2);
    if (found.nodes.length) return found.selector;
    var toggles = deepQuery('[aria-expanded="true"]', 40);
    for (var i = 0; i < toggles.length; i++) {
      var text = panelLabel(toggles[i]);
      if (text && !panelAvoided(text) && panelWants(text)) return "aria-expanded";
    }
    return "";
  }

  function panelControl() {
    var nodes = deepQuery(
      'button, [role="button"], [role="menuitem"], [role="menuitemcheckbox"], [role="tab"]',
      600
    );
    var best = null, bestRank = 1e9, bestText = "";
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (!panelVisible(el)) continue;
      var text = panelLabel(el);
      if (!text || text.length > 80) continue;
      if (panelAvoided(text)) continue;
      for (var w = 0; w < PANEL_WANTED.length; w++) {
        var want = panelNorm(PANEL_WANTED[w]);
        if (!want) continue;
        // An exact match beats a "contains" one, and an earlier entry in the
        // provider's list beats a later one.
        var rank = text === want ? w : (text.indexOf(want) !== -1 ? 1000 + w : -1);
        if (rank >= 0 && rank < bestRank) {
          best = el; bestRank = rank; bestText = text;
        }
      }
    }
    return { el: best, text: bestText, seen: nodes.length };
  }

  function openRoster() {
    var open;
    try {
      open = panelOpen();
    } catch (e) {
      return { open: false, clicked: null, source: "",
               error: "exception:" + (e && e.name ? e.name : "unknown") };
    }
    if (open) return { open: true, clicked: null, source: open, candidates: 0 };
    var found;
    try {
      found = panelControl();
    } catch (e) {
      return { open: false, clicked: null, source: "",
               error: "exception:" + (e && e.name ? e.name : "unknown") };
    }
    if (!found.el) {
      return { open: false, clicked: null, source: "", candidates: found.seen };
    }
    try { found.el.scrollIntoView({ block: "center" }); } catch (e) { /* ignore */ }
    try {
      found.el.click();
    } catch (e) {
      return { open: false, clicked: null, source: "", candidates: found.seen,
               error: "exception:" + (e && e.name ? e.name : "unknown") };
    }
    return { open: false, clicked: found.text, source: "", candidates: found.seen };
  }
"""

_PROVIDER_BLOCKS: dict[str, str] = {
    "teams": _TEAMS_JS,
    "meet": _MEET_JS,
    "zoom": _ZOOM_JS,
}


def _probe_body(provider_id: str) -> str:
    """The prelude, the provider's block, and a ``probe()``/``captionRows()`` pair."""
    provider = (provider_id or "").strip().lower()
    if provider in _PROVIDER_BLOCKS:
        return _PRELUDE_JS + _PROVIDER_BLOCKS[provider] + _SHIM_JS.replace(
            "__PROVIDER__", provider
        )
    return _PRELUDE_JS + _TEAMS_JS + _MEET_JS + _ZOOM_JS + _GENERIC_SHIM_JS


def build_probe_script(provider_id: str) -> str:
    """One read of the meeting page, as a self-contained expression.

    Used by the Node DOM tests and available for a one-off diagnostic read. The
    sampler itself does not use it: a poll this coarse cannot see the signals
    that matter (see the module docstring).
    """
    return "(function () {\n" + _probe_body(provider_id) + "\n  return JSON.stringify(probe());\n})()"


def build_install_script(
    provider_id: str, run_token: str, *, captions: bool = True, tick_ms: int = TICK_MS
) -> str:
    """The resident observer for one recording.

    ``run_token`` identifies this recording. Re-running the script with the
    same token is a no-op that keeps whatever has been collected; running it
    with a different one tears down the previous observer first, so two
    meetings held back to back in the same page can never be mixed together.
    """
    observer = (
        _OBSERVER_JS.replace("__RUN__", json.dumps(str(run_token)))
        .replace("__TICK_MS__", json.dumps(int(tick_ms)))
        .replace("__HEARTBEAT_MS__", json.dumps(int(HEARTBEAT_MS)))
        .replace("__CAPTIONS__", "true" if captions else "false")
    )
    return "(function () {\n" + _probe_body(provider_id) + observer + "\n})()"


def build_drain_script(run_token: str, *, flush: bool = False) -> str:
    """Hand over everything the observer has accumulated, and clear it."""
    return (
        _DRAIN_JS.replace("__RUN__", json.dumps(str(run_token)))
        .replace("__FLUSH__", "true" if flush else "false")
        .replace("__SETTLE_MS__", json.dumps(int(CAPTION_SETTLE_MS)))
    )


def build_captions_script() -> str:
    """One click pass that can only ever switch captions on, never off."""
    return build_click_script(
        list(CAPTION_BUTTON_TEXTS), avoid_texts=CAPTION_AVOID_TEXTS
    )


def _panel_families(provider_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The panel selectors and button words for one provider.

    An unrecognised provider gets all three families in turn, exactly as
    ``_probe_body`` does with the probes: a Webex meeting is more likely to
    answer to somebody else's selectors than to none at all.
    """
    provider = (provider_id or "").strip().lower()
    if provider in ROSTER_PANEL_SELECTORS:
        return ROSTER_PANEL_SELECTORS[provider], ROSTER_BUTTON_TEXTS[provider]

    selectors: list[str] = []
    words: list[str] = []
    for known in ("teams", "meet", "zoom"):
        selectors.extend(ROSTER_PANEL_SELECTORS[known])
        for word in ROSTER_BUTTON_TEXTS[known]:
            if word.lower() not in {w.lower() for w in words}:
                words.append(word)
    return tuple(selectors), tuple(words)


def build_roster_panel_script(provider_id: str) -> str:
    """One pass that opens the participant list, and can only ever open it.

    The reply says which of the two things happened — ``open`` for a panel
    somebody already had up and ``clicked`` for one this pass opened — because
    "nothing was pressed" means two very different things and the log should
    not have to guess which.
    """
    selectors, words = _panel_families(provider_id)
    body = (
        _PANEL_JS.replace("__PANEL__", json.dumps(list(selectors)))
        .replace("__WANTED__", json.dumps(list(words)))
        .replace("__AVOID__", json.dumps(list(ROSTER_AVOID_TEXTS)))
    )
    return "(function () {\n" + _PRELUDE_JS + body + "\n  return openRoster();\n})()"


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------


@dataclass
class RosterSample:
    """Who the meeting window listed, and who it showed talking, at one moment.

    ``at`` is **seconds since this recording started**, not a wall clock and
    not the page's milliseconds. Everything the minutes feature records shares
    that one origin so the two audio tracks, the transcript and these samples
    line up with each other.
    """

    at: float
    participants: list[str] = field(default_factory=list)
    speaking: list[str] = field(default_factory=list)
    ok: bool = True
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": round(float(self.at), 3),
            "participants": list(self.participants),
            "speaking": list(self.speaking),
            "ok": bool(self.ok),
            "reason": str(self.reason or ""),
        }

    @classmethod
    def from_dict(cls, row: Any) -> "RosterSample | None":
        if not isinstance(row, dict):
            return None
        return cls(
            at=_float(row.get("at")),
            participants=_names(row.get("participants")),
            speaking=_names(row.get("speaking")),
            ok=bool(row.get("ok", True)),
            reason=str(row.get("reason") or ""),
        )


@dataclass
class CaptionLine:
    """One line the meeting's own captions wrote down, with its author.

    ``at`` is seconds since the recording started, taken from when the line
    first appeared rather than when it settled — that is when the words were
    being said.
    """

    at: float
    speaker: str = ""
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": round(float(self.at), 3),
            "speaker": str(self.speaker or ""),
            "text": str(self.text or ""),
        }

    @classmethod
    def from_dict(cls, row: Any) -> "CaptionLine | None":
        if not isinstance(row, dict):
            return None
        text = str(row.get("text") or "").strip()
        if not text:
            return None
        return cls(at=_float(row.get("at")), speaker=_name(row.get("speaker")), text=text)


def available(config: Any) -> tuple[bool, str]:
    """``(can the meeting window be read, and if not why not)``."""
    try:
        if not config.bool_("MINUTES_IDENTIFY_REMOTE"):
            return False, "Reading the meeting window is switched off."
        if config.bool_("DEV_MODE"):
            return False, (
                "“Development mode” is on, so there is no real meeting window "
                "to read. Remote speakers will not be named."
            )
        return True, ""
    except Exception:  # pragma: no cover - a broken config must not raise here
        return False, "The meeting window cannot be read at the moment."


# ---------------------------------------------------------------------------
# The sampler
# ---------------------------------------------------------------------------


class RosterSampler:
    """Watches one meeting's window for as long as it is being recorded.

    Started and stopped by ``service.py`` around a recording. It owns one
    daemon thread, which installs the in-page observer and then drains it every
    couple of seconds, writing what it finds straight to disk as it goes. No
    method here raises: the worst that can happen is a recording with fewer
    names in it.
    """

    def __init__(self, config: Any, browser: Any) -> None:
        self.config = config
        self.browser = browser

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._provider = ""
        self._dir: Path | None = None
        self._run = ""
        self._origin = 0.0
        self._reason = ""

        self._samples: list[RosterSample] = []
        self._latest: RosterSample | None = None
        self._caption_count = 0
        self._dropped = 0

        self._installed = False
        self._ever_installed = False
        self._surface_seen = False
        self._signal_seen = False
        self._absence_logged = False
        self._source = ""
        self._health: dict[str, Any] = {}

        self._sample_file: Any = None
        self._caption_file: Any = None

    # -- lifecycle -------------------------------------------------------
    @property
    def running(self) -> bool:
        with self._lock:
            thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self, provider_id: str, directory: Path) -> None:
        """Begin watching, unless the feature is off or already running."""
        try:
            self._start(provider_id, directory)
        except Exception:  # pragma: no cover - starting must never raise
            log.exception("minutes.roster_start_failed")

    def _start(self, provider_id: str, directory: Path) -> None:
        if self.running:
            return
        ok, why = available(self.config)
        if not ok:
            with self._lock:
                self._reason = why
                self._latest = RosterSample(at=0.0, ok=False, reason=why)
            log_event(log, logging.INFO, "minutes.roster_unavailable", reason=why)
            return

        with self._lock:
            self._stop.clear()
            self._provider = str(provider_id or "").strip().lower()
            self._dir = Path(directory)
            self._run = secrets.token_hex(8)
            self._origin = time.monotonic()
            self._reason = ""
            self._samples = []
            self._latest = None
            self._caption_count = 0
            self._dropped = 0
            self._installed = False
            self._ever_installed = False
            self._surface_seen = False
            self._signal_seen = False
            self._absence_logged = False
            self._source = ""
            self._health = {}
            thread = threading.Thread(
                target=self._watch, name="minutes-roster", daemon=True
            )
            self._thread = thread

        try:
            self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            log_event(
                log, logging.WARNING, "minutes.roster_dir_failed", error=str(exc)
            )

        thread.start()
        log_event(
            log, logging.INFO, "minutes.roster_started",
            provider=self._provider or "unknown",
            captions=self.config.bool_("MINUTES_READ_CAPTIONS"),
        )

    def stop(self) -> list[RosterSample]:
        """Stop watching and return what was collected. Safe at any time."""
        try:
            return self._stop_now()
        except Exception:  # pragma: no cover - stopping must never raise
            log.exception("minutes.roster_stop_failed")
            with self._lock:
                return list(self._samples)

    def _stop_now(self) -> list[RosterSample]:
        self._stop.set()
        with self._lock:
            thread = self._thread
            started = bool(self._run)
        if thread and thread.is_alive():
            thread.join(timeout=8.0)
        with self._lock:
            self._thread = None
            samples = list(self._samples)
            captions = self._caption_count
            provider = self._provider
            surface = self._surface_seen
            signal = self._signal_seen
            source = self._source
            health = dict(self._health)
            already = self._absence_logged
            self._absence_logged = True
        self._close_files()

        if not started:
            return samples

        # One line, once per meeting, and only when there was something to read
        # but nothing to hear. This is the alarm that says a vendor has shipped
        # a breaking change — months before anybody complains that the
        # transcript stopped naming people.
        if surface and not signal and not already:
            log_event(
                log, logging.WARNING, "minutes.speaker_signal_absent",
                provider=provider or "unknown", source=source or "none",
                tiles=int(health.get("tiles") or 0),
                roster=int(health.get("roster") or 0),
                captions=int(health.get("captions") or 0),
            )
        log_event(
            log, logging.INFO, "minutes.roster_stopped",
            provider=provider or "unknown", samples=len(samples),
            captions=captions, source=source or "none",
        )
        return samples

    def snapshot(self) -> RosterSample | None:
        """What the meeting window shows right now, for the web page."""
        with self._lock:
            return self._latest

    # -- the watching thread ---------------------------------------------
    def _watch(self) -> None:
        """Install the observer, then drain it until the meeting ends."""
        began = time.monotonic()
        failures = 0
        try:
            if self.config.bool_("MINUTES_TURN_ON_CAPTIONS"):
                self._press_captions()
            if self.config.bool_("MINUTES_OPEN_ROSTER"):
                self._open_participants()

            while not self._stop.is_set():
                status = self._pass()
                if status == "gone":
                    # "No meeting on screen" means the meeting has ended — stop,
                    # rather than keep asking a page that is not there. Before
                    # the observer has ever gone in it means the opposite, that
                    # the room is still working through a pre-join screen, so
                    # that case gets a short grace period instead.
                    with self._lock:
                        ever = self._ever_installed
                    if ever or (time.monotonic() - began) > INSTALL_GRACE_SECONDS:
                        log_event(
                            log, logging.INFO, "minutes.roster_page_gone",
                            installed=ever,
                        )
                        return
                    failures += 1
                elif status == "ok":
                    failures = 0
                else:
                    failures += 1

                interval = POLL_SECONDS
                if failures >= SLOWEST_AFTER_FAILURES:
                    interval = SLOWEST_POLL_SECONDS
                elif failures >= SLOW_AFTER_FAILURES:
                    interval = SLOW_POLL_SECONDS
                self._stop.wait(timeout=interval)

            with self._lock:
                ever = self._ever_installed
            if ever:
                self._pass(flush=True)
        except Exception:  # pragma: no cover - the thread must never die loudly
            log.exception("minutes.roster_thread_failed")
        finally:
            self._close_files()

    def _pass(self, *, flush: bool = False) -> str:
        """One install-or-drain. Returns ``ok``, ``quiet``, ``retry`` or ``gone``."""
        with self._lock:
            installed = self._installed
            run = self._run
            provider = self._provider
        if not run:
            return "gone"

        if not installed:
            if flush:
                # On the way out there is nothing to flush, and installing an
                # observer here would leave a timer ticking on the room's
                # television for a meeting that has just ended.
                return "gone"
            script = build_install_script(
                provider, run, captions=self.config.bool_("MINUTES_READ_CAPTIONS")
            )
            payload = self._read(script)
            if payload is None:
                return "gone"
            if not (isinstance(payload, dict) and payload.get("ok")):
                return "retry"
            with self._lock:
                self._installed = True
                self._ever_installed = True
            log_event(
                log, logging.INFO, "minutes.roster_watching",
                provider=provider or "unknown",
                state=str(payload.get("state") or "installed"),
            )

        payload = self._read(build_drain_script(run, flush=flush))
        if payload is None:
            return "gone"
        if not isinstance(payload, dict):
            return "retry"
        if not payload.get("installed"):
            # The page reloaded or the app remounted and took the observer with
            # it. Put it back on the next pass; the samples already drained are
            # safely on disk.
            with self._lock:
                self._installed = False
            return "retry"
        return self._consume(payload)

    def _read(self, script: str, *, user_gesture: bool = False) -> Any:
        """``browser.read_meeting_page`` with the last of the belt and braces.

        ``user_gesture`` stays off for every reading pass. Only the captions
        control needs one, because the page gates it behind a real interaction.
        """
        try:
            return self.browser.read_meeting_page(
                script, timeout=6.0, user_gesture=user_gesture
            )
        except Exception:  # pragma: no cover - the door is documented never to
            log.exception("minutes.roster_read_failed")
            return None

    def _consume(self, payload: dict[str, Any]) -> str:
        """File one drain's worth of samples and caption lines."""
        elapsed = max(0.0, time.monotonic() - self._origin)
        page_now = payload.get("now")

        source = str(payload.get("source") or "")
        health = payload.get("health")
        speaking_now = _names(payload.get("speaking"))
        roster_now = _names(payload.get("participants"))

        with self._lock:
            if source:
                self._source = source
            if isinstance(health, dict):
                self._health = health
            if payload.get("surface"):
                self._surface_seen = True
            if payload.get("signal") or speaking_now:
                self._signal_seen = True
            self._latest = RosterSample(
                at=round(elapsed, 3),
                participants=roster_now,
                speaking=speaking_now,
                ok=bool(payload.get("ok")),
                reason=str(payload.get("reason") or ""),
            )

        rows = payload.get("samples")
        samples: list[RosterSample] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                sample = RosterSample(
                    at=_relative_seconds(page_now, row.get("at"), elapsed),
                    participants=_names(row.get("participants")),
                    speaking=_names(row.get("speaking")),
                    ok=bool(row.get("ok", True)),
                    reason=str(row.get("reason") or ""),
                )
                samples.append(sample)
        samples.sort(key=lambda s: s.at)

        lines: list[CaptionLine] = []
        rows = payload.get("captions")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                text = str(row.get("text") or "").strip()
                if not text:
                    continue
                lines.append(
                    CaptionLine(
                        at=_relative_seconds(page_now, row.get("at"), elapsed),
                        speaker=_name(row.get("speaker")),
                        text=text[:2000],
                    )
                )
        lines.sort(key=lambda line: line.at)

        self._append(samples, lines)
        if samples or lines:
            return "ok"
        # A drain that found the meeting and had nothing new to say is a quiet
        # moment, not a fault — but it should not reset the back-off either.
        return "ok" if payload.get("ok") else "quiet"

    def _append(self, samples: list[RosterSample], lines: list[CaptionLine]) -> None:
        """Write straight through to disk, one JSON object per line.

        Streaming rather than buffering is the whole point: a meeting cut short
        by somebody pulling the plug keeps everything up to the last couple of
        seconds. Each write is flushed to the operating system, which is what
        survives this process being killed or restarted; an ``fsync`` every two
        seconds for an hour would be nearly two thousand extra writes to an SD
        card that has to last years, for the sake of the final second.
        """
        with self._lock:
            directory = self._dir
            if directory is None:
                return
            if samples:
                self._samples.extend(samples)
                if len(self._samples) > MAX_MEMORY_SAMPLES:
                    over = len(self._samples) - MAX_MEMORY_SAMPLES
                    del self._samples[:over]
                    self._dropped += over
                handle = self._sample_file
                if handle is None:
                    handle = self._sample_file = _open_append(directory / ROSTER_FILE)
                if handle is not None:
                    _write_lines(handle, [s.to_dict() for s in samples])
            if lines:
                self._caption_count += len(lines)
                handle = self._caption_file
                if handle is None:
                    handle = self._caption_file = _open_append(directory / CAPTIONS_FILE)
                if handle is not None:
                    _write_lines(handle, [line.to_dict() for line in lines])

    def _close_files(self) -> None:
        with self._lock:
            for name in ("_sample_file", "_caption_file"):
                handle = getattr(self, name, None)
                setattr(self, name, None)
                if handle is None:
                    continue
                try:
                    handle.flush()
                    handle.close()
                except OSError:  # pragma: no cover - closing must not raise
                    pass

    # -- switching captions on -------------------------------------------
    def _press_captions(self) -> None:
        """One attempt at the meeting's own captions control, and no more.

        Captions appear on the television for everyone in the room, so this
        only ever happens when an administrator has asked for it, and it
        follows the join clicker's discipline exactly: match the visible words,
        never a class name; never press anything on the deny list; and if it
        does not work, leave the meeting where a person would have left it.

        It is one pass on purpose. On Teams the control usually sits behind a
        “More” menu, so a single pass will often find nothing — and hunting
        through somebody's menus on the room's screen mid-meeting is worse than
        not having captions.
        """
        payload = self._read(build_captions_script(), user_gesture=True)
        pressed = bool(isinstance(payload, dict) and payload.get("clicked"))
        log_event(
            log, logging.INFO, "minutes.roster_captions_requested", pressed=pressed
        )

    # -- opening the participant list -------------------------------------
    def _open_participants(self) -> None:
        """One attempt at the meeting's own participant panel, and no more.

        The same discipline as the captions press, for the same reason: the
        panel appears on the television and shrinks everybody's video, so it
        happens only when an administrator has asked for it, only once, and
        only if the panel is not up already. A room that toggled a panel
        somebody else had opened would be taking it away from them.
        """
        with self._lock:
            provider = self._provider
        payload = self._read(
            build_roster_panel_script(provider), user_gesture=True
        )
        reply = payload if isinstance(payload, dict) else {}
        already = bool(reply.get("open"))
        pressed = bool(reply.get("clicked"))
        log_event(
            log,
            logging.INFO,
            "minutes.roster_panel_requested",
            pressed=pressed,
            already_open=already,
            provider=provider or "unknown",
        )


# ---------------------------------------------------------------------------
# Reading it all back
# ---------------------------------------------------------------------------


def load_samples(directory: Path) -> list[RosterSample]:
    """Every roster sample recorded for one session, oldest first."""
    out: list[RosterSample] = []
    for row in _read_lines(Path(directory) / ROSTER_FILE):
        sample = RosterSample.from_dict(row)
        if sample is not None:
            out.append(sample)
    out.sort(key=lambda s: s.at)
    return out


def load_captions(directory: Path) -> list[CaptionLine]:
    """Every caption line recorded for one session, oldest first."""
    out: list[CaptionLine] = []
    for row in _read_lines(Path(directory) / CAPTIONS_FILE):
        line = CaptionLine.from_dict(row)
        if line is not None:
            out.append(line)
    out.sort(key=lambda line: line.at)
    return out


def caption_segments(directory: Path) -> list[Segment]:
    """The meeting's own captions, as far-end transcript segments.

    The speaker is already known — the meeting app attached the name itself —
    so these arrive attributed, which is the single biggest reason captions
    beat scraping the active-speaker highlight.
    """
    try:
        lines = _settle(load_captions(directory))
        return _segments(lines)
    except Exception:  # pragma: no cover - a bad file must not stop a meeting
        log.exception("minutes.caption_segments_failed")
        return []


def _settle(lines: list[CaptionLine]) -> list[CaptionLine]:
    """Fold each sentence's interim drafts back into the finished sentence.

    These interfaces rewrite a caption line as the words are recognised, so the
    same sentence arrives as "so", "so the", "so the plan is". The observer
    waits for a line to stop changing before handing it over, which catches
    most of it; this catches the rest — a line revised after it settled, or a
    file written by an older version. Consecutive lines from one speaker where
    one is a prefix of the other are one sentence, and the longest reading
    wins.
    """
    out: list[CaptionLine] = []
    seen_at = 0.0
    for line in lines:
        text = re.sub(r"\s+", " ", str(line.text or "")).strip()
        if not text:
            continue
        speaker = _name(line.speaker)
        at = _float(line.at)
        # Close together in time as well as in wording: a draft is rewritten
        # within a second or two, so two lines a minute apart where one happens
        # to begin with the other are two different things somebody said.
        near = out and (at - seen_at) <= CAPTION_MERGE_GAP_SECONDS
        if near and out[-1].speaker == speaker and _extends(out[-1].text, text):
            if len(text) > len(out[-1].text):
                out[-1].text = text
            seen_at = at
            continue
        out.append(CaptionLine(at=at, speaker=speaker, text=text))
        seen_at = at
    return out


def _extends(before: str, after: str) -> bool:
    """Is one of these the same sentence as the other, a few words further on?

    A plain prefix test, with no minimum length: a first draft is very often
    one or two characters ("so", "I"), and a length floor here would leave
    exactly the half-sentences this is meant to remove. It is safe because the
    caller has already established that the two lines are from the same speaker
    and moments apart.
    """
    a, b = before.casefold(), after.casefold()
    if a == b:
        return True
    if not a or not b:
        return False
    return b.startswith(a) or a.startswith(b)


def _segments(lines: list[CaptionLine]) -> list[Segment]:
    """Group settled caption lines into one segment per speaking turn."""
    out: list[Segment] = []
    group: list[CaptionLine] = []

    def flush() -> None:
        if not group:
            return
        start = group[0].at
        end = group[-1].at + _spoken_seconds(group[-1].text)
        out.append(
            Segment(
                start=round(max(0.0, start), 3),
                end=round(max(start + 0.25, end), 3),
                text=" ".join(line.text for line in group).strip(),
                track=TRACK_FAR_END,
                speaker=group[0].speaker,
                source=SOURCE_ROSTER,
                confidence=CAPTION_CONFIDENCE,
            )
        )

    for line in lines:
        if group and (
            line.speaker != group[0].speaker
            or line.at - group[-1].at > CAPTION_MERGE_GAP_SECONDS
            or line.at - group[0].at > CAPTION_MAX_SEGMENT_SECONDS
        ):
            flush()
            group = []
        group.append(line)
    flush()

    # A guessed end must never run over the next person's start, or every turn
    # would overlap the one after it and the attribution pass would have to
    # arbitrate between two speakers who never actually overlapped.
    for first, second in zip(out, out[1:]):
        if first.end > second.start:
            first.end = round(max(first.start + 0.25, second.start), 3)
    return out


def _spoken_seconds(text: str) -> float:
    """Roughly how long a caption line took to say, at ordinary meeting pace."""
    words = len(str(text or "").split())
    return min(CAPTION_MAX_SECONDS, max(CAPTION_MIN_SECONDS, words * CAPTION_SECONDS_PER_WORD))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _relative_seconds(page_now: Any, page_at: Any, elapsed: float) -> float:
    """Turn the page's ``Date.now()`` stamp into seconds since recording began.

    This is the seam where the two halves of the feature meet, and the one
    place a mistake is silent rather than loud. The page stamps every sample in
    milliseconds since 1970; everything downstream — ``attribute.py``, the
    transcript, the audio tracks — counts seconds from the start of the
    recording. Handing a raw ``Date.now()`` straight through produces speaking
    spans fifty thousand years long that overlap every segment in the meeting,
    and nothing anywhere would complain.

    The two clocks are joined at the drain rather than once at the start: how
    long ago the page took the sample, subtracted from how long this recording
    has been running. A clock correction mid-meeting can then shift at most one
    drain's worth of samples, instead of every sample after it.
    """
    age = 0.0
    try:
        if page_now is not None and page_at is not None:
            age = (float(page_now) - float(page_at)) / 1000.0
    except (TypeError, ValueError):
        age = 0.0
    if age < 0.0 or age > MAX_SAMPLE_AGE_SECONDS:
        age = 0.0
    return round(max(0.0, elapsed - age), 3)


def _open_append(path: Path) -> Any:
    """Open ``path`` for appending, owner-readable only. None if it cannot be."""
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError as exc:
        log_event(
            log, logging.WARNING, "minutes.roster_write_failed", error=str(exc)
        )
        return None
    try:
        os.fchmod(fd, 0o600)
    except OSError:  # pragma: no cover - a filesystem without modes
        pass
    try:
        return os.fdopen(fd, "a", encoding="utf-8")
    except OSError:  # pragma: no cover
        try:
            os.close(fd)
        except OSError:
            pass
        return None


def _write_lines(handle: Any, rows: list[dict[str, Any]]) -> None:
    try:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
        handle.flush()
    except (OSError, TypeError, ValueError) as exc:
        log_event(log, logging.WARNING, "minutes.roster_write_failed", error=str(exc))


def _read_lines(path: Path) -> Iterator[dict[str, Any]]:
    """Every readable JSON object in a ``.jsonl`` file.

    A line that will not parse is skipped rather than fatal. That is the whole
    reason for the format: a meeting interrupted by a power cut leaves a final
    half-written line, and losing that one line should not lose the meeting.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row
    except FileNotFoundError:
        return
    except OSError as exc:
        log_event(log, logging.WARNING, "minutes.roster_read_file_failed", error=str(exc))
        return


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _name(value: Any) -> str:
    """A name, or nothing at all. Never a diagnostic pretending to be one.

    An unknown speaker stays unknown. A confidently wrong name is far worse
    than a blank one: it puts words in a named person's mouth.
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()[:120]
    if not text or _NOT_A_NAME.match(text):
        return ""
    return text


def _names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value[:200]:
        name = _name(item)
        if name and name not in out:
            out.append(name)
    return out
