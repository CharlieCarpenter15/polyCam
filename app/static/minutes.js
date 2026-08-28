/* Meeting minutes: the status of the feature, the people the room may
   recognise, the meetings it has written up, and two buttons that prove the
   plumbing works.

   Same shape as panel.js — one IIFE, no framework, no build step — because
   this page ships to the same Raspberry Pi and is read by the same person.

   One thing deliberately absent: getUserMedia. A browser will not open a
   microphone over plain HTTP on a LAN address, and even where it would, the
   phone's microphone is not the one that has to recognise somebody during a
   meeting. The appliance records the sample through the room's own far-field
   microphone instead, which is why "Record voice" is a request to the server
   that takes as long as the sample does. */

(function () {
  "use strict";

  var R = window.Room;
  var STATUS_POLL_MS = 5000;

  var people = [];
  var limits = { photo_mb: 8, min_sample_seconds: 5, max_sample_seconds: 30,
                 default_sample_seconds: 15 };
  var openSessionId = "";
  /* The elapsed time the server last reported, and when it said so, so the
     seconds keep counting between polls instead of jumping every five. */
  var recordingBase = null;
  var photoTarget = "";

  function $(id) { return document.getElementById(id); }

  function show(el, visible) {
    if (!el) return;
    if (visible) el.removeAttribute("hidden"); else el.setAttribute("hidden", "hidden");
  }

  function esc(text) { return R.escapeHtml(text); }

  function pad2(value) { return value < 10 ? "0" + value : String(value); }

  function clock(totalSeconds) {
    var total = Math.max(0, Math.round(totalSeconds));
    var hours = Math.floor(total / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    var seconds = total % 60;
    if (hours) return hours + ":" + pad2(minutes) + ":" + pad2(seconds);
    return minutes + ":" + pad2(seconds);
  }

  function when(iso) {
    var date = new Date(iso);
    if (!iso || isNaN(date.getTime())) return "";
    var day = date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
    var time = date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    return day + ", " + time;
  }

  function timeOnly(iso) {
    var date = new Date(iso);
    if (!iso || isNaN(date.getTime())) return "—";
    return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }

  function plural(count, one, many) {
    return count + " " + (count === 1 ? one : many);
  }

  function findRow(container, selector, attribute, value) {
    /* Attribute values here are people's names and speaker labels, which may
       contain quotes; comparing in JavaScript avoids having to escape them
       into a selector. */
    var rows = container.querySelectorAll(selector);
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].getAttribute(attribute) === value) return rows[i];
    }
    return null;
  }

  // -------------------------------------------------------------- 1 · status

  function loadStatus() {
    return R.get("/api/minutes/status").then(renderStatus).catch(function (error) {
      $("minutes-state").textContent = "Unavailable";
      $("minutes-pill").className = "pill error";
      $("minutes-dot").className = "dot error";
      var box = $("status-error");
      box.textContent = error.message || "The status could not be read.";
      show($("status-error"), true);
    });
  }

  function renderStatus(data) {
    var recording = data.recording;
    var pill = $("minutes-pill");
    var busy = !!recording;

    pill.className = "pill " + (busy ? "warning" : "ok");
    $("minutes-dot").className = "dot " + (busy ? "warning" : "ok");
    $("minutes-state").textContent = busy ? "Recording" : "Watching for meetings";

    show($("recording-row"), busy);
    show($("idle-note"), !busy);
    if (recording) {
      $("recording-title").textContent = recording.title || "Meeting";
      // Anything the recorder has had to work around, while there is still
      // time to do something about it. Finding out after the meeting that the
      // far end was silent for twenty minutes is finding out too late.
      var trouble = (recording.notices || []).join(" ");
      $("recording-sub").textContent = trouble ||
        ("Session " + (recording.session_id || ""));
      $("recording-sub").className = trouble ? "block-note mn-note-warn" : "block-note";
      recordingBase = { seconds: Number(recording.seconds) || 0, at: Date.now() };
      tickClock();
    } else {
      recordingBase = null;
    }

    var working = data.working_on || "";
    var queued = Number(data.queued) || 0;
    show($("worker-row"), !!working || queued > 0);
    $("worker-text").textContent = working
      ? working + (queued ? " (" + plural(queued, "meeting", "meetings") + " waiting)" : "")
      : plural(queued, "meeting", "meetings") + " waiting";

    var lastError = data.last_error || "";
    show($("status-error"), !!lastError);
    if (lastError) {
      $("status-error").textContent = "The last attempt failed: " + lastError;
    }

    limits = data.limits || limits;
    renderCapabilities(data);
    renderDependencies(data.dependencies || []);
    renderEngines(data.engines);

    var stats = data.people || {};
    $("people-count").textContent = (stats.people || 0) + " enrolled · " +
      (stats.with_face || 0) + " with a face · " + (stats.with_voice || 0) + " with a voice";
  }

  /* Capability names in the order somebody would work through them: can it
     hear the room, can it write the words down, can it work out who spoke,
     can it summarise, can it send. */
  var CAPABILITIES = [
    ["audio", "Record the room"],
    ["transcribe", "Write down what was said"],
    ["roster", "Read the meeting window for remote names"],
    ["faces", "Recognise faces (experimental)"],
    ["voices", "Recognise voices (experimental)"],
    ["summary", "Write the summary with Claude"],
    ["email", "Email the summary"]
  ];

  function checkRow(name, ok, why) {
    /* The reason is the valuable half of this page. It is written to be acted
       on — "… is not installed. Install it with: pip install …" — so it is
       printed in full underneath rather than truncated into a column. */
    var badge = ok
      ? '<span class="mn-badge mn-badge-ok">ready</span>'
      : '<span class="mn-badge mn-badge-off">unavailable</span>';
    var reason = (!ok && why) ? '<p class="mn-why">' + esc(why) + "</p>" : "";
    return '<li class="check mn-check">' +
      '<span class="mn-check-top"><span class="check-name">' + esc(name) + "</span>" +
      badge + "</span>" + reason + "</li>";
  }

  function renderCapabilities(data) {
    var capabilities = data.capabilities || {};
    var html = CAPABILITIES.map(function (row) {
      var capability = capabilities[row[0]] || {};
      return checkRow(row[1], !!capability.ok, capability.detail || "");
    }).join("");
    $("capability-list").innerHTML = html ||
      '<li class="check"><span class="check-name">Nothing to report.</span></li>';
  }

  function renderDependencies(list) {
    var html = list.map(function (item) {
      return checkRow(item.name, !!item.ok, item.detail || "");
    }).join("");
    $("dependency-list").innerHTML = html ||
      '<li class="check"><span class="check-name">Nothing to report.</span></li>';
  }

  function renderEngines(engines) {
    var target = $("engine-list");
    if (!engines) { target.innerHTML = ""; return; }
    /* engine_report() may be a list of rows or a name-keyed object; both are
       reasonable shapes and neither is worth a second endpoint to normalise.
       Whichever one is actually in use is worth saying — "auto" is the default
       setting, and it is otherwise impossible to tell what auto chose. */
    var rows = [];
    if (Object.prototype.toString.call(engines) === "[object Array]") {
      rows = engines.map(function (item) {
        return [item.name || item.engine || "engine", !!item.ok,
                item.detail || item.why || "", !!item.chosen];
      });
    } else {
      rows = Object.keys(engines).map(function (key) {
        var item = engines[key] || {};
        if (typeof item !== "object") return [key, !!item, "", false];
        return [key, !!item.ok, item.detail || item.why || "", !!item.chosen];
      });
    }
    target.innerHTML = rows.map(function (row) {
      return checkRow(row[0] + (row[3] ? " — in use" : ""), row[1], row[2]);
    }).join("");
  }

  function tickClock() {
    if (!recordingBase) return;
    var elapsed = recordingBase.seconds + (Date.now() - recordingBase.at) / 1000;
    $("recording-elapsed").textContent = clock(elapsed);
  }

  // -------------------------------------------------------------- 2 · people

  function loadPeople() {
    return R.get("/api/minutes/people").then(function (data) {
      people = data.people || [];
      renderPeople();
      return data;
    });
  }

  function renderPeople() {
    var list = $("people-list");
    show($("people-empty"), people.length === 0);
    if (!people.length) {
      list.innerHTML = "";
      $("people-empty").textContent =
        "Nobody is enrolled yet. Add somebody above, or name a speaker in a " +
        "transcript further down — that enrols them from their own voice.";
      return;
    }

    list.innerHTML = people.map(function (person) {
      var photos = (person.photos || []).map(function (index) {
        return '<img class="mn-photo" alt="" loading="lazy" src="/api/minutes/people/' +
          encodeURIComponent(person.id) + "/photo/" + index + '">';
      }).join("");
      if (!photos) {
        photos = '<span class="mn-photo-blank" aria-hidden="true">' +
          esc((person.name || "?").slice(0, 1).toUpperCase()) + "</span>";
      }

      var sub = [];
      if (person.email) sub.push(person.email);
      if (person.notes) sub.push(person.notes);

      var faces = Number(person.faces) || 0;
      var voices = Number(person.voices) || 0;
      var tags =
        '<span class="mn-tag ' + (faces ? "mn-tag-on" : "mn-tag-off") + '">' +
          (faces ? plural(faces, "face sample", "face samples") : "no face samples") + "</span>" +
        '<span class="mn-tag ' + (voices ? "mn-tag-on" : "mn-tag-off") + '">' +
          (voices ? plural(voices, "voice sample", "voice samples") : "no voice samples") + "</span>";

      var seconds = [10, 15, 20, 30].filter(function (value) {
        return value >= (limits.min_sample_seconds || 5) &&
               value <= (limits.max_sample_seconds || 30);
      });
      var options = seconds.map(function (value) {
        var chosen = value === (limits.default_sample_seconds || 15) ? " selected" : "";
        return '<option value="' + value + '"' + chosen + ">" + value + " s</option>";
      }).join("");

      return '<div class="mn-person" data-person="' + esc(person.id) + '">' +
        '<div class="mn-person-head">' +
          '<div class="mn-photos">' + photos + "</div>" +
          '<div class="mn-person-text">' +
            '<span class="mn-person-name">' + esc(person.name) + "</span>" +
            '<span class="mn-person-sub">' + esc(sub.join(" · ")) + "</span>" +
          "</div>" +
        "</div>" +
        '<div class="mn-tags">' + tags + "</div>" +
        '<div class="mn-actions">' +
          '<button class="btn btn-small" data-act="photo">Add photo</button>' +
          '<select data-seconds aria-label="How long to record">' + options + "</select>" +
          '<button class="btn btn-small" data-act="voice">Record voice</button>' +
          '<button class="btn btn-small btn-quiet" data-act="edit">Edit</button>' +
          '<button class="btn btn-small btn-quiet" data-act="clear-face"' +
            (faces ? "" : " disabled") + ">Forget faces</button>" +
          '<button class="btn btn-small btn-quiet" data-act="clear-voice"' +
            (voices ? "" : " disabled") + ">Forget voice</button>" +
          '<button class="btn btn-small btn-danger" data-act="delete">Delete</button>' +
        "</div>" +
        '<div class="mn-edit" data-edit hidden>' +
          '<div class="field"><label class="field-label">Name</label>' +
            '<input type="text" data-field="name" value="' + esc(person.name) + '"></div>' +
          '<div class="field"><label class="field-label">Email address</label>' +
            '<input type="text" data-field="email" inputmode="email" value="' +
            esc(person.email || "") + '"></div>' +
          '<div class="field"><label class="field-label">Notes</label>' +
            '<input type="text" data-field="notes" value="' + esc(person.notes || "") + '"></div>' +
          '<div class="btn-row">' +
            '<button class="btn btn-small btn-primary" data-act="save">Save</button>' +
            '<button class="btn btn-small btn-quiet" data-act="cancel">Cancel</button>' +
          "</div>" +
        "</div>" +
      "</div>";
    }).join("");
  }

  function personCard(element) {
    var card = element.closest("[data-person]");
    return card ? { card: card, id: card.getAttribute("data-person") } : null;
  }

  function onPeopleClick(event) {
    var button = event.target.closest("[data-act]");
    if (!button) return;
    var found = personCard(button);
    if (!found) return;
    var action = button.getAttribute("data-act");
    var url = "/api/minutes/people/" + encodeURIComponent(found.id);

    if (action === "photo") {
      photoTarget = found.id;
      $("photo-input").click();
      return;
    }

    if (action === "voice") {
      var select = found.card.querySelector("[data-seconds]");
      var seconds = Number(select ? select.value : limits.default_sample_seconds) || 15;
      R.toast("Recording for " + seconds + " seconds — speak now.");
      R.withButton(button, function () {
        button.textContent = "Recording…";
        return R.post(url + "/voice", { seconds: seconds });
      }).then(refreshPeopleAndStatus).catch(function () {});
      return;
    }

    if (action === "edit" || action === "cancel") {
      var panel = found.card.querySelector("[data-edit]");
      show(panel, action === "edit" && panel.hasAttribute("hidden"));
      return;
    }

    if (action === "save") {
      var fields = {};
      Array.prototype.forEach.call(
        found.card.querySelectorAll("[data-field]"),
        function (input) { fields[input.getAttribute("data-field")] = input.value; }
      );
      R.withButton(button, function () { return R.post(url, fields); })
        .then(refreshPeopleAndStatus).catch(function () {});
      return;
    }

    if (action === "clear-face" || action === "clear-voice") {
      var kind = action === "clear-face" ? "face" : "voice";
      if (!R.confirmAction(
        "Forget every " + kind + " sample for this person?\n\n" +
        "The profile stays; only the biometric samples are deleted."
      )) return;
      R.withButton(button, function () { return R.post(url + "/clear", { kind: kind }); })
        .then(refreshPeopleAndStatus).catch(function () {});
      return;
    }

    if (action === "delete") {
      var name = found.card.querySelector(".mn-person-name").textContent;
      if (!R.confirmAction(
        "Delete " + name + "?\n\n" +
        "Their face and voice samples are deleted permanently and cannot be " +
        "recovered. Transcripts already written keep their name."
      )) return;
      R.withButton(button, function () { return R.del(url); })
        .then(refreshPeopleAndStatus).catch(function () {});
    }
  }

  function refreshPeopleAndStatus() {
    return loadPeople().then(loadStatus);
  }

  function uploadPhoto(file) {
    if (!file || !photoTarget) return;
    var form = new FormData();
    form.append("photo", file);
    R.toast("Reading the photo…");
    fetch("/api/minutes/people/" + encodeURIComponent(photoTarget) + "/photo", {
      method: "POST",
      body: form,
      headers: { "X-Room-Token": R.csrf, "Accept": "application/json" },
      credentials: "same-origin"
    }).then(function (response) {
      return response.json().catch(function () { return {}; });
    }).then(function (data) {
      if (data && data.ok) R.toast(data.detail || "Photo added.", "ok");
      else R.toast((data && data.error) || "That photo could not be used.", "error");
      return refreshPeopleAndStatus();
    }).catch(function () {
      R.toast("That photo could not be uploaded.", "error");
    });
  }

  function addPerson(button) {
    var name = $("person-name").value.trim();
    if (!name) {
      R.toast("A name is required.", "error");
      $("person-name").focus();
      return;
    }
    R.withButton(button, function () {
      return R.post("/api/minutes/people", {
        name: name,
        email: $("person-email").value.trim(),
        notes: $("person-notes").value.trim()
      });
    }).then(function () {
      $("person-name").value = "";
      $("person-email").value = "";
      $("person-notes").value = "";
      return refreshPeopleAndStatus();
    }).catch(function () {});
  }

  // ------------------------------------------------------------ 3 · meetings

  /* Only "sent" is green. "summarised" means the words exist and nothing has
     left the appliance yet, which is not the same thing and should not look
     like it. */
  var STAGE_CLASS = {
    sent: "mn-stage-sent",
    failed: "mn-stage-failed",
    recording: "mn-stage-busy",
    captured: "mn-stage-busy",
    transcribed: "mn-stage-busy"
  };

  function loadSessions() {
    return R.get("/api/minutes/sessions").then(function (data) {
      renderSessions(data.sessions || []);
      var days = Number(data.keep_days) || 0;
      var audio = Number(data.keep_audio_days) || 0;
      $("retention-note").textContent =
        "Meetings are deleted from this appliance after " + plural(days, "day", "days") +
        ". " + (audio
          ? "The audio is kept for " + plural(audio, "day", "days") + "."
          : "The audio is deleted as soon as it has been transcribed.");
      return data;
    });
  }

  function renderSessions(rows) {
    var list = $("session-list");
    show($("session-empty"), rows.length === 0);
    if (!rows.length) {
      list.innerHTML = "";
      $("session-empty").textContent =
        "No meetings have been recorded yet. One is recorded automatically when " +
        "the room joins a meeting.";
      return;
    }

    list.innerHTML = rows.map(function (row) {
      var provider = row.provider || "";
      var chip = provider
        ? '<span class="provider-chip ' + esc(provider) + '"></span>'
        : "";
      var bits = [];
      if (provider) bits.push(provider);
      if (row.has_summary) bits.push("summary written");
      else bits.push("no summary");
      if (row.sent_to) bits.push("emailed to " + row.sent_to);
      var stageClass = STAGE_CLASS[row.stage] || "";

      return '<li class="meeting-row" data-open="' + esc(row.session_id) + '">' +
        '<span class="meeting-when">' + esc(timeOnly(row.started_at)) + "</span>" +
        '<span class="meeting-main">' +
          '<span class="meeting-name">' + esc(row.title || "Meeting") + "</span>" +
          '<span class="meeting-sub">' + chip + esc(when(row.started_at)) + " · " +
            esc(bits.join(" · ")) + "</span>" +
        "</span>" +
        '<span class="mn-stage ' + stageClass + '">' + esc(row.stage || "") + "</span>" +
        '<button class="btn btn-small mn-open">Open</button>' +
      "</li>";
    }).join("");
  }

  function openSession(sessionId) {
    return R.get("/api/minutes/sessions/" + encodeURIComponent(sessionId))
      .then(function (data) {
        openSessionId = sessionId;
        if (data.people) { people = data.people; }
        // A plain link to a server route, so the browser saves the file itself
        // and the transcript never has to exist twice in the page.
        var base = "/api/minutes/sessions/" + encodeURIComponent(sessionId) + "/download";
        $("detail-download").setAttribute("href", base);
        $("detail-download-transcript").setAttribute("href", base + "?part=transcript");
        renderSession(data.session);
        show($("sessions-block"), false);
        show($("session-detail"), true);
        window.scrollTo(0, 0);
      })
      .catch(function (error) { R.toast(error.message, "error"); });
  }

  function closeSession() {
    openSessionId = "";
    show($("session-detail"), false);
    show($("sessions-block"), true);
  }

  function label(segment) {
    if (segment.speaker) return segment.speaker;
    return segment.track === "far-end" ? "Remote speaker" : "Room speaker";
  }

  function renderSession(session) {
    var meta = session.meta || {};
    var transcript = session.transcript || null;
    var segments = (transcript && transcript.segments) || [];

    $("detail-title").textContent = meta.title || "Meeting";

    var facts = [
      ["When", when(meta.started_at) + (meta.ended_at ? " – " + timeOnly(meta.ended_at) : "")],
      ["Where", meta.room || "—"],
      ["Provider", meta.provider || "—"],
      ["Stage", meta.stage || "—"],
      ["Lines", String(segments.length)]
    ];
    if (transcript && (transcript.notices || []).length) {
      facts.push(["Capture notes", transcript.notices.join(" ")]);
    }
    $("detail-facts").innerHTML = facts.map(function (row) {
      return "<dt>" + esc(row[0]) + "</dt><dd>" + esc(row[1]) + "</dd>";
    }).join("");

    show($("detail-error"), !!meta.error);
    if (meta.error) $("detail-error").textContent = meta.error;

    var summary = session.summary;
    var haveSummary = !!(summary && summary.ok && summary.text);
    $("detail-summary").textContent = haveSummary ? summary.text : "";
    show($("detail-summary"), haveSummary);
    show($("detail-summary-missing"), !haveSummary);
    if (!haveSummary) {
      $("detail-summary-missing").textContent = (summary && summary.error) ||
        "No summary has been written for this meeting yet.";
    }

    renderTranscript(segments);
    renderNaming(segments);

    var recipients = session.recipients || [];
    $("detail-recipients").innerHTML = recipients.map(function (address) {
      return '<span class="chip">' + esc(address) + "</span>";
    }).join("");

    var delivery = session.delivery;
    if (delivery && delivery.ok) {
      $("detail-delivery").textContent =
        "Sent to " + plural((delivery.sent_to || []).length, "address", "addresses") + ".";
    } else if (delivery && delivery.error) {
      $("detail-delivery").textContent = "Not sent: " + delivery.error;
    } else if (recipients.length) {
      $("detail-delivery").textContent = "Not sent yet.";
    } else {
      $("detail-delivery").textContent =
        "Nobody in this meeting has an email address on file, so there is " +
        "nowhere to send it. Add addresses under People.";
    }
  }

  function renderTranscript(segments) {
    var target = $("detail-transcript");
    show($("detail-transcript-empty"), segments.length === 0);
    show($("detail-transcript"), segments.length > 0);
    if (!segments.length) { target.innerHTML = ""; return; }

    var ordered = segments.slice().sort(function (a, b) {
      return (a.start || 0) - (b.start || 0);
    });

    target.innerHTML = ordered.map(function (segment) {
      var who = label(segment);
      var named = !!segment.person_id;
      var remote = segment.track === "far-end";
      var classes = "mn-line-who" + (named ? (remote ? " remote" : "") : " unknown");
      /* The control to say who this was sits on the line itself. Somebody
         reading a transcript realises they know the voice at the line, not in
         a panel above it, so that is where the answer is asked for. */
      var ask = named ? "" :
        '<button class="mn-who-btn" data-name-label="' + esc(who) + '">Who was this?</button>';
      var source = segment.source
        ? '<span class="mn-src">' + esc(segment.source) +
          (segment.confidence ? " " + Number(segment.confidence).toFixed(2) : "") + "</span>"
        : "";
      return '<div class="mn-line">' +
        '<span class="mn-line-time">' + esc(clock(segment.start || 0)) + "</span>" +
        '<span class="' + classes + '">' + esc(who) + "</span>" +
        ask + source +
        '<span class="mn-line-text">' + esc(segment.text || "") + "</span>" +
      "</div>";
    }).join("");
  }

  function renderNaming(segments) {
    /* One row per label that is not linked to an enrolled person. A segment
       can carry a name without a person_id — the meeting window said who was
       speaking but this room has never met them — and those are worth linking
       too, because that is what gives them an address to send the minutes to. */
    var counts = {};
    var order = [];
    segments.forEach(function (segment) {
      if (segment.person_id) return;
      var name = label(segment);
      if (!(name in counts)) { counts[name] = 0; order.push(name); }
      counts[name] += 1;
    });

    var target = $("naming-list");
    show($("naming-none"), order.length === 0);
    if (!order.length) { target.innerHTML = ""; return; }

    var options = '<option value="">Who was this?</option>' +
      people.map(function (person) {
        return '<option value="' + esc(person.id) + '">' + esc(person.name) + "</option>";
      }).join("") +
      '<option value="+">Somebody new…</option>';

    target.innerHTML = order.map(function (name) {
      return '<div class="mn-name-row" data-label="' + esc(name) + '">' +
        '<span class="mn-name-label">' + esc(name) +
          ' <span class="mn-name-count">· ' + plural(counts[name], "line", "lines") + "</span></span>" +
        '<select data-who aria-label="Who was this speaker">' + options + "</select>" +
        '<input class="mn-name-new" type="text" data-new placeholder="Their name" hidden>' +
        '<button class="btn btn-small btn-primary" data-relabel>Save</button>' +
      "</div>";
    }).join("");
  }

  function onNamingChange(event) {
    var select = event.target.closest("[data-who]");
    if (!select) return;
    var row = select.closest(".mn-name-row");
    var input = row.querySelector("[data-new]");
    var wantsNew = select.value === "+";
    show(input, wantsNew);
    if (wantsNew) input.focus();
  }

  function onNamingClick(event) {
    var button = event.target.closest("[data-relabel]");
    if (!button) return;
    var row = button.closest(".mn-name-row");
    var name = row.getAttribute("data-label");
    var select = row.querySelector("[data-who]");
    var chosen = select.value;

    if (!chosen) {
      R.toast("Choose who that was.", "error");
      select.focus();
      return;
    }

    if (chosen !== "+") {
      relabel(button, name, chosen);
      return;
    }

    var newName = row.querySelector("[data-new]").value.trim();
    if (!newName) {
      R.toast("Type their name.", "error");
      row.querySelector("[data-new]").focus();
      return;
    }
    /* Enrol and attribute in one gesture: creating the profile and then
       naming the speaker is two requests, but it is one decision. */
    R.withButton(button, function () {
      return R.post("/api/minutes/people", { name: newName }).then(function (data) {
        return R.post(
          "/api/minutes/sessions/" + encodeURIComponent(openSessionId) + "/relabel",
          { label: name, person_id: data.person.id }
        );
      });
    }).then(afterRelabel).catch(function () {});
  }

  function relabel(button, name, personId) {
    R.withButton(button, function () {
      return R.post(
        "/api/minutes/sessions/" + encodeURIComponent(openSessionId) + "/relabel",
        { label: name, person_id: personId }
      );
    }).then(afterRelabel).catch(function () {});
  }

  function afterRelabel(data) {
    if (data && data.session) renderSession(data.session);
    return loadPeople().then(loadStatus).then(loadSessions);
  }

  function onTranscriptClick(event) {
    var button = event.target.closest("[data-name-label]");
    if (!button) return;
    var wanted = button.getAttribute("data-name-label");
    var row = findRow($("naming-list"), ".mn-name-row", "data-label", wanted);
    if (!row) return;
    row.scrollIntoView({ block: "center" });
    var select = row.querySelector("[data-who]");
    if (select) select.focus();
  }

  // -------------------------------------------------------------- 4 · try it

  function sendTestEmail(button) {
    var address = $("test-email-to").value.trim();
    if (!address) {
      R.toast("Type an address to send the test to.", "error");
      $("test-email-to").focus();
      return;
    }
    R.withButton(button, function () {
      return R.post("/api/minutes/test-email", { to: address });
    }).catch(function () {});
  }

  function lookAtRoom(button) {
    $("look-result").textContent = "Looking…";
    R.withButton(button, function () { return R.post("/api/minutes/look"); })
      .then(function (data) {
        $("look-result").textContent = data.detail || "";
        return loadStatus();
      })
      .catch(function (error) {
        $("look-result").textContent = error.message || "The camera could not be read.";
      });
  }

  function mb(bytes) {
    var n = Number(bytes) || 0;
    if (!n) return "";
    return n < 1048576 ? Math.round(n / 1024) + " kB" : Math.round(n / 1048576) + " MB";
  }

  function renderModels(report) {
    var files = (report && report.files) || [];
    $("models-list").innerHTML = files.map(function (file) {
      var here = !!file.present;
      var size = mb(here ? file.bytes : file.expected_bytes);
      return '<li class="mn-model">' +
        '<span class="mn-model-name">' + R.escapeHtml(file.file || "") + "</span>" +
        '<span class="mn-model-what">' + R.escapeHtml(file.purpose || "") + "</span>" +
        '<span class="mn-model-size">' + R.escapeHtml(size) + "</span>" +
        '<span class="' + (here ? "mn-model-here" : "mn-model-missing") + '">' +
        (here ? "here" : "missing") + "</span></li>";
    }).join("");

    var missing = Number(report && report.missing) || 0;
    var button = $("models-install");
    button.textContent = missing
      ? "Download the " + plural(missing, "missing model", "missing models")
      : "Models are all here";
    button.disabled = !missing || !!(report && report.downloading);
    if (report && report.downloading) {
      button.textContent = "Downloading…";
      $("models-result").textContent =
        "Downloading. It keeps going even if you leave this page.";
    }
  }

  function loadModels() {
    return R.get("/api/minutes/models")
      .then(function (data) { renderModels(data.models); })
      .catch(function () { $("models-list").innerHTML = ""; });
  }

  function installModels(button) {
    R.withButton(button, function () { return R.post("/api/minutes/models/install"); })
      .then(function (data) {
        $("models-result").textContent = data.detail || "";
        // The unit outlives this request, so keep asking until the files land.
        var tries = 0;
        var poll = setInterval(function () {
          tries += 1;
          if (tries > 120) { clearInterval(poll); return; }
          loadModels();
        }, 5000);
      })
      .catch(function (error) {
        $("models-result").textContent = error.message || "The download could not be started.";
      });
  }

  function probeWindow(button) {
    $("probe-result").textContent = "Reading the meeting window…";
    R.withButton(button, function () { return R.post("/api/minutes/probe"); })
      .then(function (data) {
        var probe = data.probe || {};
        var lines = [data.detail || ""];
        if ((probe.participants || []).length) {
          lines.push("Saw: " + probe.participants.join(", "));
        }
        $("probe-result").textContent = lines.filter(Boolean).join(" ");
      })
      .catch(function (error) {
        $("probe-result").textContent =
          error.message || "The meeting window could not be read.";
      });
  }

  // ------------------------------------------------------------------- wiring

  function wire() {
    $("refresh-status").addEventListener("click", function () {
      R.withButton(this, function () {
        return loadStatus().then(function () { return { detail: "Checked." }; });
      }).catch(function () {});
    });

    $("person-add").addEventListener("click", function () { addPerson(this); });
    $("people-list").addEventListener("click", onPeopleClick);
    $("photo-input").addEventListener("change", function () {
      uploadPhoto(this.files && this.files[0]);
      this.value = "";
    });

    $("refresh-sessions").addEventListener("click", function () {
      R.withButton(this, function () {
        return loadSessions().then(function () { return { detail: "Refreshed." }; });
      }).catch(function () {});
    });

    $("session-list").addEventListener("click", function (event) {
      var row = event.target.closest("[data-open]");
      if (row) openSession(row.getAttribute("data-open"));
    });

    $("sweep-now").addEventListener("click", function () {
      if (!R.confirmAction(
        "Delete every meeting past its retention date now?\n\nThis cannot be undone."
      )) return;
      R.withButton(this, function () { return R.post("/api/minutes/sweep"); })
        .then(loadSessions).catch(function () {});
    });

    $("detail-back").addEventListener("click", closeSession);
    $("naming-list").addEventListener("change", onNamingChange);
    $("naming-list").addEventListener("click", onNamingClick);
    $("detail-transcript").addEventListener("click", onTranscriptClick);

    $("detail-reprocess").addEventListener("click", function () {
      R.withButton(this, function () {
        return R.post("/api/minutes/sessions/" + encodeURIComponent(openSessionId) + "/reprocess");
      }).then(loadSessions).catch(function () {});
    });

    $("detail-delete").addEventListener("click", function () {
      if (!R.confirmAction(
        "Delete this meeting?\n\nThe audio, the transcript and the summary are " +
        "removed from this appliance. Anything already emailed has left and is " +
        "not affected."
      )) return;
      R.withButton(this, function () {
        return R.del("/api/minutes/sessions/" + encodeURIComponent(openSessionId));
      }).then(function () { closeSession(); return loadSessions(); }).catch(function () {});
    });

    $("test-email-send").addEventListener("click", function () { sendTestEmail(this); });
    $("look-now").addEventListener("click", function () { lookAtRoom(this); });
    $("probe-window").addEventListener("click", function () { probeWindow(this); });
    $("models-install").addEventListener("click", function () { installModels(this); });
  }

  function init() {
    wire();
    loadStatus();
    loadModels();
    loadPeople().catch(function () {
      $("people-empty").textContent = "The people list could not be read.";
    });
    loadSessions().catch(function () {
      $("session-empty").textContent = "The meeting list could not be read.";
    });

    setInterval(function () {
      if (!document.hidden) loadStatus();
    }, STATUS_POLL_MS);
    /* The server only says how long a recording has been running when it is
       asked, so the seconds in between are counted here. */
    setInterval(tickClock, 1000);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) loadStatus();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
