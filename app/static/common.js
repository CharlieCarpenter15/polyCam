/* Shared helpers for the control panel, settings and diagnostics pages.
   No framework, no build step — one small global so each page stays readable. */

window.Room = (function () {
  "use strict";

  var csrf = document.body.getAttribute("data-csrf") || "";
  var toastTimer = null;

  function request(url, options) {
    var settings = options || {};
    settings.headers = settings.headers || {};
    settings.headers["Accept"] = "application/json";
    settings.credentials = "same-origin";
    settings.cache = "no-store";

    if (settings.method && settings.method !== "GET") {
      settings.headers["X-Room-Token"] = csrf;
      if (settings.json !== undefined) {
        settings.headers["Content-Type"] = "application/json";
        settings.body = JSON.stringify(settings.json);
        delete settings.json;
      }
    }

    return fetch(url, settings).then(function (response) {
      return response.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }

        if (response.status === 401 && data && data.needs_pin) {
          window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname);
          throw new Error("Signing in…");
        }
        if (data && data.reload) {
          // The page's token no longer matches the session.
          window.location.reload();
          throw new Error("Reloading…");
        }
        if (!response.ok) {
          var message = (data && (data.error || data.detail)) ||
            "Request failed (" + response.status + ")";
          var error = new Error(message);
          error.data = data;
          error.status = response.status;
          throw error;
        }
        return data || {};
      });
    });
  }

  function get(url) { return request(url); }
  function post(url, payload) { return request(url, { method: "POST", json: payload || {} }); }
  function del(url) { return request(url, { method: "DELETE", json: {} }); }

  function toast(message, kind) {
    var el = document.getElementById("toast");
    if (!el) { return; }
    el.textContent = message;
    el.className = "toast" + (kind ? " " + kind : "");
    el.removeAttribute("hidden");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.setAttribute("hidden", "hidden"); }, 5000);
  }

  /* Runs an action with a busy state on its button, so a slow restart cannot
     be triggered five times by an impatient tap. */
  function withButton(button, promiseFactory) {
    if (!button || button.disabled) return Promise.resolve();
    var originalText = button.textContent;
    button.disabled = true;
    button.classList.add("busy");
    return promiseFactory()
      .then(function (data) {
        if (data && data.detail) toast(data.detail, "ok");
        return data;
      })
      .catch(function (error) {
        toast(error.message || "That did not work", "error");
        throw error;
      })
      .then(
        function (data) { restore(); return data; },
        function (error) { restore(); return Promise.reject(error); }
      );

    function restore() {
      button.disabled = false;
      button.classList.remove("busy");
      button.textContent = originalText;
    }
  }

  function confirmAction(message) {
    return window.confirm(message);
  }

  function escapeHtml(text) {
    return String(text === undefined || text === null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function pad(value) { return value < 10 ? "0" + value : String(value); }

  function formatTime(iso, use24) {
    var date = new Date(iso);
    if (isNaN(date)) return "";
    var hours = date.getHours();
    var minutes = pad(date.getMinutes());
    if (use24) return pad(hours) + ":" + minutes;
    var meridiem = hours >= 12 ? "pm" : "am";
    var display = hours % 12;
    if (display === 0) display = 12;
    return display + ":" + minutes + meridiem;
  }

  function relativeStart(iso) {
    var date = new Date(iso);
    if (isNaN(date)) return "";
    var mins = (date.getTime() - Date.now()) / 60000;
    if (mins < -1) return "started " + Math.abs(Math.round(mins)) + " min ago";
    if (mins < 1) return "starting now";
    if (mins < 60) return "in " + Math.ceil(mins) + " min";
    var hours = Math.floor(mins / 60);
    return "in " + hours + " h " + Math.round(mins % 60) + " min";
  }

  return {
    csrf: csrf,
    get: get, post: post, del: del, request: request,
    toast: toast, withButton: withButton, confirmAction: confirmAction,
    escapeHtml: escapeHtml, formatTime: formatTime, relativeStart: relativeStart
  };
})();
