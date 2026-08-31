#!/usr/bin/env bash
#
# AirPlay receiver supervisor.
#
# Run by room-airplay.service. Starts UxPlay, watches its output, and tells the
# room backend when someone starts or stops mirroring — which is what lets the
# dashboard step aside during a screen share and come back afterwards.
#
# Deliberately independent of the backend: if the backend is down, UxPlay keeps
# running and people can still share their screens. Events are reported on a
# best-effort basis and never block or fail the receiver.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=scripts/lib-room.sh
. "$HERE/lib-room.sh"

# ------------------------------------------------------------ event matching

# Translate one line of UxPlay output into an event, or into nothing.
#
# The distinction that matters is between *sockets* and *sessions*. UxPlay
# prints "Accepted IPv4 client on socket 7" and "Connection closed on socket 7"
# for every TCP connection it handles — including the ones an iPhone opens just
# to ask what this receiver is when somebody opens the Screen Mirroring menu,
# and the several a real client opens and closes around one screen share.
# Matching those meant the dashboard stepped aside for a phone that was only
# looking, and stepped back over a screen that was still being shared.
#
# These are the lines that describe the session itself. All three are logged at
# UxPlay's default level; the connection counters next to them are not, so
# there is nothing more specific to wait for:
#
#   connection request from NAME (MODEL) with deviceID = ID   who is asking
#   raop_rtp_mirror starting mirroring                        mirroring is live
#   raop_rtp_mirror->running is no longer true                mirroring ended
#
# Prints "client NAME", "connected" or "disconnected"; empty means "not an
# event". Kept free of side effects so the test suite can check it against
# recorded UxPlay output.
airplay_event_for_line() {
  local line="$1"
  local lowered="${1,,}"
  case "$lowered" in
    # -nohold announces the takeover by IP address, and the mirroring line that
    # follows reports the session anyway. Matched first: it also says "from".
    *'"nohold" feature'*)
      : ;;
    *"connection request from"*)
      printf 'client %s\n' "$(printf '%s' "$line" |
        sed -n 's/^.*connection request from \(.*\) ([^()]*) with deviceID.*$/\1/p')"
      ;;
    *"starting mirroring"*)
      printf 'connected\n'
      ;;
    *"is no longer true"* | *"lost connection with client"*)
      printf 'disconnected\n'
      ;;
  esac
}

# True when a line looks like UxPlay explaining why it is about to give up.
#
# UxPlay exits rather than carrying on when it cannot register with mDNS or open
# a window, and says why immediately beforehand — "Could not initialize dnssd
# library!: error -65537" is avahi-daemon not running. Keeping that sentence is
# the difference between the dashboard saying "AirPlay: Problem · avahi is not
# running" and leaving somebody to search the network for a fault on this Pi.
airplay_line_is_failure() {
  local lowered="${1,,}"
  case "$lowered" in
    # A client going away is ordinary, and is already a session event.
    *"lost connection with client"*) return 1 ;;
    *"could not"* | *"cannot"* | *"failed"* | *"error"*) return 0 ;;
    *) return 1 ;;
  esac
}

# Sourcing this script with ROOM_AIRPLAY_MATCH_ONLY set defines the matchers and
# stops there, which is how the tests exercise them without starting a receiver.
if [ -n "${ROOM_AIRPLAY_MATCH_ONLY:-}" ]; then
  # Sourced (the tests) returns; run directly by mistake, exit instead.
  [ "${BASH_SOURCE[0]}" != "$0" ] && return 0
  exit 0
fi

room_load_config

ENABLED="$(room_config AIRPLAY_ENABLED true)"
ROOM_NAME="$(room_config ROOM_NAME 'Meeting Room')"
AIRPLAY_NAME="$(room_config AIRPLAY_NAME '')"
AIRPLAY_PIN="$(room_config AIRPLAY_PIN '')"
EXTRA_ARGS="$(room_config AIRPLAY_EXTRA_ARGS '')"
[ -n "$AIRPLAY_NAME" ] || AIRPLAY_NAME="$ROOM_NAME"

if ! room_is_true "$ENABLED"; then
  room_log "airplay.disabled_by_configuration"
  exec sleep infinity
fi

if ! command -v uxplay >/dev/null 2>&1; then
  room_log "airplay.uxplay_not_installed" "hint=run scripts/install.sh, or apt install uxplay"
  # Sleeping keeps the unit "active" without a restart loop; the dashboard
  # reports the problem instead.
  exec sleep infinity
fi

# UxPlay's output is read inside a pipeline, which bash runs in a subshell, so
# what it saw cannot be read back from a variable once the pipeline ends. These
# two files carry it across: whether UxPlay is up (for the heartbeat, which runs
# in yet another subshell) and the last thing it complained about.
ROOM_VAR="${ROOM_APPLIANCE_VAR:-$ROOM_ROOT/var}"
mkdir -p "$ROOM_VAR" 2>/dev/null || true
UXPLAY_STATE_FILE="$ROOM_VAR/airplay-uxplay-state"
UXPLAY_ERROR_FILE="$ROOM_VAR/airplay-last-error"

report() {
  # report EVENT [CLIENT] [DETAIL]
  #
  # The client name and the detail both come from UxPlay's output, so they are
  # untrusted text going into a JSON body. Rather than escaping it, reduce them
  # to characters that cannot affect JSON at all — no double quote, no
  # backslash, no control characters — and cap the length. An apostrophe is
  # safe and stays, because "Charlie's iPhone" is what goes up on the TV.
  local event="$1"
  local client detail running
  client="$(printf '%s' "${2-}" | tr -cd "[:alnum:].:_ '-" | cut -c1-60)"
  detail="$(printf '%s' "${3-}" | tr -cd "[:alnum:].,:;!?()/_ '-" | cut -c1-200)"
  running=false
  [ "$(cat "$UXPLAY_STATE_FILE" 2>/dev/null)" = "up" ] && running=true
  room_post_internal /api/internal/airplay \
    "$(printf '{"event":"%s","client":"%s","detail":"%s","running":%s}' \
       "$event" "$client" "$detail" "$running")"
}

# --------------------------------------------------------------------- args

# Why these flags:
#   -n NAME     the name shown in Screen Mirroring
#   -nh         do not append the hostname to that name
#   -vsync no   lowest latency: do not pace frames to the display clock, which
#               matters far more than smoothness for a shared laptop screen
#   -s 1920x1080 a sensible cap; a 4K stream on a Pi is not worth the latency
#   -reset 15   drop a stalled client instead of wedging the receiver
#   -nohold     a new person sharing takes over rather than being refused
UXPLAY_ARGS=(
  -n "$AIRPLAY_NAME"
  -nh
  -vsync no
  -s 1920x1080
  -fps 30
  -reset 15
  -nohold
)

[ -n "$AIRPLAY_PIN" ] && UXPLAY_ARGS+=(-pin "$AIRPLAY_PIN")

if [ -n "$EXTRA_ARGS" ]; then
  read -r -a EXTRA_ARRAY <<< "$EXTRA_ARGS"
  UXPLAY_ARGS+=("${EXTRA_ARRAY[@]}")
fi

# UxPlay needs the graphical session to put a window on the TV.
if [ -z "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ]; then
  for socket in "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"/wayland-*; do
    [ -S "$socket" ] && { WAYLAND_DISPLAY="$(basename "$socket")"; export WAYLAND_DISPLAY; break; }
  done
  [ -z "${WAYLAND_DISPLAY:-}" ] && [ -S /tmp/.X11-unix/X0 ] && { DISPLAY=":0"; export DISPLAY; }
fi

# Avahi advertises the receiver on the network; without it nothing appears in
# Screen Mirroring. Worth saying so once, clearly.
if command -v systemctl >/dev/null 2>&1; then
  systemctl is-active --quiet avahi-daemon 2>/dev/null || \
    room_log "airplay.avahi_not_running" "hint=sudo systemctl enable --now avahi-daemon"
fi

if [ -n "$AIRPLAY_PIN" ]; then PIN_STATE=yes; else PIN_STATE=no; fi
room_log "airplay.starting" "name=$AIRPLAY_NAME" "pin=$PIN_STATE"

# ------------------------------------------------------------------ run loop

# A heartbeat says *this script* is alive, which is a different claim from
# "UxPlay is up" — a supervisor faithfully restarting a receiver that will not
# start is precisely the case worth reporting. `report` reads the current answer
# to the second question out of the state file.
heartbeat() {
  while true; do
    sleep 60
    report heartbeat
  done
}
heartbeat &

cleanup() {
  local code=$?
  # Take the trap off first, so a signal arriving during cleanup cannot re-enter.
  trap - EXIT INT TERM
  # Kill everything this script started: the heartbeat, and UxPlay itself when
  # run by hand. Under systemd, KillMode=control-group already handles the tree;
  # this matters when an engineer runs the script directly and presses Ctrl+C.
  local pid
  for pid in $(jobs -p); do
    kill "$pid" 2>/dev/null || true
  done
  printf 'down\n' > "$UXPLAY_STATE_FILE" 2>/dev/null || true
  report stopped
  rm -f "$UXPLAY_STATE_FILE" "$UXPLAY_ERROR_FILE" 2>/dev/null || true
  # A signal handler returns to wherever the script was, so without this the
  # run loop carried on after systemd had asked it to stop: the receiver kept
  # restarting through the whole of TimeoutStopSec, and every "restart AirPlay"
  # took fifteen seconds to do something it had already reported doing.
  exit "$code"
}
trap cleanup EXIT INT TERM

RESTARTS=0

# The device name arrives one line before mirroring starts, so it is held here
# until there is a session to attach it to.
PENDING_CLIENT=""

# Act on one line of UxPlay output.
handle_line() {
  local verdict event name
  verdict="$(airplay_event_for_line "$1")"
  [ -n "$verdict" ] || return 0

  event="${verdict%% *}"
  name="${verdict#"$event"}"
  name="${name# }"

  case "$event" in
    client) PENDING_CLIENT="$name" ;;
    connected) report connected "$PENDING_CLIENT" ;;
    disconnected)
      PENDING_CLIENT=""
      report disconnected
      ;;
  esac
}

while true; do
  # stdbuf keeps UxPlay's output unbuffered, so a connect event is not stuck in
  # a pipe buffer for several seconds.
  if command -v stdbuf >/dev/null 2>&1; then
    UXPLAY_CMD=(stdbuf -oL -eL uxplay)
  else
    UXPLAY_CMD=(uxplay)
  fi

  # ${PIPESTATUS[0]} names UxPlay's own exit code unambiguously. (`$?` happens
  # to agree here because pipefail is set, but it reports the pipeline as a
  # whole, so it would start lying the moment the read loop could fail.)
  : > "$UXPLAY_ERROR_FILE" 2>/dev/null || true
  printf 'up\n' > "$UXPLAY_STATE_FILE" 2>/dev/null || true
  report started

  "${UXPLAY_CMD[@]}" "${UXPLAY_ARGS[@]}" 2>&1 |
    while IFS= read -r line; do
      printf '%s\n' "$line"
      airplay_line_is_failure "$line" && printf '%s\n' "$line" > "$UXPLAY_ERROR_FILE"
      handle_line "$line"
    done
  status="${PIPESTATUS[0]}"

  printf 'down\n' > "$UXPLAY_STATE_FILE" 2>/dev/null || true
  RESTARTS=$((RESTARTS + 1))
  REASON="$(head -n1 "$UXPLAY_ERROR_FILE" 2>/dev/null)"
  room_log "airplay.uxplay_exited" "status=$status" "restarts=$RESTARTS" \
    "reason=${REASON:-none reported}"
  report exited "" "${REASON:-UxPlay exited with status $status}"

  # Back off a little so a persistent failure (no display, port already in use)
  # does not spin. systemd would restart the whole unit anyway; this keeps the
  # journal readable in the meantime.
  sleep 5
done
