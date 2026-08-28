/* Diagnostics: what the appliance can see, and why something is not working. */

(function () {
  "use strict";

  var R = window.Room;
  var logTimer = null;

  function $(id) { return document.getElementById(id); }

  function statusDot(status) {
    return '<span class="dot ' + R.escapeHtml(status || "unknown") + '"></span>';
  }

  function humanStatus(status) {
    return { ok: "OK", warning: "Check", error: "Problem", disabled: "Off", unknown: "Unknown" }[status] || status;
  }

  function kv(pairs) {
    return pairs.filter(function (pair) { return pair[1] !== undefined && pair[1] !== null && pair[1] !== ""; })
      .map(function (pair) {
        return "<dt>" + R.escapeHtml(pair[0]) + "</dt><dd>" + R.escapeHtml(String(pair[1])) + "</dd>";
      }).join("");
  }

  var COMPONENT_LABELS = {
    backend: "Room software", calendar: "Calendar", browser: "TV display (Chromium)",
    airplay: "AirPlay receiver", camera: "Camera", microphone: "Microphone",
    speaker: "Speaker", network: "Network"
  };

  function loadHealth() {
    return R.get("/api/health").then(function (health) {
      var components = health.components || {};
      var detail = {
        calendar: (health.calendar && (health.calendar.error || health.calendar.description)) || "",
        browser: (health.browser && health.browser.current_url) || "",
        airplay: (health.airplay && health.airplay.name) ? ("advertising “" + health.airplay.name + "”") : "",
        camera: ((health.poly || {}).camera || {}).path || "",
        microphone: ((health.poly || {}).microphone || {}).description || "",
        speaker: ((health.poly || {}).speaker || {}).description || "",
        network: ((health.network || {}).addresses || []).join(", "),
        backend: "up " + Math.round((health.backend || {}).uptime_seconds / 60) + " min, v" +
          ((health.backend || {}).version || "?")
      };

      $("health-list").innerHTML = Object.keys(COMPONENT_LABELS).map(function (key) {
        var status = components[key] || "unknown";
        return '<li class="check">' + statusDot(status) +
          '<span class="check-name">' + R.escapeHtml(COMPONENT_LABELS[key]) + "</span>" +
          '<span class="check-value">' + R.escapeHtml(humanStatus(status)) +
          (detail[key] ? " · " + R.escapeHtml(detail[key]) : "") + "</span></li>";
      }).join("") +
        '<li class="check">' + statusDot(health.status) +
        '<span class="check-name"><strong>Overall</strong></span>' +
        '<span class="check-value">' + R.escapeHtml(humanStatus(health.status)) +
        " · mode: " + R.escapeHtml(health.mode || "?") + "</span></li>";

      var host = health.host || {};
      $("paths").innerHTML = kv([
        ["Mode", health.mode],
        ["Pi uptime", Math.round((host.uptime_seconds || 0) / 3600) + " h"],
        ["Load", (host.load || []).join(" / ")],
        ["Temperature", host.temperature_c ? host.temperature_c + " °C" : ""],
        ["Disk free", host.disk_free_percent ? host.disk_free_percent + " %" : ""],
        ["Memory free", host.memory_available_mb ? host.memory_available_mb + " MB" : ""],
        ["Hostname", (health.network || {}).hostname]
      ]);

      var units = health.units || {};
      $("units").innerHTML = Object.keys(units).map(function (unit) {
        var active = units[unit] === "active";
        var known = units[unit] !== "unknown";
        return '<li class="check">' +
          statusDot(active ? "ok" : (known ? "error" : "unknown")) +
          '<span class="check-name">' + R.escapeHtml(unit) + "</span>" +
          '<span class="check-value">' + R.escapeHtml(units[unit]) + "</span></li>";
      }).join("");

      // Recent self-repairs are the most useful thing in a support call.
      if ((health.recoveries || []).length) {
        var rows = health.recoveries.slice(-5).reverse().map(function (entry) {
          return '<li class="check">' + statusDot("warning") +
            '<span class="check-name">' + R.escapeHtml(entry.component) + "</span>" +
            '<span class="check-value">' + R.escapeHtml(entry.action) + "</span></li>";
        }).join("");
        $("units").innerHTML += '<li class="check"><span class="check-name" style="color:var(--text-faint)">' +
          "Recent automatic repairs</span></li>" + rows;
      }
      return health;
    });
  }

  function loadDiagnostics() {
    return R.get("/api/diagnostics").then(function (data) {
      var poly = data.poly || {};
      $("poly-summary").innerHTML = kv([
        ["USB device", poly.usb_present ? (poly.usb_name || "detected") : "not detected"],
        ["Match words", (poly.match_words || []).join(", ")],
        ["Missing tools", (poly.tools_missing || []).join(", ") || "none"]
      ]);

      $("poly-lists").innerHTML =
        deviceList("Cameras", (poly.cameras || []).map(function (camera) {
          return { label: camera.name || camera.path, value: camera.path, matched: camera.matched };
        })) +
        deviceList("Microphones", (poly.sources || []).map(function (source) {
          return { label: source.description || source.name, value: source.name,
                   matched: source.matched, isDefault: source.is_default };
        })) +
        deviceList("Speakers", (poly.sinks || []).map(function (sink) {
          return { label: sink.description || sink.name, value: sink.name,
                   matched: sink.matched, isDefault: sink.is_default };
        }));

      var remote = data.remote || {};
      var remoteStatus = remote.status || {};
      $("remote-summary").innerHTML = kv([
        ["evdev installed", remoteStatus.available ? "yes" : "no (install python3-evdev)"],
        ["Enabled", remoteStatus.enabled ? "yes" : "no"],
        ["Watching", (remoteStatus.devices || []).join(", ") || "nothing"],
        ["Last button", remoteStatus.last_key || "none yet"],
        ["Last action", remoteStatus.last_action || "none yet"],
        ["Presses seen", remoteStatus.presses],
        ["Problem", remoteStatus.error]
      ]);

      var mappings = remote.mappings || {};
      $("remote-devices").innerHTML =
        "<p class=\"field-help\">Current mapping</p><div class=\"chip-list\">" +
        (Object.keys(mappings).length
          ? Object.keys(mappings).map(function (key) {
              return '<span class="chip">' + R.escapeHtml(key) + " &rarr; " +
                R.escapeHtml(mappings[key]) + "</span>";
            }).join("")
          : '<span class="field-help">Nothing mapped</span>') +
        "</div>" +
        ((remote.devices || []).length
          ? "<p class=\"field-help\" style=\"margin-top:12px\">Input devices</p><div class=\"chip-list\">" +
            remote.devices.map(function (device) {
              return '<span class="chip">' + R.escapeHtml(device.name) +
                (device.likely_remote ? " ★" : "") + "</span>";
            }).join("") + "</div>"
          : "");

      var browser = data.browser || {};
      var lastJoin = browser.last_join || {};
      $("join-summary").innerHTML = kv([
        ["Browser reachable", browser.alive ? "yes" : "no"],
        ["Debug port", browser.debug_port],
        ["Showing", browser.current_url || "unknown"],
        ["Should be showing", browser.target],
        ["Last join provider", lastJoin.provider],
        ["Buttons pressed", (lastJoin.clicks || []).join(" → ")],
        ["Reached the call", lastJoin.in_call === undefined ? "" : (lastJoin.in_call ? "yes" : "no")],
        ["Automation passes", lastJoin.passes],
        ["Gave up", lastJoin.gave_up === undefined ? "" : (lastJoin.gave_up ? "yes" : "no")],
        ["Last problem", lastJoin.error]
      ]);

      var flows = data.join_flows || {};
      $("join-flows").innerHTML = Object.keys(flows).map(function (provider) {
        var flow = flows[provider];
        return '<p class="field-help" style="margin-bottom:4px"><strong>' +
          R.escapeHtml(provider) + "</strong> — " + R.escapeHtml(flow.notes || "") + "</p>" +
          '<div class="chip-list" style="margin-bottom:12px">' +
          (flow.priority_texts || []).map(function (text) {
            return '<span class="chip">' + R.escapeHtml(text) + "</span>";
          }).join("") + "</div>";
      }).join("");

      var paths = data.paths || {};
      $("paths").innerHTML += kv([
        ["Configuration", data.config_file],
        ["Working files", paths.var],
        ["Browser profile", paths.profile],
        ["Calendar cache", paths.cache]
      ]);
      return data;
    });
  }

  function deviceList(title, items) {
    if (!items.length) {
      return '<p class="field-help"><strong>' + R.escapeHtml(title) + ":</strong> none found</p>";
    }
    return '<p class="field-help" style="margin-bottom:4px"><strong>' + R.escapeHtml(title) +
      "</strong></p><ul class=\"check-list\" style=\"margin-bottom:12px\">" +
      items.map(function (item) {
        var flags = [];
        if (item.matched) flags.push("matches the bar");
        if (item.isDefault) flags.push("system default");
        return '<li class="check">' + statusDot(item.matched ? "ok" : "unknown") +
          '<span class="check-name">' + R.escapeHtml(item.label || "") + "</span>" +
          '<span class="check-value">' + R.escapeHtml(item.value || "") +
          (flags.length ? " · " + flags.join(", ") : "") + "</span></li>";
      }).join("") + "</ul>";
  }

  function loadLogs() {
    var unit = $("log-unit").value;
    var query = "/api/logs?lines=300" + (unit ? "&unit=" + encodeURIComponent(unit) : "");
    return R.get(query).then(function (data) {
      var output = $("log-output");
      output.textContent = data.text || "No log entries.";
      output.scrollTop = output.scrollHeight;
    }).catch(function (error) {
      $("log-output").textContent = "Could not read the log: " + error.message;
    });
  }

  function refreshAll() {
    return Promise.all([loadHealth(), loadDiagnostics()])
      .catch(function (error) { R.toast(error.message, "error"); });
  }

  $("refresh").addEventListener("click", function () {
    R.withButton(this, refreshAll).catch(function () {});
  });

  $("load-logs").addEventListener("click", function () {
    R.withButton(this, loadLogs).catch(function () {});
  });

  $("log-unit").addEventListener("change", loadLogs);

  $("log-auto").addEventListener("change", function () {
    if (logTimer) { clearInterval(logTimer); logTimer = null; }
    if (this.checked) { loadLogs(); logTimer = setInterval(loadLogs, 5000); }
  });

  $("capture").addEventListener("click", function () {
    var button = this;
    $("capture-status").textContent = "Listening — press a button on the remote now…";
    $("captured-keys").innerHTML = "";
    R.withButton(button, function () {
      return R.post("/api/diagnostics/capture-remote", { seconds: 10 });
    }).then(function (data) {
      if (!data.ok) {
        $("capture-status").textContent = data.error || "Could not listen for buttons.";
        return;
      }
      var keys = data.keys || [];
      $("capture-status").textContent = keys.length
        ? "Copy a name below into the matching field in Settings."
        : "No buttons were seen. Is the remote paired and is the right device selected?";
      $("captured-keys").innerHTML = keys.map(function (entry) {
        return '<span class="chip">' + R.escapeHtml(entry.key) + " · " +
          R.escapeHtml(entry.device) + "</span>";
      }).join("");
    }).catch(function (error) {
      $("capture-status").textContent = error.message;
    });
  });

  refreshAll();
  setInterval(function () { if (!document.hidden) loadHealth(); }, 15000);
})();
