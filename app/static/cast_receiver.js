/* ==========================================================================
   The receiving half of PC screen sharing, running on the TV.

   It lives inside the dashboard page rather than a page of its own, and that is
   the whole trick: the dashboard is already open on the TV, so an incoming
   screen appears in about a second with no navigation, no second Chromium
   window, and nothing to go wrong if the backend happens to be restarting.

   The Pi decodes the stream and nothing else — no relaying, no re-encoding —
   which is what makes this affordable on a Raspberry Pi 4.

   Recovery matters here. The kiosk page can reload at any moment (a drift
   correction, a health restart, somebody power-cycling the TV), and when it
   does the connection it was holding is gone while the laptop still thinks it
   is sharing. So a fresh page that finds a session already running asks the
   sender to offer again, and the picture comes back by itself.
   ========================================================================== */

(function () {
  "use strict";

  var POLL_IDLE_MS = 1000;      // nothing happening: watching for an offer
  var POLL_ACTIVE_MS = 300;     // mid-handshake: candidates need to flow now
  var POLL_LIVE_MS = 2000;      // playing: nothing to exchange, just a heartbeat

  //: Never ask the sender to start over more than this often, so a page that
  //: cannot play the stream cannot spin the laptop in circles.
  var RENEGOTIATE_MIN_MS = 6000;

  var peer = null;
  var sessionId = "";           // the session `peer` belongs to
  var onScreen = false;         // a picture is actually being drawn
  var pollTimer = null;
  var lastRenegotiate = 0;
  var pendingCandidates = [];
  var remoteReady = false;

  var csrf = document.body.getAttribute("data-csrf") || "";

  function $(id) { return document.getElementById(id); }

  function show(el, visible) {
    if (!el) return;
    if (visible) el.removeAttribute("hidden");
    else el.setAttribute("hidden", "hidden");
  }

  function playing(isPlaying) {
    // The dashboard's own "Screen sharing" overlay stands down while there is
    // a real picture to show; styles.css keys off this class.
    onScreen = !!isPlaying;
    document.body.classList.toggle("cast-playing", onScreen);
    show($("cast-stage"), onScreen);
  }

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Room-Token": csrf },
      body: JSON.stringify(body || {}),
      cache: "no-store"
    }).then(function (response) {
      return response.json().catch(function () { return { ok: false }; });
    });
  }

  // ------------------------------------------------------------------- poll

  function schedule(delay) {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(poll, delay);
  }

  function poll() {
    fetch("/api/cast/receiver", { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (result) {
        var active = handle(result || {});
        schedule(active ? (onScreen ? POLL_LIVE_MS : POLL_ACTIVE_MS) : POLL_IDLE_MS);
      })
      .catch(function () {
        // The backend is restarting. Keep the picture up — the video is
        // peer-to-peer and does not need the backend to keep flowing.
        schedule(POLL_IDLE_MS);
      });
  }

  function handle(result) {
    var incoming = result.session || "";

    if (!incoming) {
      if (peer || sessionId) teardown();
      return false;
    }

    if (incoming !== sessionId && peer) {
      // A different laptop took over.
      teardown();
    }

    var messages = result.messages || [];
    for (var i = 0; i < messages.length; i++) {
      apply(incoming, messages[i]);
    }

    if (!peer && !messages.length) {
      // A session is running but this page is not part of it: either it has
      // just loaded into an existing share, or the offer went to a page that
      // has since gone away. Ask for a new one.
      renegotiate(incoming);
    }
    return true;
  }

  function apply(incoming, message) {
    if (!message) return;
    if (message.type === "offer") {
      accept(incoming, message.sdp);
    } else if (message.type === "candidate") {
      if (!remoteReady) { pendingCandidates.push(message.candidate); return; }
      addCandidate(message.candidate);
    }
  }

  // -------------------------------------------------------------- answering

  function accept(incoming, offer) {
    teardown();
    sessionId = incoming;
    peer = new window.RTCPeerConnection({ iceServers: [] });

    peer.addEventListener("icecandidate", function (event) {
      if (!event.candidate) return;
      post("/api/cast/receiver/candidate", {
        session: sessionId,
        candidate: event.candidate.toJSON ? event.candidate.toJSON() : event.candidate
      });
    });

    peer.addEventListener("track", function (event) {
      var video = $("cast-video");
      if (!video) return;
      var mine = sessionId;
      video.srcObject = event.streams[0];

      // Go full-screen when frames actually arrive, not when the track is
      // announced: `track` fires as the answer is built, a second before any
      // picture exists, and filling the TV with black then is worse than
      // leaving the dashboard's "Screen sharing" notice up for that second.
      video.addEventListener("playing", function () {
        if (sessionId === mine) playing(true);
      }, { once: true });

      var started = video.play();
      if (started && started.catch) {
        started.catch(function () {
          // Autoplay refused. The element is muted, so this should not happen;
          // if it somehow does, do not leave the room on the dashboard while
          // somebody's screen is being sent to it — hand the session back.
          if (sessionId === mine) report("the room screen could not start playback");
        });
      }
    });

    peer.addEventListener("connectionstatechange", function () {
      if (!peer) return;
      if (peer.connectionState === "failed") {
        report("the connection to the sharing device failed");
      } else if (peer.connectionState === "closed") {
        playing(false);
      }
    });

    peer.setRemoteDescription(new window.RTCSessionDescription(offer))
      .then(function () {
        remoteReady = true;
        drainCandidates();
        return peer.createAnswer();
      })
      .then(function (answer) { return peer.setLocalDescription(answer); })
      .then(function () {
        return post("/api/cast/receiver/answer", {
          session: sessionId,
          sdp: { type: peer.localDescription.type, sdp: peer.localDescription.sdp }
        });
      })
      .catch(function () {
        report("the room screen could not accept the stream");
      });
  }

  function drainCandidates() {
    var waiting = pendingCandidates;
    pendingCandidates = [];
    waiting.forEach(addCandidate);
  }

  function addCandidate(candidate) {
    if (!peer || !candidate) return;
    peer.addIceCandidate(candidate).catch(function () {
      // Normal: ICE offers several and only needs one to work.
    });
  }

  function renegotiate(incoming) {
    var now = Date.now();
    if (now - lastRenegotiate < RENEGOTIATE_MIN_MS) return;
    lastRenegotiate = now;
    post("/api/cast/receiver/renegotiate", { session: incoming });
  }

  function report(reason) {
    var failed = sessionId;
    teardown();
    if (failed) post("/api/cast/receiver/failed", { session: failed, reason: reason });
  }

  function teardown() {
    sessionId = "";
    remoteReady = false;
    pendingCandidates = [];
    if (peer) {
      try { peer.close(); } catch (error) { /* already closed */ }
      peer = null;
    }
    var video = $("cast-video");
    if (video) {
      video.pause();
      video.srcObject = null;
    }
    playing(false);
  }

  // -------------------------------------------------------------------- wire

  function init() {
    if (!$("cast-stage")) return;                       // nothing to draw into
    if (typeof window.RTCPeerConnection !== "function") return;  // very old Chromium
    if (document.body.getAttribute("data-cast") !== "on") return;  // switched off
    poll();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
