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

  // Background slideshow
  var slideshow = { images: [], index: -1, layer: 0, timer: null, seconds: 45 };

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
    if (setup) setText("setup-url", payload.panel_url || "");
  }

  function renderFooter(payload) {
    var airplay = payload.airplay || {};
    var display = payload.display || {};
    setText("airplay-name", airplay.name || payload.room.name || "Meeting Room");
    show($("sharing"), !!display.show_instructions && airplay.enabled !== false);

    var hint = $("panel-hint");
    var wanted = !!display.show_panel_url && !!payload.panel_url;
    show(hint, wanted);
    if (wanted) setText("panel-url", String(payload.panel_url).replace(/^https?:\/\//, ""));
  }

  // ------------------------------------------------------------- slideshow

  function applyBackground(payload) {
    var config = payload.backgrounds || {};
    var backdrop = $("backdrop");
    var shade = $("backdrop-shade");
    if (!backdrop) return;

    var images = config.images || [];
    var mode = config.mode || "theme";

    if (mode === "solid") {
      stopSlideshow();
      backdrop.classList.remove("has-image");
      backdrop.style.background = config.solid || "#0b1220";
      return;
    }
    backdrop.style.background = "";

    if (mode !== "slideshow" || !images.length) {
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
    var layers = [$("backdrop-a"), $("backdrop-b")];
    for (var i = 0; i < layers.length; i++) {
      if (layers[i]) layers[i].style.filter = blur ? "blur(" + blur + "px)" : "";
    }

    var changed = images.join("|") !== slideshow.images.join("|");
    slideshow.seconds = Math.max(5, Number(config.seconds) || 45);
    slideshow.shuffle = !!config.shuffle;

    if (changed) {
      slideshow.images = images.slice();
      slideshow.index = -1;
      backdrop.classList.add("has-image");
      nextSlide();
    } else {
      backdrop.classList.add("has-image");
    }
    startSlideshow();
  }

  function startSlideshow() {
    if (slideshow.timer) return;
    if (slideshow.images.length < 2) return;
    slideshow.timer = setInterval(nextSlide, slideshow.seconds * 1000);
  }

  function stopSlideshow() {
    if (slideshow.timer) { clearInterval(slideshow.timer); slideshow.timer = null; }
  }

  function nextSlide() {
    var images = slideshow.images;
    if (!images.length) return;

    var next;
    if (slideshow.shuffle && images.length > 2) {
      do { next = Math.floor(Math.random() * images.length); }
      while (next === slideshow.index);
    } else {
      next = (slideshow.index + 1) % images.length;
    }
    slideshow.index = next;

    var target = slideshow.layer === 0 ? $("backdrop-b") : $("backdrop-a");
    var current = slideshow.layer === 0 ? $("backdrop-a") : $("backdrop-b");
    if (!target) return;

    // Preload so the crossfade never shows a blank frame.
    var image = new Image();
    image.onload = function () {
      target.style.backgroundImage = 'url("' + images[next] + '")';
      target.classList.add("visible");
      if (current) current.classList.remove("visible");
      slideshow.layer = slideshow.layer === 0 ? 1 : 0;
    };
    image.onerror = function () {
      // A deleted image: drop it and try the next one.
      slideshow.images = slideshow.images.filter(function (url) { return url !== images[next]; });
      if (slideshow.images.length) nextSlide();
    };
    image.src = images[next];
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
    renderFooter(payload);
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
