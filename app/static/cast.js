/* ==========================================================================
   Screen sharing from a laptop (the page served by the HTTPS listener).

   The laptop captures its screen and sends it straight to the Chromium on the
   TV over WebRTC. The room's backend only passes the introductions along, so
   the video never touches the Raspberry Pi's CPU.

   Two decisions worth knowing about:

   * `iceServers: []` — no STUN, no TURN, no internet. Both ends are on the
     same network, so host candidates are all that is needed, and a room
     appliance should not depend on a Google server to put a slide on a TV.

   * The room screen is the answering side, so it is the one that gets a real
     address to aim at. Chromium hides a page's local addresses behind mDNS
     names until it has been granted a device permission, and the TV has been
     granted none — but this side has just been given the screen, so its
     candidates are real. The connectivity check that succeeds is the one the
     TV sends here, and ICE learns the rest by itself.

   Deliberately plain: no framework, no build step, ES5 syntax so an older
   browser in a meeting room does not fall over on a fat arrow.
   ========================================================================== */

(function () {
  "use strict";

  // Fast while connecting, slow once it is up (then it is just a heartbeat
  // telling the room this laptop is still here).
  var POLL_CONNECTING_MS = 250;
  var POLL_LIVE_MS = 2000;

  // How long to wait for the TV before admitting something is wrong.
  var CONNECT_GIVE_UP_MS = 25000;

  var session = "";
  var peer = null;
  var stream = null;
  var pollTimer = null;
  var giveUpTimer = null;
  var live = false;
  var starting = false;
  var pendingCandidates = [];
  var remoteReady = false;

  function $(id) { return document.getElementById(id); }

  function show(el, visible) {
    if (!el) return;
    if (visible) el.removeAttribute("hidden");
    else el.setAttribute("hidden", "hidden");
  }

  function say(text, kind) {
    var el = $("message");
    if (!el) return;
    el.textContent = text || "";
    el.className = "message" + (kind ? " " + kind : "");
    show(el, !!text);
  }

  function label(text) {
    var el = $("share-label");
    if (el) el.textContent = text;
  }

  function busy(isBusy) {
    var button = $("share-button");
    if (button) button.disabled = isBusy;
  }

  // ------------------------------------------------------------------ server

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
      cache: "no-store"
    }).then(function (response) {
      return response.json().catch(function () {
        return { ok: false, error: "The room did not answer properly." };
      });
    });
  }

  function get(path) {
    return fetch(path, { cache: "no-store" }).then(function (response) {
      return response.json().catch(function () {
        return { ok: false, error: "The room did not answer properly." };
      });
    });
  }

  // ------------------------------------------------------- capability checks

  function unsupportedReason() {
    if (!window.isSecureContext) {
      // Should be unreachable: this page is only served over HTTPS. Worth
      // naming anyway, because the symptom is otherwise inexplicable.
      return "This page was not opened securely, so the browser will not let it "
        + "read your screen. Open it again from the room's address.";
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
      return "This browser cannot share a screen. Chrome, Edge and Firefox can; "
        + "on a Mac, use Screen Mirroring instead.";
    }
    if (typeof window.RTCPeerConnection !== "function") {
      return "This browser is missing the connection support this needs. Try "
        + "Chrome, Edge or Firefox.";
    }
    return "";
  }

  // --------------------------------------------------------------- lifecycle

  function describeThisDevice() {
    // Only ever shown on the room's own screen ("Shared from …"), so it wants
    // to be recognisable to the person sharing, not precise.
    var agent = navigator.userAgent || "";
    var platform = "Laptop";
    if (/Windows/i.test(agent)) platform = "Windows PC";
    else if (/Macintosh|Mac OS X/i.test(agent)) platform = "Mac";
    else if (/CrOS/i.test(agent)) platform = "Chromebook";
    else if (/Android/i.test(agent)) platform = "Android";
    else if (/Linux/i.test(agent)) platform = "Linux PC";

    var browser = "";
    if (/Edg\//.test(agent)) browser = "Edge";
    else if (/OPR\//.test(agent)) browser = "Opera";
    else if (/Firefox\//.test(agent)) browser = "Firefox";
    else if (/Chrome\//.test(agent)) browser = "Chrome";
    else if (/Safari\//.test(agent)) browser = "Safari";

    return browser ? platform + " (" + browser + ")" : platform;
  }

  function startSharing() {
    if (starting || live) return;

    var blocked = unsupportedReason();
    if (blocked) { say(blocked, "error"); return; }

    starting = true;
    busy(true);
    say("");
    label("Waiting for you to choose…");

    // The picker has to be opened from inside the click, before any `await`:
    // browsers require the user gesture to still be in progress, and a
    // round-trip to the room would spend it.
    var wanted = navigator.mediaDevices.getDisplayMedia({
      video: {
        // A cap rather than a demand. The Pi has to decode this, and a 4K
        // stream buys nothing on a meeting-room TV while costing a great deal
        // of latency.
        frameRate: { ideal: 30, max: 30 },
        width: { max: 1920 },
        height: { max: 1080 }
      },
      // Room audio belongs to the conference bar, and a laptop that shares its
      // own audio into a call it is also in creates a feedback loop.
      audio: false
    });

    wanted.then(function (media) {
      stream = media;
      label("Connecting to the room…");
      // The browser's own "Stop sharing" bar ends the track directly.
      media.getVideoTracks().forEach(function (track) {
        track.addEventListener("ended", function () {
          finish("You stopped sharing.");
        });
      });
      return openSession();
    }).catch(function (error) {
      starting = false;
      busy(false);
      label("Share this screen");
      stopStream();
      say(pickerError(error), "error");
    });
  }

  function pickerError(error) {
    var name = (error && error.name) || "";
    if (name === "NotAllowedError") {
      // Either the picker was dismissed, or the OS refuses to allow capture at
      // all — on macOS that is a system permission, which is worth naming.
      return "Nothing was shared. Press the button and choose a screen or "
        + "window. If no picker appeared, your computer may be blocking screen "
        + "recording for this browser.";
    }
    if (name === "NotFoundError") return "No screen or window was available to share.";
    if (name === "NotReadableError") {
      return "Your computer would not let the browser read that screen. Try "
        + "picking a single window instead.";
    }
    return "That screen could not be captured" + (error && error.message ? ": " + error.message : ".");
  }

  function openSession() {
    var pin = ($("pin-input") && $("pin-input").value) || "";
    return post("/api/cast/start", { pin: pin, label: describeThisDevice() })
      .then(function (result) {
        if (!result.ok) {
          if (result.needs_pin) {
            show($("pin-row"), true);
            var input = $("pin-input");
            if (input) input.focus();
          }
          throw new Error(result.error || "The room refused the connection.");
        }
        session = result.session;
        show($("pin-row"), false);
        return connect();
      })
      .catch(function (error) {
        starting = false;
        busy(false);
        label("Share this screen");
        stopStream();
        say(error.message || "The room could not be reached.", "error");
      });
  }

  function connect() {
    peer = new window.RTCPeerConnection({ iceServers: [] });

    peer.addEventListener("icecandidate", function (event) {
      if (!event.candidate || !session) return;
      post("/api/cast/candidate", {
        session: session,
        candidate: event.candidate.toJSON ? event.candidate.toJSON() : event.candidate
      });
    });

    peer.addEventListener("connectionstatechange", function () {
      if (!peer) return;
      if (peer.connectionState === "connected") {
        becomeLive();
      } else if (peer.connectionState === "failed") {
        finish("The room could not be reached over this network. It may keep "
          + "devices separated — try the room's own Wi-Fi.", true);
      } else if (peer.connectionState === "disconnected" && live) {
        say("The connection dropped. Trying to recover…", "warn");
      }
    });

    stream.getTracks().forEach(function (track) {
      peer.addTrack(track, stream);
    });

    // A shared screen is mostly still, then changes all at once. Telling the
    // encoder that keeps text sharp instead of smearing a slide transition.
    peer.getSenders().forEach(function (sender) {
      if (!sender.track || sender.track.kind !== "video") return;
      var parameters = sender.getParameters();
      if (!parameters.encodings || !parameters.encodings.length) {
        parameters.encodings = [{}];
      }
      parameters.encodings[0].maxBitrate = 4000000;
      parameters.degradationPreference = "maintain-resolution";
      try {
        sender.setParameters(parameters);
      } catch (error) {
        // Older browsers reject some of these. Not worth failing over: the
        // defaults still work, they just look softer on a slide change.
      }
    });

    var video = stream.getVideoTracks()[0];
    if (video && typeof video.contentHint !== "undefined") {
      video.contentHint = "detail";
    }

    return peer.createOffer()
      .then(function (offer) { return peer.setLocalDescription(offer); })
      .then(function () {
        return post("/api/cast/offer", {
          session: session,
          sdp: { type: peer.localDescription.type, sdp: peer.localDescription.sdp }
        });
      })
      .then(function (result) {
        if (!result.ok) throw new Error(result.error || "The room refused the offer.");
        starting = false;
        label("Sharing…");
        say("Waiting for the room screen…");
        schedulePoll(POLL_CONNECTING_MS);
        giveUpTimer = window.setTimeout(function () {
          if (live) return;
          finish("The room screen never picked this up. It may be showing a "
            + "meeting, or the room software may need a restart.", true);
        }, CONNECT_GIVE_UP_MS);
      })
      .catch(function (error) {
        starting = false;
        finish(error.message || "The connection could not be set up.", true);
      });
  }

  function becomeLive() {
    if (live) return;
    live = true;
    window.clearTimeout(giveUpTimer);
    giveUpTimer = null;
    busy(false);
    say("");
    show($("live"), true);
    show($("share-button"), false);
    show($("stage-note"), false);

    var preview = $("preview");
    if (preview && stream) {
      preview.srcObject = stream;
      var playing = preview.play();
      if (playing && playing.catch) playing.catch(function () { /* a paused preview is fine */ });
    }
    schedulePoll(POLL_LIVE_MS);
  }

  // ------------------------------------------------------------- signalling

  function schedulePoll(delay) {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(poll, delay);
  }

  function poll() {
    if (!session) return;
    get("/api/cast/poll?session=" + encodeURIComponent(session))
      .then(function (result) {
        if (!result.ok) {
          if (result.ended) {
            finish(result.error || "This sharing session ended.");
            return;
          }
          schedulePoll(POLL_LIVE_MS);
          return;
        }
        (result.messages || []).forEach(handleMessage);
        if (session) schedulePoll(live ? POLL_LIVE_MS : POLL_CONNECTING_MS);
      })
      .catch(function () {
        // A dropped poll is not a dropped share: WebRTC carries the video by
        // itself. Keep asking.
        if (session) schedulePoll(POLL_LIVE_MS);
      });
  }

  function handleMessage(message) {
    if (!message || !peer) return;
    if (message.type === "answer") {
      peer.setRemoteDescription(new window.RTCSessionDescription(message.sdp))
        .then(function () {
          remoteReady = true;
          drainCandidates();
        })
        .catch(function (error) {
          finish("The room's reply could not be used: " + (error.message || "unknown"), true);
        });
    } else if (message.type === "candidate") {
      if (!remoteReady) {
        // Candidates can arrive before the answer they belong to; adding one
        // then throws.
        pendingCandidates.push(message.candidate);
        return;
      }
      addCandidate(message.candidate);
    } else if (message.type === "renegotiate") {
      reoffer();
    }
    // Nothing signals the end through this channel: a session that has ended
    // no longer exists, so the poll itself comes back with `ended` and the
    // reason. See _ended() in cast_web.py.
  }

  function reoffer() {
    // The room screen reloaded and lost its half of the connection. The capture
    // is still running here, so rebuild and offer again: the picture comes back
    // on its own and nobody has to walk over and press Share a second time.
    if (!session || !stream) return;
    if (peer) {
      try { peer.close(); } catch (error) { /* already closed */ }
      peer = null;
    }
    live = false;
    remoteReady = false;
    pendingCandidates = [];
    window.clearTimeout(giveUpTimer);
    giveUpTimer = null;
    say("Reconnecting to the room screen…", "warn");
    connect();
  }

  function drainCandidates() {
    var waiting = pendingCandidates;
    pendingCandidates = [];
    waiting.forEach(addCandidate);
  }

  function addCandidate(candidate) {
    if (!peer || !candidate) return;
    peer.addIceCandidate(candidate).catch(function () {
      // One unusable candidate is normal; ICE tries the others.
    });
  }

  // ------------------------------------------------------------------ ending

  function stopStream() {
    if (!stream) return;
    stream.getTracks().forEach(function (track) {
      try { track.stop(); } catch (error) { /* already gone */ }
    });
    stream = null;
  }

  function finish(reason, isError) {
    var had = session;
    session = "";
    live = false;
    starting = false;
    remoteReady = false;
    pendingCandidates = [];
    window.clearTimeout(pollTimer);
    window.clearTimeout(giveUpTimer);
    pollTimer = null;
    giveUpTimer = null;

    if (peer) {
      try { peer.close(); } catch (error) { /* already closed */ }
      peer = null;
    }
    stopStream();

    var preview = $("preview");
    if (preview) preview.srcObject = null;

    show($("live"), false);
    show($("share-button"), true);
    show($("stage-note"), true);
    busy(false);
    label("Share this screen");
    say(reason || "", isError ? "error" : "");

    if (had) {
      // Best effort: tells the room to stop showing this straight away rather
      // than waiting for the session to time out.
      post("/api/cast/stop", { session: had });
    }
  }

  // -------------------------------------------------------------------- wire

  function init() {
    var button = $("share-button");
    if (!button) return;  // sharing is switched off; the page says so

    button.addEventListener("click", startSharing);

    var stop = $("stop-button");
    if (stop) {
      stop.addEventListener("click", function () { finish("You stopped sharing."); });
    }

    var input = $("pin-input");
    if (input) {
      input.addEventListener("keydown", function (event) {
        if (event.key === "Enter") startSharing();
      });
    }

    if (document.body.getAttribute("data-pin-required") === "yes") {
      show($("pin-row"), true);
    }

    var blocked = unsupportedReason();
    if (blocked) {
      say(blocked, "error");
      button.disabled = true;
    }

    // Closing the tab should free the room immediately rather than after the
    // timeout. `pagehide` fires where `beforeunload` is unreliable on mobile.
    window.addEventListener("pagehide", function () {
      if (!session) return;
      var body = JSON.stringify({ session: session });
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/api/cast/stop", new Blob([body], { type: "application/json" }));
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
