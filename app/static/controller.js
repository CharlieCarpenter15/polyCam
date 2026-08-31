/* The hand-held room controller (/controller).

   One page, one job: someone walks in, scans the code on the TV, and has to
   understand in about three seconds what is happening in this room, what to
   press, and what the buttons on the physical remote do.

   Everything comes from /api/controller/state — one call, polled while the
   page is on screen — and every press goes to /api/controller/action. Plain
   ES5: this runs on whatever phone happens to walk into the room. */

(function () {
  "use strict";

  var R = window.Room;

  var POLL_MS = 3000;
  var POLL_MAX_MS = 30000;
  var HIDDEN_MS = 4000;          // a phone in a pocket must not hammer the Pi
  var SOON_MINUTES = 20;         // "starting soon" rather than "later today"
  var VOLUME_HOLD_MS = 4000;     // do not fight a finger on the slider

  var view = null;               // what the primary button currently means
  var use24 = false;
  var failures = 0;
  var pollDelay = POLL_MS;
  var pollTimer = null;
  var stopped = false;
  var volumeHeldUntil = 0;

  var airplayName = document.body.getAttribute("data-airplay") || "this room";

  // --------------------------------------------------------------- tiny DOM

  function $(id) { return document.getElementById(id); }

  function show(el, visible) {
    if (!el) return;
    if (visible) el.removeAttribute("hidden");
    else el.setAttribute("hidden", "hidden");
  }

  function setText(id, text) {
    var el = $(id);
    // Only touch the DOM on a real change: the status line is an aria-live
    // region, and rewriting it every three seconds would make a screen reader
    // repeat itself endlessly.
    if (el && el.textContent !== text) el.textContent = text;
  }

  /* classList.toggle's second argument is ignored by some older phone
     browsers, so say what we mean. */
  function setClass(el, name, on) {
    if (!el) return;
    if (on) el.classList.add(name);
    else el.classList.remove(name);
  }

  function setDisabled(el, disabled) {
    // A button that is mid-request owns its own state until the answer lands.
    if (!el || el.classList.contains("busy")) return;
    el.disabled = !!disabled;
  }

  function setBusy(button, busy) {
    if (!button) return;
    setClass(button, "busy", busy);
    button.disabled = !!busy;
  }

  function andList(items) {
    if (items.length < 2) return items.join("");
    return items.slice(0, -1).join(", ") + " and " + items[items.length - 1];
  }

  function shorten(text, max) {
    var value = String(text || "");
    return value.length > max ? value.slice(0, max - 1) + "…" : value;
  }

  // ------------------------------------------------------------- meetings

  function titleOf(meeting) {
    return (meeting && meeting.title) || "the meeting";
  }

  function clockTime(iso) {
    return R.formatTime(iso, use24) || "";
  }

  function minutesUntil(iso) {
    var target = new Date(iso);
    if (isNaN(target.getTime())) return null;
    return (target.getTime() - Date.now()) / 60000;
  }

  /* "in 25 min", "in 2 h 10 min". Not Room.relativeStart: it rounds the
     leftover minutes on their own, so 3 h 59.6 min comes out as "3 h 60 min". */
  function relativeWhen(iso) {
    var mins = minutesUntil(iso);
    if (mins === null) return "";
    if (mins < -1) return "started " + Math.abs(Math.round(mins)) + " min ago";
    if (mins < 1) return "now";
    if (mins < 60) return "in " + Math.ceil(mins) + " min";
    var total = Math.round(mins);
    var hours = Math.floor(total / 60);
    var rest = total % 60;
    return "in " + hours + " h" + (rest ? " " + rest + " min" : "");
  }

  /* The meeting the big button would open. The backend joins the meeting in
     progress, else the next one that has a link — mirror that here so the
     button names the meeting that will actually open. */
  function joinTarget(payload) {
    var candidates = [];
    if (payload.current) candidates.push(payload.current);
    var upcoming = payload.upcoming || [];
    for (var i = 0; i < upcoming.length; i++) {
      if (!payload.current || upcoming[i].id !== payload.current.id) {
        candidates.push(upcoming[i]);
      }
    }
    for (var j = 0; j < candidates.length; j++) {
      if (candidates[j] && candidates[j].has_link && !candidates[j].cancelled) {
        return candidates[j];
      }
    }
    return null;
  }

  function isInProgress(meeting, payload) {
    return !!(payload.current && meeting && meeting.id === payload.current.id);
  }

  /* True when the room could actually open a meeting now. Offline or
     unconfigured, a "Join" button would only produce an error. */
  function canJoinAnything(payload) {
    return !payload.setup_required && payload.mode !== "offline" && payload.network_ok !== false;
  }

  /* The meeting a person in the room is thinking about: the one in progress,
     or the one about to start. Anything further away is "later today" and
     must not hijack the big button. */
  function imminent(meeting, payload) {
    if (!meeting) return false;
    if (isInProgress(meeting, payload)) return true;
    var mins = minutesUntil(meeting.start);
    return mins !== null && mins <= SOON_MINUTES;
  }

  /* "automatic" (the room opens meetings itself) or "manual". */
  function joinMode(payload) {
    return (payload.join && payload.join.mode) || "automatic";
  }

  function automation(payload) {
    return (payload.join && payload.join.automation) || {};
  }

  function calendarTrouble(payload) {
    var cal = payload.calendar || {};
    if (!cal.configured) return "";
    if (cal.stale) return "The calendar has not refreshed lately, so this list may be out of date.";
    if (cal.ok === false) {
      return "The room cannot read the calendar right now" + (cal.error ? ": " + cal.error : ".");
    }
    return "";
  }

  // ------------------------------------------------------- reading the room

  function asJoin(result, meeting, payload) {
    result.kind = "join";
    result.disabled = false;
    result.meetingId = meeting.id || "";
    result.label = "Join " + shorten(titleOf(meeting), 34);
    var when = isInProgress(meeting, payload)
      ? "on now until " + clockTime(meeting.end)
      : "starts at " + clockTime(meeting.start);
    var provider = meeting.provider_name ? meeting.provider_name + " · " : "";
    result.note = provider + when;
    return result;
  }

  /* Turns the state payload into the four things the top of the page shows:
     a headline, a plain-English hint, the primary button, and the small line
     under it. Order matters — the first case that matches wins, because that
     is the thing the person in the room needs to deal with. */
  function describe(payload) {
    var result = {
      headline: "Room controller",
      hint: "",
      label: "Nothing to join right now",
      note: "",
      kind: "idle",
      meetingId: "",
      disabled: true
    };

    var active = payload.active_meeting;
    var sharing = payload.sharing || {};
    var screen = sharing.name || airplayName;
    var target = joinTarget(payload);

    if (payload.setup_required) {
      result.headline = "This room is not set up yet";
      result.hint = "No calendar is connected, so the room does not know about any " +
        "meetings. Whoever looks after this room can add one in its settings.";
      result.note = "Nothing to join until a calendar is connected.";
      return result;
    }

    var cal = payload.calendar || {};
    var knowsNothing = !payload.current && !(payload.upcoming || []).length;
    if (cal.configured && cal.ok === false && !cal.stale && knowsNothing) {
      result.headline = "The calendar is not answering";
      result.hint = "The room cannot read its calendar, so it does not know what is " +
        "booked in here" + (cal.error ? " (" + cal.error + ")" : "") + ". It keeps " +
        "trying. You can still share your screen with " + screen + ".";
      result.note = "Nothing to join until the calendar comes back.";
      return result;
    }

    if (payload.mode === "offline" || payload.network_ok === false) {
      result.headline = "The room is offline";
      result.hint = "This room has lost its network connection, so it cannot open a " +
        "meeting. It usually comes back on its own within a minute.";
      if (active) {
        result.kind = "leave";
        result.label = "Leave the meeting";
        result.disabled = false;
        result.note = titleOf(active) + " is still on the TV.";
      } else {
        result.note = "The room needs its connection back before it can join anything.";
      }
      return result;
    }

    if (active || payload.mode === "meeting") {
      var join = automation(payload);

      // On the meeting page but not in the call: from a chair in the room
      // that looks like the room having joined, so the one button on this
      // page has to be the one that finishes the job.
      if (active && join.gave_up) {
        result.headline = "The room has not joined yet";
        result.hint = titleOf(active) + " is on the TV, but the room could not " +
          "press Join on the meeting's own page. Tap below and it will have " +
          "another go.";
        result.kind = "join";
        result.disabled = false;
        result.meetingId = active.id || "";
        result.label = "Try joining again";
        result.note = active.scheduled_end
          ? "Booked until " + clockTime(active.scheduled_end)
          : "";
        return result;
      }

      result.headline = "In a meeting";
      result.hint = join.waiting
        ? (active ? titleOf(active) : "The meeting") + " is on the TV and the " +
          "room is waiting to be let in. It joins the moment the host admits it."
        : (active ? titleOf(active) : "A meeting") +
          " is on the TV. Tap Leave when you are done, so the room is free for " +
          "whoever is in here next.";
      result.kind = "leave";
      result.label = "Leave the meeting";
      result.disabled = false;
      if (active && active.scheduled_end) {
        result.note = "Booked until " + clockTime(active.scheduled_end);
      }
      return result;
    }

    if (sharing.active) {
      result.headline = "Someone is sharing their screen";
      result.hint = (sharing.client ? sharing.client + " is sharing" : "A device is sharing") +
        " to " + screen + ". Stop sharing on that device to put the dashboard back.";
      if (target && imminent(target, payload)) {
        asJoin(result, target, payload);
        result.note = "Joining takes the screen share off the TV.";
      } else {
        result.note = "Nothing on the calendar to join right now.";
      }
      return result;
    }

    // Something to join now, or in the next few minutes.
    if (target && imminent(target, payload)) {
      asJoin(result, target, payload);
      var mins = minutesUntil(target.start);
      if (isInProgress(target, payload)) {
        result.headline = titleOf(target) + " is on now";
        result.hint = "Nobody has put it on the TV yet. Tap the big button and the " +
          "room joins it for you.";
      } else {
        result.headline = mins !== null && mins <= 1
          ? titleOf(target) + " starts now"
          : titleOf(target) + " starts in " + Math.ceil(mins) + " minutes";
        result.hint = joinMode(payload) === "manual"
          ? "This room does not join by itself. Tap the big button and it goes " +
            "on the TV."
          : "It goes on the TV by itself just before it starts. Tap the big " +
            "button if you want it now.";
      }
      return result;
    }

    // The room is taken right now, but by something it cannot join. This has
    // to beat a joinable meeting hours away, or the page would claim the room
    // is free while people are sitting in it.
    if (payload.current) {
      result.headline = "The room is booked";
      result.hint = titleOf(payload.current) + " has this room until " +
        clockTime(payload.current.end) + ", but there is no online link to join. " +
        "Share your screen with " + screen + " if you need something on the TV.";
      result.note = "That meeting has no online link, so the room cannot join it.";
      return result;
    }

    // Free for now: say what is next, and let someone open it early.
    if (target) {
      asJoin(result, target, payload);
      result.headline = "The room is free";
      result.hint = "Nothing is booked until " + titleOf(target) + " at " +
        clockTime(target.start) + ". You can open it early if you need the room now.";
      return result;
    }

    if (payload.next) {
      result.headline = "The room is free";
      result.hint = "Next up is " + titleOf(payload.next) + " at " +
        clockTime(payload.next.start) + ", which has no online link. Share your " +
        "screen with " + screen + " if you need the TV now.";
      result.note = "That meeting has no online link, so the room cannot join it.";
      return result;
    }

    result.headline = "Nothing on right now";
    result.hint = "The room is free. To put something on the TV, choose " + screen +
      " in your laptop or phone's screen-mirroring menu.";
    result.note = "Nothing on the calendar to join.";
    return result;
  }

  // -------------------------------------------------------------- rendering

  function renderHero(payload) {
    view = describe(payload);

    setText("mode-line", view.headline);
    setText("hint-line", view.hint);

    var button = $("primary-action");
    if (!button.classList.contains("busy")) {
      button.textContent = view.label;
      button.disabled = !!view.disabled;
    }
    setClass(button, "is-leave", view.kind === "leave");

    setText("primary-note", view.note);
    show($("primary-note"), !!view.note);
  }

  function renderEcho(payload) {
    var remote = payload.remote;
    var el = $("remote-echo");
    if (!remote || !remote.action) {
      show(el, false);
      return;
    }
    // Two people can be controlling one room — the phone and the remote on the
    // table. Saying what just happened stops them fighting each other.
    var phrases = {
      join: "joined the meeting",
      hangup: "left the meeting",
      leave: "left the meeting",
      mute: "changed the microphone",
      volume_up: "turned the volume up",
      volume_down: "turned the volume down",
      volume_set: "changed the volume",
      camera: "switched the camera",
      home: "put the dashboard back"
    };
    var where = remote.source === "remote" ? "on the room remote" : "from another phone";
    var what = phrases[remote.action] || "pressed a button";
    var failed = remote.ok === false ? " — that did not work" : "";
    el.textContent = "Someone " + what + " " + where + failed + ".";
    show(el, true);
  }

  function renderControls(payload) {
    var audio = payload.audio || {};
    var active = payload.active_meeting;

    // Microphone: it is muted at the operating-system level, so it works
    // whether or not a meeting is open.
    var micOk = audio.microphone_ok !== false;
    var muted = audio.muted === true;
    setText("mute-label", muted ? "Unmute microphone" : "Mute microphone");
    setText("mute-sub", !micOk
      ? "No microphone detected"
      : (muted ? "Nobody can hear the room" : "The room can be heard"));
    setClass($("mute-button"), "is-on", micOk && muted);
    setDisabled($("mute-button"), !micOk);

    // Camera: the toggle reaches into the meeting page, so outside a meeting
    // there is nothing for it to do. Say that rather than fail on the tap.
    var cameraOk = audio.camera_ok !== false;
    setText("camera-sub", !cameraOk
      ? "No camera detected"
      : (active ? "Turn it on or off" : "Only during a meeting"));
    setDisabled($("camera-button"), !cameraOk || !active);

    var speakerOk = audio.speaker_ok !== false;
    var level = typeof audio.volume === "number" ? audio.volume : null;
    setText("volume-down-sub", speakerOk ? "Quieter" : "No speaker detected");
    setText("volume-up-sub", speakerOk ? "Louder" : "No speaker detected");
    setDisabled($("volume-down"), !speakerOk);
    setDisabled($("volume-up"), !speakerOk);

    var slider = $("volume-slider");
    slider.disabled = !speakerOk;
    if (level !== null && Date.now() > volumeHeldUntil) {
      slider.value = String(level);
      setText("volume-value", level + "%");
    } else if (level === null) {
      setText("volume-value", speakerOk ? "—" : "No speaker");
    }

    var missing = [];
    if (!micOk) missing.push("microphone");
    if (!speakerOk) missing.push("speaker");
    if (!cameraOk) missing.push("camera");
    setText("device-note", missing.length
      ? "The room cannot find its " + andList(missing) + ". Tell whoever looks " +
        "after this room — the greyed-out buttons come back on their own once " +
        "the hardware does."
      : "Camera, microphone and speaker are the conference bar in this room.");
  }

  function renderMeetings(payload) {
    var rows = [];
    if (payload.current) rows.push({ meeting: payload.current, live: true });
    var upcoming = payload.upcoming || [];
    for (var i = 0; i < upcoming.length; i++) {
      if (!payload.current || upcoming[i].id !== payload.current.id) {
        rows.push({ meeting: upcoming[i], live: false });
      }
    }

    var list = $("meeting-list");
    var empty = $("meeting-empty");

    if (!rows.length) {
      list.innerHTML = "";
      var cal = payload.calendar || {};
      var message = "Nothing booked in this room for the rest of the day.";
      if (!cal.configured) message = "No calendar is connected to this room yet.";
      // The reason is already spelled out in the hero; do not print it twice.
      else if (cal.ok === false) message = "The room cannot read the calendar right now.";
      empty.textContent = message;
      show(empty, true);
      return;
    }

    var trouble = calendarTrouble(payload);
    empty.textContent = trouble;
    show(empty, !!trouble);

    var canJoin = canJoinAnything(payload);
    var html = "";
    for (var r = 0; r < rows.length; r++) {
      var meeting = rows[r].meeting;
      // Calendar titles are external data: escape everything that goes in.
      var when = R.escapeHtml(clockTime(meeting.start));
      var name = R.escapeHtml(meeting.title || "Meeting");
      // "chip-" prefixed: the provider id is a class name here, and one of them
      // is "meet", which would otherwise collide with the row's own class.
      var chip = meeting.provider
        ? '<span class="chip chip-' + R.escapeHtml(meeting.provider) + '" aria-hidden="true"></span>'
        : "";
      // The countdown first: it is the half of this line worth reading, and
      // the half that survives when a narrow phone truncates it.
      var detail = rows[r].live ? "on now" : relativeWhen(meeting.start);
      var about = meeting.provider_name || meeting.location ||
        (meeting.has_link ? "" : "no online link");
      var sub = R.escapeHtml(detail + (about ? " · " + about : ""));
      var inner =
        '<span class="meet-when">' + when + "</span>" +
        '<span class="meet-main">' +
          '<span class="meet-name">' + name + "</span>" +
          '<span class="meet-sub">' + chip + sub + "</span>" +
        "</span>";

      if (meeting.has_link && !meeting.cancelled && canJoin) {
        html += '<li class="meet-item">' +
          '<button type="button" class="meet' + (rows[r].live ? " is-live" : "") +
            '" data-join="' + R.escapeHtml(meeting.id) + '">' +
            inner + '<span class="meet-go">Join</span>' +
          "</button></li>";
      } else {
        // A row the room cannot open is not a button: it says why instead of
        // waiting to fail under someone's thumb.
        var why = meeting.cancelled ? "Cancelled"
          : (!meeting.has_link ? "No link" : "Room offline");
        html += '<li class="meet-item"><div class="meet is-flat">' +
          inner + '<span class="meet-none">' + why + "</span>" +
        "</div></li>";
      }
    }
    list.innerHTML = html;
  }

  function setLink(connected) {
    var chip = $("link-chip");
    setClass(chip, "is-live", connected);
    setClass(chip, "is-lost", !connected);
    setText("link-text", connected ? "Connected" : "Reconnecting…");
  }

  function render(payload) {
    use24 = !!payload.time_format_24h;
    renderHero(payload);
    renderEcho(payload);
    renderControls(payload);
    renderMeetings(payload);
  }

  // ---------------------------------------------------------------- actions

  function isPairingError(error) {
    return !!(error && error.status === 401 && error.data && error.data.needs_pairing);
  }

  function handleError(error) {
    if (isPairingError(error)) {
      stopped = true;
      window.location.href = "/controller/locked";
      return;
    }
    R.toast((error && error.message) || "That did not work", "error");
  }

  /* Every press goes through here: busy state on the button, one toast saying
     what happened, and a fresh read of the room straight afterwards rather
     than waiting up to three seconds for the next poll.

     Not Room.withButton: it restores a button by rewriting textContent, which
     would flatten the two-line labels on this page, and it reads a 200 with
     `ok: false` (which the action dispatcher returns when there was nothing to
     do) as a success. */
  function runAction(button, options) {
    if (!button || button.disabled || button.classList.contains("busy")) return;
    var body = { action: options.action };
    var extra = options.body;
    if (extra) {
      for (var key in extra) {
        if (Object.prototype.hasOwnProperty.call(extra, key)) body[key] = extra[key];
      }
    }
    setBusy(button, true);
    R.post("/api/controller/action", body)
      .then(function (data) {
        var answer = data || {};
        if (answer.ok === false) {
          // `fail` may be a function where the server's own detail is worth
          // reading (joining), or a plain sentence where it is not: a refused
          // hang-up answers with the word "left".
          var why = typeof options.fail === "function" ? options.fail(answer) : options.fail;
          R.toast(answer.error || why, "error");
        } else {
          R.toast(options.ok(answer), "ok");
        }
      })
      .catch(handleError)
      .then(function () {
        setBusy(button, false);
        refreshNow();
      });
  }

  function joining(data) {
    return "Opening " + (data.detail || "the meeting") + " on the TV…";
  }

  function whyNotJoined(answer) {
    return answer.detail || "There is no meeting to join.";
  }

  function onPrimary() {
    if (!view || view.disabled) return;
    var button = $("primary-action");
    if (view.kind === "leave") {
      runAction(button, {
        action: "leave",
        ok: function () { return "Left the meeting. The dashboard is back on the TV."; },
        fail: "There was no meeting open to leave."
      });
      return;
    }
    runAction(button, {
      action: "join",
      body: view.meetingId ? { meeting_id: view.meetingId } : null,
      ok: joining,
      fail: whyNotJoined
    });
  }

  /* Delegated, because the list is rewritten on every poll and a listener per
     row would go with it. */
  function onMeetingClick(event) {
    var node = event.target;
    while (node && node !== this && node.getAttribute && !node.getAttribute("data-join")) {
      node = node.parentNode;
    }
    if (!node || node === this || !node.getAttribute || !node.getAttribute("data-join")) return;
    runAction(node, {
      action: "join",
      body: { meeting_id: node.getAttribute("data-join") },
      ok: joining,
      fail: whyNotJoined
    });
  }

  function volumeSaid(data) {
    return typeof data.volume === "number" ? "Volume " + data.volume + "%" : "Volume changed";
  }

  function onVolumeChange() {
    var level = Number($("volume-slider").value);
    volumeHeldUntil = Date.now() + VOLUME_HOLD_MS;
    R.post("/api/controller/action", { action: "volume_set", level: level })
      .then(function (data) {
        var answer = data || {};
        if (answer.ok === false) R.toast(answer.error || "The volume could not be changed.", "error");
        else R.toast("Volume " + level + "%", "ok");
      })
      .catch(handleError)
      .then(refreshNow);
  }

  function wire() {
    $("primary-action").addEventListener("click", onPrimary);

    $("mute-button").addEventListener("click", function () {
      runAction(this, {
        action: "mute",
        ok: function (data) { return data.muted ? "Microphone muted." : "Microphone is on."; },
        fail: "The room could not reach its microphone."
      });
    });

    $("camera-button").addEventListener("click", function () {
      runAction(this, {
        action: "camera",
        ok: function () { return "Camera switched — check the TV."; },
        fail: "The camera can only be switched during a meeting."
      });
    });

    $("volume-down").addEventListener("click", function () {
      volumeHeldUntil = 0;
      runAction(this, {
        action: "volume_down",
        ok: volumeSaid,
        fail: "The room could not reach its speaker."
      });
    });

    $("volume-up").addEventListener("click", function () {
      volumeHeldUntil = 0;
      runAction(this, {
        action: "volume_up",
        ok: volumeSaid,
        fail: "The room could not reach its speaker."
      });
    });

    $("home-button").addEventListener("click", function () {
      runAction(this, {
        action: "home",
        ok: function () { return "The dashboard is back on the TV."; },
        fail: "The dashboard could not be brought back."
      });
    });

    var slider = $("volume-slider");
    slider.addEventListener("input", function () {
      volumeHeldUntil = Date.now() + VOLUME_HOLD_MS;
      setText("volume-value", this.value + "%");
    });
    slider.addEventListener("change", onVolumeChange);

    $("meeting-list").addEventListener("click", onMeetingClick);

    wireAdminActions();

    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) refreshNow();
    });
  }

  /* The "Room setup" card is rendered only for a signed-in phone, so these
     buttons may not exist at all. They talk to /api/actions/*, not the
     controller API, because they are administrator actions. */
  function wireAdminActions() {
    var buttons = document.querySelectorAll("[data-restart]");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener("click", onRestartClick);
    }
  }

  function onRestartClick() {
    var button = this;
    var target = button.getAttribute("data-restart");
    if (button.disabled) return;
    var original = button.textContent;
    button.disabled = true;
    button.textContent = "Working…";

    R.post("/api/actions/restart", { target: target })
      .then(function (data) {
        R.toast((data && data.detail) || "Done.", "ok");
      })
      .catch(function (error) {
        R.toast(error.message || "That did not work.", "error");
      })
      .then(function () {
        button.disabled = false;
        button.textContent = original;
      });
  }

  // ---------------------------------------------------------------- polling

  function schedule(delay) {
    if (pollTimer) clearTimeout(pollTimer);
    if (stopped) return;
    pollTimer = setTimeout(tick, delay);
  }

  function tick() {
    // Nothing to look at while the phone is in a pocket: skip the request but
    // keep the loop alive so it picks up the moment the screen comes back.
    if (document.hidden) {
      schedule(HIDDEN_MS);
      return;
    }
    load();
  }

  function refreshNow() {
    if (stopped) return;
    load();
  }

  function load() {
    return R.get("/api/controller/state")
      .then(function (payload) {
        failures = 0;
        pollDelay = POLL_MS;
        show($("reconnect"), false);
        setLink(true);
        render(payload);
      })
      .catch(function (error) {
        if (isPairingError(error)) {
          stopped = true;
          window.location.href = "/controller/locked";
          return;
        }
        failures++;
        // A failed poll must never blank the page: the last good render stays
        // exactly as it was, with a quiet note that we are trying again.
        if (failures >= 2) {
          show($("reconnect"), true);
          setLink(false);
        }
        pollDelay = Math.min(POLL_MAX_MS, Math.round(pollDelay * 1.6));
      })
      .then(function () { schedule(pollDelay); });
  }

  function init() {
    wire();
    setLink(false);
    setText("link-text", "Connecting…");
    load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
