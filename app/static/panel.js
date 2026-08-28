/* Phone control panel: join meetings, adjust sound, manage the background
   slideshow, and restart things when the room misbehaves. */

(function () {
  "use strict";

  var R = window.Room;
  var POLL_MS = 6000;
  var state = null;
  var use24 = false;
  var pollTimer = null;
  var suppressBackgroundSave = false;

  function $(id) { return document.getElementById(id); }
  function show(el, visible) {
    if (!el) return;
    if (visible) el.removeAttribute("hidden"); else el.setAttribute("hidden", "hidden");
  }

  // ------------------------------------------------------------------- state

  // Kept short: this sits beside the health pill on a narrow phone.
  var MODE_TEXT = {
    home: "Dashboard",
    meeting: "In a meeting",
    "screen-sharing": "Screen sharing",
    offline: "Offline"
  };

  function renderState(payload) {
    state = payload;
    use24 = !!payload.time_format_24h;

    $("mode-text").textContent = MODE_TEXT[payload.mode] || payload.mode;

    var sub = [];
    var room = payload.room || {};
    sub.push(room.available ? "Room available" : "Room in use");
    if (payload.calendar && payload.calendar.stale) sub.push("calendar offline, showing saved meetings");
    else if (payload.calendar && !payload.calendar.ok && payload.calendar.error) sub.push(payload.calendar.error);
    if (!payload.network_ok) sub.push("no internet");
    $("mode-sub").textContent = sub.join(" · ");

    var overall = (payload.status && payload.status.overall) || "unknown";
    var pill = $("health-pill");
    pill.className = "pill " + overall;
    pill.querySelector(".dot").className = "dot " + overall;
    $("health-text").textContent =
      overall === "ok" ? "All good" : (overall === "warning" ? "Needs a look" : "Problem");

    var active = payload.active_meeting;
    var note = $("active-note");
    show(note, !!active);
    if (active) {
      note.textContent = "On screen: " + (active.title || "meeting") +
        (active.opened_manually ? " (opened by hand)" : " (opened automatically)");
    }

    var joinButton = $("join-now");
    joinButton.disabled = !payload.join_available && !active;
    joinButton.textContent = active ? "Rejoin / retry join" : "Join next meeting";

    renderMeetings(payload);
    renderAudio(payload);
    renderBackgroundState(payload);
  }

  function renderMeetings(payload) {
    var list = $("meeting-list");
    var empty = $("meeting-empty");
    var rows = [];

    if (payload.current) rows.push({ meeting: payload.current, live: true });
    (payload.upcoming || []).forEach(function (meeting) {
      if (!payload.current || meeting.id !== payload.current.id) {
        rows.push({ meeting: meeting, live: false });
      }
    });

    if (!rows.length) {
      list.innerHTML = "";
      empty.textContent = (payload.calendar && !payload.calendar.configured)
        ? "No calendar connected yet."
        : "No meetings coming up.";
      show(empty, true);
      return;
    }
    show(empty, false);

    list.innerHTML = rows.map(function (row) {
      var meeting = row.meeting;
      var when = R.formatTime(meeting.start, use24);
      var chip = meeting.provider
        ? '<span class="provider-chip ' + R.escapeHtml(meeting.provider) + '"></span>'
        : "";
      var subtitle = meeting.provider
        ? R.escapeHtml(meeting.provider_name || "")
        : R.escapeHtml(meeting.location || "No online link");
      var detail = row.live ? "now" : R.relativeStart(meeting.start);
      var button = meeting.has_link
        ? '<button class="btn btn-small btn-primary" data-join="' + R.escapeHtml(meeting.id) + '">Join</button>'
        : '<button class="btn btn-small" disabled>No link</button>';

      return '<li class="meeting-row">' +
        '<span class="meeting-when">' + R.escapeHtml(when) + "</span>" +
        '<span class="meeting-main">' +
          '<span class="meeting-name">' + R.escapeHtml(meeting.title || "Meeting") + "</span>" +
          '<span class="meeting-sub">' + chip + subtitle + " · " + R.escapeHtml(detail) + "</span>" +
        "</span>" + button + "</li>";
    }).join("");
  }

  function renderAudio(payload) {
    var poly = (payload.status && payload.status.components) || {};
    var note = [];
    if (poly.speaker === "error") note.push("No speaker detected.");
    if (poly.microphone === "error") note.push("No microphone detected.");
    if (poly.camera === "error") note.push("No camera detected.");
    $("audio-note").textContent = note.join(" ") ||
      "Using the conference bar for camera, microphone and speaker.";
  }

  function refreshAudioLevels() {
    R.get("/api/health").then(function (health) {
      var poly = health.poly || {};
      var speaker = poly.speaker || {};
      var microphone = poly.microphone || {};
      if (typeof speaker.volume === "number") {
        $("volume").value = speaker.volume;
        $("volume-value").textContent = speaker.volume + "%";
      } else {
        $("volume-value").textContent = "unavailable";
      }
      $("mute-toggle").textContent = microphone.muted ? "Unmute microphone" : "Mute microphone";
    }).catch(function () { /* the panel is still usable without levels */ });
  }

  // -------------------------------------------------------------- backgrounds

  function renderBackgroundState(payload) {
    var config = payload.backgrounds || {};
    suppressBackgroundSave = true;
    $("background-mode").value = config.mode || "theme";
    $("background-seconds").value = config.seconds || 45;
    $("seconds-value").textContent = (config.seconds || 45);
    $("background-dim").value = config.dim === undefined ? 55 : config.dim;
    $("dim-value").textContent = ($("background-dim").value) + "%";
    $("background-shuffle").checked = !!config.shuffle;
    suppressBackgroundSave = false;
    show($("slideshow-options"), (config.mode || "theme") === "slideshow");
  }

  function loadBackgrounds() {
    return R.get("/api/backgrounds").then(function (data) {
      var grid = $("thumb-grid");
      var html = (data.images || []).map(function (image) {
        return '<div class="thumb" style="background-image:url(\'' + R.escapeHtml(image.url) + '\')">' +
          '<button class="thumb-remove" data-remove="' + R.escapeHtml(image.name) +
          '" aria-label="Remove image">&times;</button></div>';
      }).join("");

      if (data.uploads_allowed) {
        html += '<button class="upload-tile" id="upload-tile">+ Add images</button>';
      }
      grid.innerHTML = html;

      $("background-count").textContent = data.count + " / " + data.max + " images";
      $("upload-note").textContent = data.uploads_allowed
        ? "JPEG, PNG, GIF or WebP, up to " + data.max_size_mb + " MB each."
        : "Uploads are switched off in Settings.";

      var tile = $("upload-tile");
      if (tile) tile.addEventListener("click", function () { $("upload-input").click(); });
      return data;
    });
  }

  function uploadFiles(files) {
    if (!files || !files.length) return;
    var queue = Array.prototype.slice.call(files);
    var uploaded = 0;
    var failures = [];

    function next() {
      if (!queue.length) {
        loadBackgrounds().then(poll);
        if (failures.length) R.toast(failures[0], "error");
        else if (uploaded) R.toast("Added " + uploaded + " image" + (uploaded === 1 ? "" : "s"), "ok");
        return;
      }
      var file = queue.shift();
      var form = new FormData();
      form.append("image", file);
      R.toast("Uploading " + file.name + "…");
      fetch("/api/backgrounds", {
        method: "POST",
        body: form,
        headers: { "X-Room-Token": R.csrf, "Accept": "application/json" },
        credentials: "same-origin"
      }).then(function (response) {
        return response.json().catch(function () { return {}; });
      }).then(function (data) {
        if (data && data.ok) uploaded++;
        else failures.push((data && data.error) || ("Could not add " + file.name));
      }).catch(function () {
        failures.push("Could not add " + file.name);
      }).then(next);
    }
    next();
  }

  function saveBackgroundSettings() {
    if (suppressBackgroundSave) return;
    var payload = {
      BACKGROUND_MODE: $("background-mode").value,
      BACKGROUND_SLIDESHOW_SECONDS: Number($("background-seconds").value),
      BACKGROUND_DIM_PERCENT: Number($("background-dim").value),
      BACKGROUND_SHUFFLE: $("background-shuffle").checked
    };
    R.post("/api/settings", payload)
      .then(function () { R.toast("Background updated", "ok"); poll(); })
      .catch(function (error) { R.toast(error.message, "error"); });
  }

  var saveTimer = null;
  function saveBackgroundSoon() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(saveBackgroundSettings, 600);
  }

  // ----------------------------------------------------------------- polling

  function poll() {
    return R.get("/api/state")
      .then(renderState)
      .catch(function () { /* keep the last view; the next poll may succeed */ });
  }

  function schedulePolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(function () {
      if (!document.hidden) poll();
    }, POLL_MS);
  }

  // ----------------------------------------------------------------- actions

  function wire() {
    $("join-now").addEventListener("click", function () {
      var button = this;
      var active = state && state.active_meeting;
      R.withButton(button, function () {
        return active ? R.post("/api/actions/retry-join") : R.post("/api/actions/join");
      }).then(poll).catch(function () {});
    });

    $("show-dashboard").addEventListener("click", function () {
      R.withButton(this, function () { return R.post("/api/actions/home"); })
        .then(poll).catch(function () {});
    });

    $("refresh-calendar").addEventListener("click", function () {
      R.withButton(this, function () { return R.post("/api/actions/refresh-calendar"); })
        .then(function () { setTimeout(poll, 1200); }).catch(function () {});
    });

    $("meeting-list").addEventListener("click", function (event) {
      var button = event.target.closest("[data-join]");
      if (!button) return;
      R.withButton(button, function () {
        return R.post("/api/actions/join", { meeting_id: button.getAttribute("data-join") });
      }).then(poll).catch(function () {});
    });

    var volume = $("volume");
    volume.addEventListener("input", function () {
      $("volume-value").textContent = volume.value + "%";
    });
    volume.addEventListener("change", function () {
      R.post("/api/actions/volume", { level: Number(volume.value) })
        .catch(function (error) { R.toast(error.message, "error"); });
    });

    $("mute-toggle").addEventListener("click", function () {
      var button = this;
      R.withButton(button, function () { return R.post("/api/actions/mute"); })
        .then(function (data) {
          button.textContent = data && data.muted ? "Unmute microphone" : "Mute microphone";
        }).catch(function () {});
    });

    $("camera-toggle").addEventListener("click", function () {
      R.withButton(this, function () { return R.post("/api/actions/remote/camera"); })
        .catch(function () {});
    });

    // Background controls
    $("background-mode").addEventListener("change", function () {
      show($("slideshow-options"), this.value === "slideshow");
      saveBackgroundSettings();
    });
    $("background-seconds").addEventListener("input", function () {
      $("seconds-value").textContent = this.value;
    });
    $("background-seconds").addEventListener("change", saveBackgroundSoon);
    $("background-dim").addEventListener("input", function () {
      $("dim-value").textContent = this.value + "%";
    });
    $("background-dim").addEventListener("change", saveBackgroundSoon);
    $("background-shuffle").addEventListener("change", saveBackgroundSettings);

    $("upload-input").addEventListener("change", function () {
      uploadFiles(this.files);
      this.value = "";
    });

    $("thumb-grid").addEventListener("click", function (event) {
      var button = event.target.closest("[data-remove]");
      if (!button) return;
      var name = button.getAttribute("data-remove");
      R.del("/api/backgrounds/" + encodeURIComponent(name))
        .then(function () { R.toast("Image removed", "ok"); return loadBackgrounds(); })
        .then(poll)
        .catch(function (error) { R.toast(error.message, "error"); });
    });

    // Recovery buttons
    Array.prototype.forEach.call(document.querySelectorAll("[data-restart]"), function (button) {
      button.addEventListener("click", function () {
        var target = button.getAttribute("data-restart");
        if (target === "all" && !R.confirmAction(
          "Restart everything in the room? The TV will go blank for a few seconds.")) return;
        R.withButton(button, function () {
          return R.post("/api/actions/restart", { target: target });
        }).then(function () { setTimeout(poll, 4000); }).catch(function () {});
      });
    });

    $("reset-safe").addEventListener("click", function () {
      if (!R.confirmAction(
        "Reset all settings to their defaults?\n\nThe calendar link, room name and " +
        "admin PIN are kept. Background images are kept.")) return;
      R.withButton(this, function () { return R.post("/api/actions/reset-safe"); })
        .then(function () { setTimeout(function () { window.location.reload(); }, 2500); })
        .catch(function () {});
    });

    $("reboot").addEventListener("click", function () {
      if (!R.confirmAction("Reboot the Raspberry Pi? The room is unavailable for about a minute."))
        return;
      R.withButton(this, function () { return R.post("/api/actions/reboot"); })
        .catch(function () {});
    });

    var signOut = $("sign-out");
    if (signOut) {
      signOut.addEventListener("click", function () {
        R.post("/logout").then(function () { window.location.href = "/login"; })
          .catch(function () { window.location.href = "/login"; });
      });
    }
  }

  function init() {
    wire();
    poll().then(loadBackgrounds).then(refreshAudioLevels);
    schedulePolling();
    setInterval(function () { if (!document.hidden) refreshAudioLevels(); }, 15000);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) { poll(); refreshAudioLevels(); }
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
