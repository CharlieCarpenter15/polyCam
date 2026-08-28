/* Settings page: collects every field, posts them together, and shows any
   per-field errors the backend returns next to the field that caused them. */

(function () {
  "use strict";

  var R = window.Room;
  var form = document.getElementById("settings-form");
  var status = document.getElementById("save-status");
  var saveButton = document.getElementById("save-button");
  var dirty = false;

  function inputs() {
    return Array.prototype.slice.call(document.querySelectorAll("[data-setting]"));
  }

  function collect() {
    var payload = {};
    inputs().forEach(function (element) {
      if (element.disabled) return;              // environment-locked
      var key = element.getAttribute("data-setting");
      if (element.type === "checkbox") payload[key] = element.checked;
      else if (element.tagName === "TEXTAREA") payload[key] = element.value;
      else payload[key] = element.value;
    });
    return payload;
  }

  function clearErrors() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-error-for]"), function (el) {
      el.setAttribute("hidden", "hidden");
      el.textContent = "";
    });
    inputs().forEach(function (el) { el.classList.remove("invalid"); });
  }

  function showErrors(errors) {
    var first = null;
    Object.keys(errors).forEach(function (key) {
      var target = document.querySelector('[data-error-for="' + key + '"]');
      var input = document.querySelector('[data-setting="' + key + '"]');
      if (target) {
        target.textContent = errors[key];
        target.removeAttribute("hidden");
      }
      if (input) {
        input.classList.add("invalid");
        // Make sure the containing group is open so the message is visible.
        var group = input.closest("details");
        while (group) { group.open = true; group = group.parentElement.closest("details"); }
        if (!first) first = input;
      }
    });
    if (first) first.scrollIntoView({ behavior: "smooth", block: "center" });
    setStatus(Object.keys(errors).length + " setting(s) need attention", "error");
  }

  function setStatus(message, kind) {
    status.textContent = message;
    status.className = "save-status" + (kind ? " " + kind : "");
  }

  function save(event) {
    if (event) event.preventDefault();
    clearErrors();
    setStatus("Saving…");
    saveButton.disabled = true;

    R.request("/api/settings", { method: "POST", json: collect() })
      .then(function (data) {
        dirty = false;
        setStatus(data.detail || "Saved.", "ok");
        if (data.needs_backend_restart) {
          R.toast("The port changed — restart the room software from the control panel.", "error");
        } else if ((data.restarted || []).length) {
          R.toast("Saved and applied.", "ok");
        } else {
          R.toast("Saved.", "ok");
        }
        // Reload so redacted secrets and advisories reflect the new state.
        setTimeout(function () { window.location.reload(); }, 900);
      })
      .catch(function (error) {
        var errors = error.data && error.data.errors;
        if (errors) showErrors(errors);
        else setStatus(error.message || "Could not save", "error");
      })
      .then(function () { saveButton.disabled = false; });
  }

  form.addEventListener("submit", save);

  inputs().forEach(function (element) {
    element.addEventListener("input", function () {
      dirty = true;
      setStatus("Unsaved changes");
    });
    element.addEventListener("change", function () {
      dirty = true;
      setStatus("Unsaved changes");
    });
  });

  window.addEventListener("beforeunload", function (event) {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });

  document.getElementById("reset-keep").addEventListener("click", function () {
    if (!R.confirmAction("Reset every setting to its default?\n\nThe calendar link, " +
      "calendar source and admin PIN are kept.")) return;
    R.withButton(this, function () {
      return R.post("/api/settings/reset", { keep_calendar: true });
    }).then(function () { window.location.reload(); }).catch(function () {});
  });

  document.getElementById("reset-all").addEventListener("click", function () {
    if (!R.confirmAction("Reset EVERYTHING, including the calendar link and the admin PIN?" +
      "\n\nYou will have to set the room up again from the start.")) return;
    R.withButton(this, function () {
      return R.post("/api/settings/reset", { keep_calendar: false });
    }).then(function () { window.location.reload(); }).catch(function () {});
  });
})();
