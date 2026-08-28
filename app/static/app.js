/* ==========================================================================
   Room dashboard.

   Deliberately plain: no framework, no build step, no dependencies. It polls
   /api/state, renders, and keeps the clock ticking locally so the time never
   freezes even if the backend goes away.

   Reliability rules followed throughout:
   * A failed poll never blanks the screen — the last good data stays up and a
     banner explains the situation.
   * The clock is driven by the browser, corrected by the server's time, so it
     is right even if the Pi's timezone differs from the browser's.
   * Polling backs off when requests fail and recovers on its own.
   ========================================================================== */

(function () {
  "use strict";

  var POLL_MS = 5000;
  var POLL_MS_MAX = 30000;
  var CLOCK_MS = 1000;

  var state = null;               // last successful /api/state payload
  var pollDelay = POLL_MS;
  var consecutiveFailures = 0;
  var serverOffsetMs = 0;         // server time minus browser time
  var joining = false;
  var toastTimer = null;

  // Background slideshow. Slides are images or videos; see the slideshow
  // section below for what each field is doing.
  var slideshow = {
    media: [],        // the slides being played, broken ones removed
    raw: "",          // signature of the list the backend last sent
    broken: {},       // urls that failed, so they are not retried every poll
    index: -1,
    layer: 0,         // which still layer is on top
    timer: null,      // the dwell timer, images only
    seconds: 45,
    shuffle: false,
    sound: false,
    loading: false,   // an image is being fetched
    loadingAt: 0,
    playing: false,   // a video slide is on screen
    forcedMute: false,  // this clip only plays with the sound off
    guard: null,      // watchdog for the playing video
    release: null,    // pending release of the video element
    metadata: false,  // the playing video reported its duration
    startedAt: 0,
    lastTime: -1,
    lastMoved: 0
  };

  // The QR codes: one small one in the corner, one large one on the first-run
  // overlay. Each remembers the src it is showing and any src that would not
  // load, so a missing image is not requested again every poll.
  var cornerQr = { src: "", failed: "" };
  var setupQr = { src: "", failed: "" };
  var badgeShown = false;  // while it is up, the "Control panel:" hint stands down
  var lastRemote = "";     // the remote-control event already announced

  var csrf = document.body.getAttribute("data-csrf") || "";

  // ---------------------------------------------------------------- helpers

  function $(id) { return document.getElementById(id); }

  function setText(id, text) {
    var el = $(id);
    if (el && el.textContent !== text) el.textContent = text;
  }

  function show(el, visible) {
    if (!el) return;
    if (visible) el.removeAttribute("hidden");
    else el.setAttribute("hidden", "hidden");
  }

  function now() { return new Date(Date.now() + serverOffsetMs); }

  function pad(value) { return value < 10 ? "0" + value : String(value); }

  function formatClock(date, use24) {
    var hours = date.getHours();
    var minutes = pad(date.getMinutes());
    if (use24) return pad(hours) + ":" + minutes;
    var meridiem = hours >= 12 ? "PM" : "AM";
    var display = hours % 12;
    if (display === 0) display = 12;
    return display + ":" + minutes +
      '<span class="meridiem">' + meridiem + "</span>";
  }

  function formatTime(date, use24) {
    var hours = date.getHours();
    var minutes = pad(date.getMinutes());
    if (use24) return pad(hours) + ":" + minutes;
    var meridiem = hours >= 12 ? "pm" : "am";
    var display = hours % 12;
    if (display === 0) display = 12;
    return display + ":" + minutes + " " + meridiem;
  }

  function formatRange(startIso, endIso, use24) {
    var start = new Date(startIso);
    var end = new Date(endIso);
    if (isNaN(start) || isNaN(end)) return "";
    return formatTime(start, use24) + " – " + formatTime(end, use24);
  }

  function formatDate(date) {
    try {
      return date.toLocaleDateString(undefined, {
        weekday: "long", day: "numeric", month: "long"
      });
    } catch (e) {
      return date.toDateString();
    }
  }

  function minutesUntil(iso) {
    var target = new Date(iso);
    if (isNaN(target)) return null;
    return (target.getTime() - now().getTime()) / 60000;
  }

  function describeCountdown(meeting) {
    if (!meeting) return "";
    var mins = minutesUntil(meeting.start);
    if (mins === null) return "";
    if (mins <= 0) {
      var left = minutesUntil(meeting.end);
      if (left !== null && left > 0) return "In progress · " + Math.ceil(left) + " min left";
      return "In progress";
    }
    if (mins < 1) return "Starting now";
    if (mins < 60) return "Starts in " + Math.ceil(mins) + " min";
    var hours = Math.floor(mins / 60);
    var rest = Math.round(mins % 60);
    return "Starts in " + hours + " h" + (rest ? " " + rest + " min" : "");
  }

  function toast(message, isError) {
    var el = $("toast");
    if (!el) return;
    el.textContent = message;
    el.className = "toast" + (isError ? " error" : "");
    show(el, true);
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { show(el, false); }, 6000);
  }

  // ------------------------------------------------------------------ clock

  function tickClock() {
    var use24 = state ? !!state.time_format_24h : false;
    var current = now();
    var clock = $("clock");
    if (clock) {
      var markup = formatClock(current, use24);
      if (clock.innerHTML !== markup) clock.innerHTML = markup;
    }
    setText("date", formatDate(current));

    // Keep the countdown fresh between polls.
    if (state && state.next) {
      setText("next-countdown", describeCountdown(state.next));
    }
  }

  // -------------------------------------------------------------- rendering

  function providerClass(provider) {
    var known = { teams: 1, meet: 1, zoom: 1, webex: 1 };
    return known[provider] ? provider : "other";
  }

  function renderAvailability(payload) {
    var el = $("availability");
    if (!el) return;
    var room = payload.room || {};
    var text, cls;

    if (payload.mode === "screen-sharing") {
      text = "Screen sharing"; cls = "soon";
    } else if (!room.available) {
      text = "In use"; cls = "busy";
      if (room.busy_until) {
        var until = new Date(room.busy_until);
        if (!isNaN(until)) text = "In use until " + formatTime(until, payload.time_format_24h);
      }
    } else {
      var next = payload.next;
      var mins = next ? minutesUntil(next.start) : null;
      if (mins !== null && mins < 1.5) {
        // Rounding down here would say "Free for 0 min".
        text = "Meeting starting"; cls = "soon";
      } else if (mins !== null && mins <= 15) {
        text = "Free for " + Math.round(mins) + " min"; cls = "soon";
      } else {
        text = "Available"; cls = "free";
      }
    }
    el.className = "availability " + cls;
    setText("availability-text", text);
  }

  function renderNext(payload) {
    var next = payload.next;
    var detail = $("next-detail");
    var empty = $("next-empty");
    var current = payload.current;

    setText("next-label", current ? "Now" : "Next meeting");

    if (!next) {
      show(detail, false);
      show(empty, true);
      var calendarOff = payload.calendar && payload.calendar.source === "none";
      setText("next-empty-text", calendarOff ? "No calendar connected" : "No more meetings today");
      setText("next-empty-sub", calendarOff ? "Share a screen any time" : "The room is free");
      return;
    }

    show(empty, false);
    show(detail, true);
    setText("next-title", next.title || "Meeting");
    setText("next-time", formatRange(next.start, next.end, payload.time_format_24h));
    setText("next-countdown", describeCountdown(next));

    var badge = $("next-provider-badge");
    var providerRow = $("next-provider");
    if (next.provider) {
      badge.className = "provider-badge " + providerClass(next.provider);
      setText("next-provider-name", next.provider_name || "Online meeting");
      show(providerRow, true);
    } else if (next.location) {
      badge.className = "provider-badge other";
      setText("next-provider-name", next.location);
      show(providerRow, true);
    } else {
      show(providerRow, false);
    }

    var button = $("join-button");
    var hint = $("join-hint");
    if (!button) return;

    if (payload.active_meeting) {
      button.querySelector(".join-label").textContent = "Return to room";
      button.disabled = false;
      button.dataset.action = "leave";
      hint.textContent = "The meeting is on screen";
    } else if (next.has_link) {
      button.querySelector(".join-label").textContent = "Join";
      button.disabled = false;
      button.dataset.action = "join";
      button.dataset.meetingId = next.id || "";
      hint.textContent = "";
    } else {
      button.querySelector(".join-label").textContent = "No meeting link";
      button.disabled = true;
      button.dataset.action = "";
      hint.textContent = next.location ? next.location : "This meeting has no online link";
    }
    if (joining) button.classList.add("busy"); else button.classList.remove("busy");
  }

  function renderUpcoming(payload) {
    var list = $("upcoming-list");
    var emptyMessage = $("upcoming-empty");
    if (!list) return;

    // The card headline already shows the first item, so list the ones after it
    // unless a meeting is currently running (then all upcoming are "later").
    var items = (payload.upcoming || []).slice();
    if (!payload.current && items.length) items = items.slice(1);

    if (!items.length) {
      list.innerHTML = "";
      show(emptyMessage, true);
      return;
    }
    show(emptyMessage, false);

    var html = "";
    for (var i = 0; i < items.length; i++) {
      var meeting = items[i];
      var start = new Date(meeting.start);
      var time = isNaN(start) ? "" : formatTime(start, payload.time_format_24h);
      var meta = meeting.provider
        ? '<span class="provider-badge ' + providerClass(meeting.provider) + '"></span>' +
          escapeHtml(meeting.provider_name || "")
        : escapeHtml(meeting.location || "No online link");
      html +=
        '<li class="upcoming-item">' +
          '<span class="upcoming-time">' + escapeHtml(time) + "</span>" +
          '<span class="upcoming-body">' +
            '<span class="upcoming-title">' + escapeHtml(meeting.title || "Meeting") + "</span>" +
            '<span class="upcoming-meta">' + meta + "</span>" +
          "</span>" +
        "</li>";
    }
    list.innerHTML = html;
  }

  function escapeHtml(text) {
    return String(text === undefined || text === null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function renderBanner(payload) {
    var banner = $("banner");
    if (!banner) return;
    var message = "";
    var level = "info";

    if (!payload.network_ok) {
      message = "No network connection. Meetings will update automatically when it returns.";
      level = "error";
    } else if (payload.calendar && !payload.calendar.configured) {
      message = "No room calendar connected yet.";
      level = "warn";
    } else if (payload.calendar && payload.calendar.stale) {
      message = "Showing saved meetings — the calendar could not be reached.";
      level = "warn";
    } else if (payload.calendar && !payload.calendar.ok && payload.calendar.error) {
      message = "Calendar: " + payload.calendar.error;
      level = "warn";
    }

    if (!message) {
      show(banner, false);
      return;
    }
    banner.className = "banner " + level;
    banner.textContent = message;
    show(banner, true);
  }

  var INDICATOR_LABELS = {
    network: "Network", calendar: "Calendar", camera: "Camera",
    microphone: "Mic", speaker: "Speaker", airplay: "AirPlay", browser: "Display"
  };
  var INDICATOR_ORDER = ["network", "calendar", "camera", "microphone", "speaker", "airplay"];

  function renderIndicators(payload) {
    var host = $("indicators");
    if (!host) return;
    var display = payload.display || {};
    if (!display.show_status) { host.innerHTML = ""; return; }

    var components = (payload.status && payload.status.components) || {};
    var html = "";
    for (var i = 0; i < INDICATOR_ORDER.length; i++) {
      var key = INDICATOR_ORDER[i];
      var status = components[key] || "unknown";
      if (status === "disabled") continue;
      var stateClass = status === "error" ? " error-state" : (status === "warning" ? " warn-state" : "");
      html +=
        '<span class="indicator' + stateClass + '">' +
          '<span class="indicator-dot ' + escapeHtml(status) + '"></span>' +
          escapeHtml(INDICATOR_LABELS[key] || key) +
        "</span>";
    }
    host.innerHTML = html;
  }

  function renderOverlays(payload) {
    var sharing = payload.mode === "screen-sharing";
    show($("overlay-sharing"), sharing);
    if (sharing) {
      var client = (payload.airplay && payload.airplay.client) || "";
      setText("sharing-client", client ? "Shared from " + client : "");
    }

    var setup = !!payload.setup_required && !sharing;
    show($("overlay-setup"), setup);
    renderSetupScan(payload, setup);
    if (!setup) return;

    setText("setup-url", payload.panel_url || "");

    // The PIN only reaches this page when the request came from the Pi itself
    // (see _setup_hint in main.py), so if it is here it is safe to display.
    var info = payload.setup || {};
    var pin = info.pin || "";
    show($("setup-pin-row"), !!pin);
    if (pin) setText("setup-pin", pin);

    setText(
      "setup-hint",
      info.lan === false
        ? "Settings are limited to this Raspberry Pi. To set the room up from a "
          + "phone, run:  roomctl lan-admin on 123456"
        : "Add the room calendar link and this screen will start showing meetings."
    );
  }

  function renderFooter(payload) {
    var airplay = payload.airplay || {};
    var display = payload.display || {};
    setText("airplay-name", airplay.name || payload.room.name || "Meeting Room");
    show($("sharing"), !!display.show_instructions && airplay.enabled !== false);

    // Two addresses in one corner is clutter, and the badge is the one people
    // can act on, so the panel hint gives way while it is on screen.
    var hint = $("panel-hint");
    var wanted = !!display.show_panel_url && !!payload.panel_url && !badgeShown;
    show(hint, wanted);
    if (wanted) setText("panel-url", String(payload.panel_url).replace(/^https?:\/\//, ""));
  }

  // ----------------------------------------------------- controller codes

  var TURN_ON =
    "turn on Settings \u2192 Room controller \u2192 " +
    "\u201cLet phones on the room network open the controller\u201d";
  var CONTROLLER_HINT = "To control the room from a phone, " + TURN_ON;
  var SETUP_HINT = "To set the room up from a phone, " + TURN_ON;

  // The corner badge is read from a metre or two; the setup overlay's code is
  // read from across the room, so it asks for a bigger symbol.
  var BADGE_SCALE = 4;
  var SETUP_SCALE = 8;

  function fingerprint(text) {
    // A small non-cryptographic hash. It only has to change when the pairing
    // URL changes; putting the URL itself in the query string would copy the
    // room's secret into every request log and into the DOM.
    var hash = 5381;
    for (var i = 0; i < text.length; i++) {
      hash = ((hash << 5) + hash + text.charCodeAt(i)) & 0x7fffffff;
    }
    return hash.toString(36);
  }

  function controllerView(payload) {
    // No controller block at all (an older backend, or a request from the LAN,
    // which never gets the pairing address) means: show nothing.
    var info = payload.controller || {};
    var qrUrl = info.qr_url || "";
    return {
      qrUrl: qrUrl,
      host: info.host || "",
      // Something to scan.
      scannable: !!(info.show_qr && info.reachable && qrUrl),
      // On, but no phone can reach it: name the setting that fixes it.
      blocked: !!(info.enabled && info.show_qr && !info.reachable),
      stamp: fingerprint(String(info.url || ""))
    };
  }

  function qrSource(view, scale) {
    if (!view.qrUrl) return "";
    return view.qrUrl + (view.qrUrl.indexOf("?") < 0 ? "?" : "&") +
      "scale=" + scale + "&v=" + view.stamp;
  }

  function applyQr(record, id, src) {
    // The code can be rotated from the panel, so the image has to follow it —
    // but only when it actually changes, or the TV would refetch every poll.
    var image = $(id);
    if (!image || src === record.src) return;
    record.src = src;
    image.src = src;
  }

  function renderController(payload) {
    var badge = $("controller-badge");
    if (!badge) {
      badgeShown = false;
      return;
    }

    var view = controllerView(payload);
    // The overlays carry their own instructions — and the setup overlay its
    // own, larger code — so the corner stands down while either is up.
    var covered = payload.mode === "screen-sharing" || !!payload.setup_required;
    var src = qrSource(view, BADGE_SCALE);
    var showQr = view.scannable && !covered && src !== cornerQr.failed;
    var showHint = view.blocked && !covered;

    badgeShown = showQr || showHint;
    badge.className = "controller-badge" + (showHint ? " is-hint" : "");
    show(badge, badgeShown);
    show($("controller-qr-tile"), showQr);
    setBodyClass("qr-badge", showQr);
    setBodyClass("qr-hint", showHint);

    if (showHint) {
      setText("controller-caption", CONTROLLER_HINT);
      setText("controller-host", "");
      return;
    }
    if (!showQr) return;

    setText("controller-caption", "Scan to control this room");
    // The host address is safe to read out; the pairing code never is.
    setText("controller-host", view.host || "");
    applyQr(cornerQr, "controller-qr", src);
  }

  function renderSetupScan(payload, overlayUp) {
    // The same source of truth as the corner badge, shown while the first-run
    // overlay is up: scanning this is how a room gets set up without anyone
    // plugging a keyboard into the Pi.
    var block = $("setup-scan");
    if (!block) return;

    var view = controllerView(payload);
    var src = qrSource(view, SETUP_SCALE);
    var showQr = overlayUp && view.scannable && src !== setupQr.failed;
    var showHint = overlayUp && view.blocked;

    block.className = "setup-scan" + (showHint ? " is-hint" : "");
    show(block, showQr || showHint);
    show($("setup-qr-tile"), showQr);

    if (showHint) {
      setText("setup-scan-text", SETUP_HINT);
      return;
    }
    if (!showQr) return;

    setText("setup-scan-text", "Scan this to set the room up");
    applyQr(setupQr, "setup-qr", src);
  }

  function watchQrImage(id, record, onFailure) {
    var image = $(id);
    if (!image) return;
    image.addEventListener("error", function () {
      record.failed = record.src;
      onFailure();
    });
  }

  function setBodyClass(name, wanted) {
    if (wanted) document.body.classList.add(name);
    else document.body.classList.remove(name);
  }

  function renderRemote(payload) {
    // Added to /api/state for the hand-held remote and the phone controller,
    // and null except for a few seconds after a button press. An older backend
    // has no key at all, which is simply nothing to announce.
    var remote = payload.remote;
    if (!remote || !remote.detail) return;
    var seen = String(remote.at || "") + "|" + String(remote.action || "") +
      "|" + String(remote.source || "");
    if (seen === lastRemote) return;
    lastRemote = seen;
    toast(remote.detail, remote.ok === false);
  }

  // ------------------------------------------------------------- slideshow
  //
  // The wall plays two kinds of slide. An image is held for `seconds` and
  // crossfaded into the next one. A video ignores `seconds` completely: it
  // plays to its end and only then does the wall move on, because cutting a
  // clip off part way through every 45 seconds is worse than an uneven
  // rotation.
  //
  // Nothing in here may leave the screen stuck. An iPhone .mov is a perfectly
  // valid file that Chromium on a Pi cannot decode, and it has to cost one
  // skip rather than the whole slideshow — hence the watchdog and the list of
  // slides that turned out not to play.

  var VIDEO_METADATA_MS = 15000;  // nothing by now: it is never going to play
  var VIDEO_STALL_MS = 20000;     // playing, but the clock stopped moving
  var VIDEO_FADE_MS = 1700;       // the CSS fade, plus a little
  var IMAGE_LOAD_MS = 30000;      // a fetch this old is not coming back

  function slidesFrom(config) {
    // "media" is the ordered list of slides, images and videos together. An
    // older backend sends only "images", which are all stills, so the wall
    // keeps working across an upgrade.
    var slides = [];
    var media = config.media || [];
    var index;
    for (index = 0; index < media.length; index++) {
      if (media[index] && media[index].url) {
        slides.push({
          url: media[index].url,
          video: media[index].kind === "video"
        });
      }
    }
    if (slides.length) return slides;
    var images = config.images || [];
    for (index = 0; index < images.length; index++) {
      slides.push({ url: images[index], video: false });
    }
    return slides;
  }

  function signature(slides) {
    var parts = [];
    for (var i = 0; i < slides.length; i++) {
      parts.push((slides[i].video ? "v:" : "i:") + slides[i].url);
    }
    return parts.join("|");
  }

  function applyBackground(payload) {
    var config = payload.backgrounds || {};
    var backdrop = $("backdrop");
    var shade = $("backdrop-shade");
    if (!backdrop) return;

    var mode = config.mode || "theme";
    var slides = slidesFrom(config);

    if (mode === "solid") {
      stopSlideshow();
      backdrop.classList.remove("has-image");
      backdrop.style.background = config.solid || "#0b1220";
      return;
    }
    backdrop.style.background = "";

    // A list the backend has changed deserves a clean start, including another
    // go at anything that failed last time (it may have been re-uploaded).
    var raw = signature(slides);
    var restart = raw !== slideshow.raw;
    if (restart) {
      slideshow.raw = raw;
      slideshow.broken = {};
    }

    var playable = [];
    for (var i = 0; i < slides.length; i++) {
      if (!slideshow.broken[slides[i].url]) playable.push(slides[i]);
    }

    if (mode !== "slideshow" || !playable.length) {
      stopSlideshow();
      backdrop.classList.remove("has-image");
      return;
    }

    if (shade) {
      var dim = Math.max(0, Math.min(95, Number(config.dim) || 0)) / 100;
      shade.style.background =
        "linear-gradient(to bottom, rgba(0,0,0," + Math.min(0.95, dim + 0.12).toFixed(2) + "), " +
        "rgba(0,0,0," + (dim * 0.7).toFixed(2) + ") 45%, rgba(0,0,0," + Math.min(0.95, dim + 0.2).toFixed(2) + "))";
    }
    var blur = Math.max(0, Math.min(40, Number(config.blur) || 0));
    var layers = [$("backdrop-a"), $("backdrop-b"), $("backdrop-video")];
    for (var layer = 0; layer < layers.length; layer++) {
      if (layers[layer]) layers[layer].style.filter = blur ? "blur(" + blur + "px)" : "";
    }

    slideshow.seconds = Math.max(5, Number(config.seconds) || 45);
    slideshow.shuffle = !!config.shuffle;
    slideshow.sound = !!config.video_sound;

    // The sound setting can be changed from the panel part way through a clip
    // — unless this clip is only playing because the sound was turned off, in
    // which case un-muting it would hand it back to the browser to pause.
    var video = $("backdrop-video");
    if (video && slideshow.playing && !slideshow.forcedMute) {
      video.muted = !slideshow.sound;
    }

    backdrop.classList.add("has-image");
    if (restart || signature(playable) !== signature(slideshow.media)) {
      slideshow.media = playable;
      slideshow.index = -1;
      nextSlide();
      return;
    }
    keepRunning();
  }

  function clearDwell() {
    if (slideshow.timer) {
      clearTimeout(slideshow.timer);
      slideshow.timer = null;
    }
  }

  function clearWatchdog() {
    if (slideshow.guard) {
      clearInterval(slideshow.guard);
      slideshow.guard = null;
    }
  }

  function armDwell() {
    clearDwell();
    if (slideshow.media.length < 2) return;  // a single still never rotates
    slideshow.timer = setTimeout(nextSlide, slideshow.seconds * 1000);
  }

  function keepRunning() {
    // A poll must never disturb what is on screen: a video is running its own
    // show, and an image still loading will arm its own timer when it lands.
    // This only picks the wall back up if it has somehow stopped.
    if (slideshow.playing || slideshow.timer) return;
    if (slideshow.loading && Date.now() - slideshow.loadingAt < IMAGE_LOAD_MS) return;
    if (slideshow.media.length < 2) return;
    nextSlide();
  }

  function stopSlideshow() {
    clearDwell();
    releaseVideo();
  }

  function nextSlide() {
    clearDwell();
    var slides = slideshow.media;
    if (!slides.length) return;

    var next;
    if (slideshow.shuffle && slides.length > 2) {
      do { next = Math.floor(Math.random() * slides.length); }
      while (next === slideshow.index);
    } else {
      next = (slideshow.index + 1) % slides.length;
    }
    slideshow.index = next;

    if (slides[next].video) showVideoSlide(slides[next]);
    else showImageSlide(slides[next]);
  }

  function dropSlide(url) {
    // A slide that will not play is set aside until the backend's list changes
    // again, so the wall is not spending 15 seconds on it every time round.
    slideshow.broken[url] = true;
    var kept = [];
    for (var i = 0; i < slideshow.media.length; i++) {
      if (slideshow.media[i].url !== url) kept.push(slideshow.media[i]);
    }
    slideshow.media = kept;
    slideshow.index = -1;
    if (!kept.length) {
      stopSlideshow();
      var backdrop = $("backdrop");
      if (backdrop) backdrop.classList.remove("has-image");  // back to the gradient
      return;
    }
    nextSlide();
  }

  // -- still images ----------------------------------------------------

  function showImageSlide(slide) {
    releaseVideo();
    var target = slideshow.layer === 0 ? $("backdrop-b") : $("backdrop-a");
    var current = slideshow.layer === 0 ? $("backdrop-a") : $("backdrop-b");
    if (!target) return;

    var url = slide.url;
    slideshow.loading = true;
    slideshow.loadingAt = Date.now();

    // Preload so the crossfade never shows a blank frame.
    var image = new Image();
    image.onload = function () {
      slideshow.loading = false;
      target.style.backgroundImage = 'url("' + url + '")';
      target.classList.add("visible");
      if (current) current.classList.remove("visible");
      slideshow.layer = slideshow.layer === 0 ? 1 : 0;
      armDwell();  // the clock starts once the picture is actually up
    };
    image.onerror = function () {
      slideshow.loading = false;
      dropSlide(url);  // a deleted image: move on
    };
    image.src = url;
  }

  // -- videos ----------------------------------------------------------

  function showVideoSlide(slide) {
    var video = $("backdrop-video");
    if (!video) {
      dropSlide(slide.url);
      return;
    }
    if (slideshow.release) {
      clearTimeout(slideshow.release);  // it is being reused, not released
      slideshow.release = null;
    }

    slideshow.playing = true;
    slideshow.loading = false;
    slideshow.forcedMute = false;
    slideshow.metadata = false;
    slideshow.startedAt = Date.now();
    slideshow.lastTime = -1;
    slideshow.lastMoved = Date.now();

    // One clip and nothing else loops. Restarting it is better than cutting it
    // off every 45 seconds, and far better than a black screen in between.
    video.loop = slideshow.media.length === 1;
    video.setAttribute("src", slide.url);
    video.load();
    video.classList.add("visible");
    playVideo(video, !slideshow.sound);

    clearWatchdog();
    slideshow.guard = setInterval(watchVideo, 2000);
  }

  function playVideo(video, muted) {
    video.muted = muted;
    var started;
    try {
      started = video.play();
    } catch (error) {
      skipVideo();
      return;
    }
    if (!started || !started.then) return;
    started.catch(function () {
      // Sound is the usual reason a browser refuses to start. Try again
      // silently rather than leaving a frozen frame on the wall.
      if (!muted) {
        slideshow.forcedMute = true;
        playVideo(video, true);
        return;
      }
      skipVideo();
    });
  }

  function watchVideo() {
    var video = $("backdrop-video");
    if (!video || !slideshow.playing) {
      clearWatchdog();
      return;
    }
    var now = Date.now();
    if (!slideshow.metadata) {
      // A file Chromium cannot decode often simply says nothing at all.
      if (now - slideshow.startedAt > VIDEO_METADATA_MS) skipVideo();
      return;
    }
    if (video.currentTime !== slideshow.lastTime) {
      slideshow.lastTime = video.currentTime;
      slideshow.lastMoved = now;
      return;
    }
    // Nothing here ever pauses a slide, so a clip whose clock has stopped is
    // stuck whether it calls itself paused or not — a browser that quietly
    // paused it is exactly the frozen frame this watchdog is for.
    if (now - slideshow.lastMoved > VIDEO_STALL_MS) skipVideo();
  }

  function endVideo() {
    if (!slideshow.playing) return;
    slideshow.playing = false;
    clearWatchdog();
    nextSlide();
  }

  function skipVideo() {
    if (!slideshow.playing) return;
    var slide = slideshow.media[slideshow.index];
    // It never even reported a duration, so it is not a clip this Pi can play:
    // set it aside. One that fails after playing gets another turn later.
    var hopeless = !slideshow.metadata && slide;
    slideshow.playing = false;
    clearWatchdog();
    if (hopeless) dropSlide(slide.url);
    else nextSlide();
  }

  function releaseVideo() {
    var video = $("backdrop-video");
    slideshow.playing = false;
    clearWatchdog();
    if (!video) return;
    video.classList.remove("visible");
    if (!video.getAttribute("src")) return;
    try {
      video.pause();
    } catch (error) {
      // Nothing was playing; the release below is what matters.
    }
    if (slideshow.release) clearTimeout(slideshow.release);
    // Let it fade out before the frame goes, then hand the decoder back: a Pi
    // should not be holding one for a video nobody is watching.
    slideshow.release = setTimeout(function () {
      slideshow.release = null;
      video.removeAttribute("src");
      video.load();
    }, VIDEO_FADE_MS);
  }

  function watchVideoLayer() {
    var video = $("backdrop-video");
    if (!video) return;
    // Every one of these is a no-op unless a video slide is actually on
    // screen, so releasing the element cannot set the wall off again.
    video.addEventListener("loadedmetadata", function () {
      slideshow.metadata = true;
    });
    video.addEventListener("ended", function () {
      if (!video.loop) endVideo();
    });
    video.addEventListener("error", skipVideo);
    video.addEventListener("stalled", skipVideo);
  }

  // ----------------------------------------------------------------- render

  function render(payload) {
    state = payload;
    document.documentElement.setAttribute("data-theme", (payload.display && payload.display.theme) || "dark");
    if (payload.display && payload.display.accent) {
      document.documentElement.style.setProperty("--accent", payload.display.accent);
    }
    setText("room-name", (payload.room && payload.room.name) || "Meeting Room");
    setText("room-subtitle", (payload.room && payload.room.subtitle) || "");

    renderAvailability(payload);
    renderNext(payload);
    renderUpcoming(payload);
    renderBanner(payload);
    renderIndicators(payload);
    renderOverlays(payload);
    renderController(payload);  // before the footer: it decides the panel hint
    renderFooter(payload);
    renderRemote(payload);
    applyBackground(payload);
    tickClock();
  }

  function renderConnectionLost() {
    var banner = $("banner");
    if (!banner) return;
    banner.className = "banner error";
    banner.textContent = "Reconnecting to the room software…";
    show(banner, true);
  }

  // ------------------------------------------------------------------ polls

  function poll() {
    fetchJson("/api/state")
      .then(function (payload) {
        consecutiveFailures = 0;
        pollDelay = POLL_MS;
        if (payload && payload.now) {
          var serverTime = new Date(payload.now).getTime();
          if (!isNaN(serverTime)) {
            var drift = serverTime - Date.now();
            // Only correct meaningful drift, to avoid a jittering clock.
            if (Math.abs(drift - serverOffsetMs) > 1500) serverOffsetMs = drift;
          }
        }
        render(payload);
      })
      .catch(function () {
        consecutiveFailures++;
        // Keep the last good screen up; just say we are reconnecting.
        if (consecutiveFailures >= 2) renderConnectionLost();
        pollDelay = Math.min(POLL_MS_MAX, pollDelay * 1.6);
      })
      .then(function () {
        setTimeout(poll, pollDelay);
      });
  }

  function fetchJson(url, options) {
    var settings = options || {};
    settings.headers = settings.headers || {};
    settings.headers["Accept"] = "application/json";
    if (settings.method && settings.method !== "GET") {
      settings.headers["X-Room-Token"] = csrf;
      settings.headers["Content-Type"] = "application/json";
    }
    settings.credentials = "same-origin";
    settings.cache = "no-store";
    return fetch(url, settings).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok && data && data.error) throw new Error(data.error);
        if (!response.ok) throw new Error("Request failed (" + response.status + ")");
        return data;
      }, function () {
        throw new Error("Unreadable response from the room software");
      });
    });
  }

  // ---------------------------------------------------------------- actions

  function onJoinClick() {
    var button = $("join-button");
    if (!button || button.disabled || joining) return;
    var action = button.dataset.action;
    if (!action) return;

    joining = true;
    button.classList.add("busy");

    var url = action === "leave" ? "/api/actions/leave" : "/api/actions/join";
    var body = action === "join" && button.dataset.meetingId
      ? JSON.stringify({ meeting_id: button.dataset.meetingId })
      : JSON.stringify({});

    fetchJson(url, { method: "POST", body: body })
      .then(function (data) {
        if (data && data.detail) toast(data.detail);
      })
      .catch(function (error) {
        toast(error.message || "That did not work", true);
      })
      .then(function () {
        joining = false;
        button.classList.remove("busy");
        poll();
      });
  }

  // ------------------------------------------------------------------- init

  function init() {
    var button = $("join-button");
    if (button) {
      button.addEventListener("click", onJoinClick);
      // So the Poly remote's OK/Enter key works on the focused button too.
      button.setAttribute("tabindex", "0");
    }
    watchVideoLayer();

    // The /qr route 404s the moment the controller is switched off, and a
    // broken-image icon on the room TV is worse than no code at all.
    watchQrImage("controller-qr", cornerQr, function () {
      show($("controller-badge"), false);
      setBodyClass("qr-badge", false);
    });
    watchQrImage("setup-qr", setupQr, function () {
      show($("setup-scan"), false);
    });

    setInterval(tickClock, CLOCK_MS);
    tickClock();
    poll();

    // If the tab is restored after being hidden, refresh at once.
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) poll();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
