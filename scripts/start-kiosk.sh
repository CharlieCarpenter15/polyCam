#!/usr/bin/env bash
#
# Launch Chromium as the room's kiosk display.
#
# Run by room-kiosk.service. Waits for a graphical session and for the room
# backend to answer, then starts Chromium fullscreen on the dashboard. If
# Chromium exits for any reason, this script exits too and systemd restarts it.
#
# The browser profile lives under var/chromium-profile so signed-in room
# accounts, granted camera/microphone permissions and cookies survive reboots.
# The administrator's own profile is never touched.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"

# shellcheck source=scripts/lib-room.sh
. "$HERE/lib-room.sh"

room_load_config

PROFILE_DIR="${ROOM_APPLIANCE_PROFILE:-$ROOT/var/chromium-profile}"
DEBUG_PORT="$(room_config CHROMIUM_DEBUG_PORT 9222)"
DASHBOARD_PORT="$(room_config DASHBOARD_PORT 8080)"
BINARY_SETTING="$(room_config CHROMIUM_BINARY auto)"
EXTRA_ARGS="$(room_config CHROMIUM_EXTRA_ARGS '')"
HIDE_CURSOR="$(room_config HIDE_CURSOR true)"
ALLOW_BLANKING="$(room_config SCREEN_BLANKING false)"
KIOSK_ENABLED="$(room_config KIOSK_ENABLED true)"

if ! room_is_true "$KIOSK_ENABLED"; then
  room_log "kiosk.disabled_by_configuration"
  # Sleep rather than exit: a bare exit would have systemd restart us in a loop.
  exec sleep infinity
fi

# ---------------------------------------------------------------- find binary

find_chromium() {
  if [ "$BINARY_SETTING" != "auto" ] && [ -n "$BINARY_SETTING" ]; then
    command -v "$BINARY_SETTING" 2>/dev/null && return 0
    [ -x "$BINARY_SETTING" ] && { printf '%s\n' "$BINARY_SETTING"; return 0; }
    room_log "kiosk.configured_binary_missing" "binary=$BINARY_SETTING"
  fi
  for candidate in chromium-browser chromium google-chrome-stable google-chrome; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

CHROMIUM="$(find_chromium || true)"
if [ -z "$CHROMIUM" ]; then
  room_log "kiosk.chromium_not_found" "hint=sudo apt install chromium-browser"
  exit 1
fi

# --------------------------------------------------------- wait for a display

# On a fresh boot the graphical session may not be ready yet. Wait, rather than
# failing and being restarted: this is the normal path, not an error.
wait_for_display() {
  local waited=0
  while [ "$waited" -lt 90 ]; do
    if [ -n "${WAYLAND_DISPLAY:-}" ] && [ -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/${WAYLAND_DISPLAY}" ]; then
      room_log "kiosk.display_ready" "server=wayland" "display=$WAYLAND_DISPLAY"
      return 0
    fi
    if [ -n "${DISPLAY:-}" ] && command -v xset >/dev/null 2>&1 && xset q >/dev/null 2>&1; then
      room_log "kiosk.display_ready" "server=x11" "display=$DISPLAY"
      return 0
    fi
    # Discover a session we were not told about.
    for socket in "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"/wayland-*; do
      if [ -S "$socket" ]; then
        WAYLAND_DISPLAY="$(basename "$socket")"
        export WAYLAND_DISPLAY
        room_log "kiosk.display_found" "display=$WAYLAND_DISPLAY"
        return 0
      fi
    done
    if [ -z "${DISPLAY:-}" ] && [ -S /tmp/.X11-unix/X0 ]; then
      DISPLAY=":0"
      export DISPLAY
      room_log "kiosk.display_found" "display=:0"
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  room_log "kiosk.no_display" "waited=${waited}s"
  return 1
}

wait_for_display || exit 1

# --------------------------------------------------------- wait for backend

DASHBOARD_URL="http://127.0.0.1:${DASHBOARD_PORT}/"
room_wait_for_url "$DASHBOARD_URL" 60 || \
  room_log "kiosk.backend_slow" "note=starting anyway; the page retries by itself"

# ------------------------------------------------------------- housekeeping

mkdir -p "$PROFILE_DIR"

# Chromium shows an "restore pages?" bubble after an unclean exit, which would
# sit on the TV forever. Clearing these flags each start prevents it.
for state_file in "$PROFILE_DIR/Default/Preferences" "$PROFILE_DIR/Local State"; do
  [ -f "$state_file" ] || continue
  python3 - "$state_file" <<'PYEOF' 2>/dev/null || true
import json, sys
path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, ValueError):
    sys.exit(0)
changed = False
profile = data.get("profile")
if isinstance(profile, dict):
    for key, value in (("exit_type", "Normal"), ("exited_cleanly", True)):
        if profile.get(key) != value:
            profile[key] = value
            changed = True
if data.get("exit_type") not in (None, "Normal"):
    data["exit_type"] = "Normal"
    changed = True
if changed:
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    except OSError:
        pass
PYEOF
done

# Stop the screen blanking so the dashboard is always visible.
if ! room_is_true "$ALLOW_BLANKING"; then
  if command -v xset >/dev/null 2>&1 && [ -n "${DISPLAY:-}" ]; then
    xset s off -dpms s noblank 2>/dev/null || true
  fi
  # Wayland/labwc equivalent, if present.
  command -v wlr-randr >/dev/null 2>&1 && true
fi

if room_is_true "$HIDE_CURSOR" && command -v unclutter >/dev/null 2>&1 && [ -n "${DISPLAY:-}" ]; then
  pgrep -u "$(id -u)" -x unclutter >/dev/null 2>&1 || unclutter -idle 1 -root &
fi

# ------------------------------------------------------------------- launch

# Why each flag is here:
#   --kiosk / --start-fullscreen : no browser UI at all
#   --remote-debugging-*         : how the backend drives the browser; bound to
#                                  localhost so it is never reachable off the Pi
#   --autoplay-policy            : meeting audio must start without a click
#   --auto-accept-camera-...     : no permission prompt for a room with no mouse
#   --disable-session-crashed-*  : never show "restore pages?" on the TV
#   --password-store=basic       : no desktop keyring prompt on a headless login
# shellcheck disable=SC2054  # the commas belong inside --disable-features values
ARGS=(
  --kiosk
  --start-fullscreen
  --start-maximized
  --user-data-dir="$PROFILE_DIR"
  --remote-debugging-port="$DEBUG_PORT"
  --remote-debugging-address=127.0.0.1
  --remote-allow-origins="http://127.0.0.1:$DEBUG_PORT"
  --autoplay-policy=no-user-gesture-required
  --auto-accept-camera-and-microphone-capture
  --disable-session-crashed-bubble
  --disable-infobars
  --disable-features=Translate,TranslateUI,AutofillServerCommunication,MediaRouter
  --no-first-run
  --no-default-browser-check
  --disable-pinch
  --overscroll-history-navigation=0
  --hide-crash-restore-bubble
  --password-store=basic
  --check-for-update-interval=31536000
  --noerrdialogs
  --disable-notifications
  --enable-features=WebRTCPipeWireCapturer
)

if room_is_true "$HIDE_CURSOR"; then
  # Chromium on Wayland has no cursor-hiding flag; unclutter (X11) covers that
  # case above. This keeps the pointer out of the way where supported.
  ARGS+=(--enable-blink-features=)
fi

if [ -n "$EXTRA_ARGS" ]; then
  # shellcheck disable=SC2206  # deliberate word splitting of admin-supplied args
  read -r -a EXTRA_ARRAY <<< "$EXTRA_ARGS"
  ARGS+=("${EXTRA_ARRAY[@]}")
fi

ARGS+=("$DASHBOARD_URL")

room_log "kiosk.starting" "binary=$CHROMIUM" "port=$DEBUG_PORT" "profile=$PROFILE_DIR"

# exec so systemd supervises Chromium itself: if it dies, the unit restarts.
exec "$CHROMIUM" "${ARGS[@]}"
