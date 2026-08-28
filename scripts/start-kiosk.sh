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
RENDER_MODE="$(room_config CHROMIUM_RENDER_MODE auto)"
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
#
# Chromium's feature lists are built as single variables below. Passing
# --enable-features twice does NOT merge the two lists: the last one silently
# wins and the earlier features are lost.
ENABLE_FEATURES="WebRTCPipeWireCapturer"
DISABLE_FEATURES="Translate,TranslateUI,AutofillServerCommunication,MediaRouter"

# ------------------------------------------------------- performance profile
#
# Every default in this file was chosen for a Raspberry Pi. On a mini-PC or a
# NUC that leaves the GPU idle and the machine loafing, so the profile adds the
# flags that hardware can actually use. See app/hardware_profile.py; Chromium
# ignores flags and features it does not recognise, so an older build is safe.
PROFILE="$(room_config _PROFILE balanced)"
PROFILE_ARGS="$(room_config _CHROMIUM_ARGS '')"
PROFILE_ENABLE="$(room_config _CHROMIUM_ENABLE '')"
PROFILE_DISABLE="$(room_config _CHROMIUM_DISABLE '')"

[ -n "$PROFILE_ENABLE" ] && ENABLE_FEATURES="$ENABLE_FEATURES,$PROFILE_ENABLE"
[ -n "$PROFILE_DISABLE" ] && DISABLE_FEATURES="$DISABLE_FEATURES,$PROFILE_DISABLE"

room_log "kiosk.performance_profile" "profile=$PROFILE" \
  "machine=$(room_config _MACHINE unknown)"

ARGS=(
  --kiosk
  --start-fullscreen
  --user-data-dir="$PROFILE_DIR"
  --remote-debugging-port="$DEBUG_PORT"
  --remote-debugging-address=127.0.0.1
  --remote-allow-origins="http://127.0.0.1:$DEBUG_PORT"
  --autoplay-policy=no-user-gesture-required
  --auto-accept-camera-and-microphone-capture
  --disable-session-crashed-bubble
  --disable-infobars
  --no-first-run
  --no-default-browser-check
  --disable-pinch
  --overscroll-history-navigation=0
  --hide-crash-restore-bubble
  --password-store=basic
  --check-for-update-interval=31536000
  --noerrdialogs
  --disable-notifications
)

# ------------------------------------------------- renderer and compositor
#
# A white or blank kiosk window is almost always Chromium and the compositor
# disagreeing rather than anything wrong with the page. Raspberry Pi OS
# Bookworm runs labwc (Wayland) by default on a Pi 5, and Chromium has to be
# told to use it or it can open a window that never paints.
#
# "software" is the last resort: slower, no GPU, but it always draws something.
case "$RENDER_MODE" in
  wayland)
    ARGS+=(--ozone-platform=wayland)
    ENABLE_FEATURES="$ENABLE_FEATURES,UseOzonePlatform"
    room_log "kiosk.render_mode" "mode=wayland"
    ;;
  x11)
    ARGS+=(--ozone-platform=x11)
    room_log "kiosk.render_mode" "mode=x11"
    ;;
  software)
    ARGS+=(--disable-gpu --disable-gpu-compositing)
    room_log "kiosk.render_mode" "mode=software" "note=no GPU acceleration"
    ;;
  *)
    # auto: let Chromium pick, but tell it Wayland exists when it does.
    if [ -n "${WAYLAND_DISPLAY:-}" ]; then
      ARGS+=(--ozone-platform-hint=auto)
      room_log "kiosk.render_mode" "mode=auto" "detected=wayland"
    else
      room_log "kiosk.render_mode" "mode=auto" "detected=x11"
    fi
    ;;
esac

if [ -n "$PROFILE_ARGS" ]; then
  # shellcheck disable=SC2206  # deliberate word splitting: a flag list
  read -r -a PROFILE_ARRAY <<< "$PROFILE_ARGS"
  ARGS+=("${PROFILE_ARRAY[@]}")
fi

ARGS+=("--enable-features=$ENABLE_FEATURES")
ARGS+=("--disable-features=$DISABLE_FEATURES")

if [ -n "$EXTRA_ARGS" ]; then
  # shellcheck disable=SC2206  # deliberate word splitting of admin-supplied args
  read -r -a EXTRA_ARRAY <<< "$EXTRA_ARGS"
  ARGS+=("${EXTRA_ARRAY[@]}")
fi

ARGS+=("$DASHBOARD_URL")

room_log "kiosk.starting" "binary=$CHROMIUM" "port=$DEBUG_PORT" "profile=$PROFILE_DIR"

# exec so systemd supervises Chromium itself: if it dies, the unit restarts.
exec "$CHROMIUM" "${ARGS[@]}"
