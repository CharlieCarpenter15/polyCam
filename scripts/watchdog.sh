#!/usr/bin/env bash
#
# External watchdog. Run every minute by room-watchdog.timer.
#
# systemd already restarts a process that *exits*. This catches the case it
# cannot see: a process that is still running but has stopped working — a
# wedged Python thread, a Chromium that no longer answers, an X session that
# went away. It checks the appliance from the outside and escalates:
#
#   1. backend not answering        -> restart room-dashboard
#   2. browser not answering        -> restart room-kiosk
#   3. AirPlay unit not active      -> restart room-airplay
#   4. nothing works for N minutes  -> reboot (rate-limited, opt-out available)
#
# Deliberately simple and dependency-free: if this script is the last thing
# working, it must not need anything else to be working.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib-room.sh
. "$HERE/lib-room.sh"

room_load_config || true

ENABLED="$(room_config WATCHDOG_ENABLED true)"
REBOOT_ENABLED="$(room_config WATCHDOG_REBOOT_ENABLED true)"
REBOOT_AFTER="$(room_config WATCHDOG_REBOOT_AFTER_FAILURES 10)"
PORT="$(room_config DASHBOARD_PORT 8080)"
KIOSK_ENABLED="$(room_config KIOSK_ENABLED true)"
AIRPLAY_ENABLED="$(room_config AIRPLAY_ENABLED true)"
DEBUG_PORT="$(room_config CHROMIUM_DEBUG_PORT 9222)"

if ! room_is_true "$ENABLED"; then
  room_log "watchdog.disabled"
  exit 0
fi

STATE_DIR="${ROOM_APPLIANCE_VAR:-$ROOM_ROOT/var}"
FAIL_FILE="$STATE_DIR/watchdog-failures"
REBOOT_STAMP="$STATE_DIR/watchdog-last-reboot"
mkdir -p "$STATE_DIR" 2>/dev/null || true

read_count() { [ -r "$FAIL_FILE" ] && tr -dc '0-9' < "$FAIL_FILE" || printf '0'; }
write_count() { printf '%s' "$1" > "$FAIL_FILE" 2>/dev/null || true; }

systemctl_user() { systemctl --user "$@" 2>/dev/null; }

unit_active() { systemctl_user is-active --quiet "$1"; }

restart_unit() {
  local unit="$1"
  local reason="$2"
  room_log "watchdog.restarting" "unit=$unit" "reason=$reason"
  systemctl_user restart "$unit" && return 0
  room_log "watchdog.restart_failed" "unit=$unit"
  return 1
}

http_ok() {
  command -v curl >/dev/null 2>&1 || return 0   # cannot check; assume fine
  curl --silent --fail --max-time 5 --output /dev/null "$1"
}

# ------------------------------------------------------------- 1. backend

BACKEND_URL="http://127.0.0.1:${PORT}/api/health"
backend_ok=1
if ! http_ok "$BACKEND_URL"; then
  # A single miss can be a slow moment; confirm before acting.
  sleep 5
  if ! http_ok "$BACKEND_URL"; then
    backend_ok=0
  fi
fi

if [ "$backend_ok" -eq 0 ]; then
  count=$(( $(read_count) + 1 ))
  write_count "$count"
  room_log "watchdog.backend_unhealthy" "consecutive=$count"
  restart_unit room-dashboard.service "backend not answering"

  # --------------------------------------------------- 4. last resort: reboot
  if room_is_true "$REBOOT_ENABLED" && [ "$count" -ge "$REBOOT_AFTER" ]; then
    # Never reboot a Pi that has only just started: give it a chance first.
    uptime_seconds="$(cut -d. -f1 < /proc/uptime 2>/dev/null || echo 9999)"
    if [ "$uptime_seconds" -lt 600 ]; then
      room_log "watchdog.reboot_skipped" "reason=uptime_too_low" "uptime=${uptime_seconds}s"
      exit 0
    fi
    # And at most once an hour, so a hardware fault cannot cause a boot loop.
    now="$(date +%s)"
    last="$( [ -r "$REBOOT_STAMP" ] && tr -dc '0-9' < "$REBOOT_STAMP" || printf '0' )"
    if [ $((now - last)) -lt 3600 ]; then
      room_log "watchdog.reboot_skipped" "reason=rate_limited" "since=$((now - last))s"
      exit 0
    fi
    printf '%s' "$now" > "$REBOOT_STAMP" 2>/dev/null || true
    write_count 0
    room_log "watchdog.rebooting" "consecutive_failures=$count"
    sudo -n systemctl reboot 2>/dev/null || sudo -n /sbin/reboot 2>/dev/null || \
      room_log "watchdog.reboot_refused" "hint=check the sudo rule from install.sh"
  fi
  exit 0
fi

# The backend is fine: forget past failures.
[ "$(read_count)" != "0" ] && write_count 0

# ------------------------------------------------------------- 2. browser

if room_is_true "$KIOSK_ENABLED"; then
  if ! unit_active room-kiosk.service; then
    restart_unit room-kiosk.service "kiosk unit not active"
  elif ! http_ok "http://127.0.0.1:${DEBUG_PORT}/json/version"; then
    sleep 5
    if ! http_ok "http://127.0.0.1:${DEBUG_PORT}/json/version"; then
      restart_unit room-kiosk.service "browser not answering on the debug port"
    fi
  fi
fi

# ------------------------------------------------------------- 3. airplay

if room_is_true "$AIRPLAY_ENABLED" && command -v uxplay >/dev/null 2>&1; then
  unit_active room-airplay.service || restart_unit room-airplay.service "airplay unit not active"
fi

# ------------------------------------------------------------ 4. remote

if room_is_true "$(room_config POLY_REMOTE_ENABLED false)"; then
  unit_active room-remote.service || restart_unit room-remote.service "remote unit not active"
fi

room_log "watchdog.ok"
