#!/usr/bin/env bash
#
# Miracast receiver supervisor.
#
# Run by room-miracast.service. Prepares the Wi-Fi radio, starts a Miracast
# sink, watches its output, and tells the room backend when a laptop starts or
# stops mirroring — which is what lets the dashboard step aside for a shared
# screen and come back afterwards.
#
# Deliberately independent of the backend, exactly like start-airplay.sh: if the
# room software is down, people can still put a screen on the TV. Events are
# reported best-effort and never block or fail the receiver.
#
# **Two sinks, one supervisor.** Neither Miracast implementation is packaged for
# Raspberry Pi OS, so which one a room has depends on who built what. Rather
# than picking a winner, this drives either:
#
#   miraclecast   miracle-wifid + miracle-sinkctl. The rigorous one; wants its
#                 own wpa_supplicant and a free radio.
#   lazycast      all.sh (Wi-Fi Direct) or mice.sh (Miracast over
#                 Infrastructure, where the video goes over the room network).
#
# Adding a third means adding one `case` branch below and one name to BACKENDS
# in app/miracast_service.py. Nothing else in the appliance needs to know.
#
# Nothing here parses a protocol. The sinks print human-readable lines; this
# turns the ones that mean "somebody connected" into events. The patterns are
# matched loosely on purpose — see handle_line.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=scripts/lib-room.sh
. "$HERE/lib-room.sh"

room_load_config

ENABLED="$(room_config MIRACAST_ENABLED false)"
ROOM_NAME="$(room_config ROOM_NAME 'Meeting Room')"
MIRACAST_NAME="$(room_config MIRACAST_NAME '')"
MIRACAST_PIN="$(room_config MIRACAST_PIN '')"
BACKEND="$(room_config MIRACAST_BACKEND auto)"
INTERFACE="$(room_config MIRACAST_INTERFACE '')"
LAZYCAST_DIR="$(room_config MIRACAST_LAZYCAST_DIR '/opt/lazycast')"
INFRASTRUCTURE="$(room_config MIRACAST_INFRASTRUCTURE true)"
FREE_RADIO="$(room_config MIRACAST_FREE_RADIO false)"
EXTRA_ARGS="$(room_config MIRACAST_EXTRA_ARGS '')"
[ -n "$MIRACAST_NAME" ] || MIRACAST_NAME="$ROOM_NAME"

report() {
  # report EVENT [CLIENT] [DETAIL]
  #
  # Client and detail come from a sink's output, so they are untrusted text
  # going into a JSON body. Rather than escaping, reduce them to characters
  # that cannot affect JSON at all and cap the length — the same treatment
  # start-airplay.sh gives UxPlay's output.
  local event="$1"
  local client detail
  client="$(printf '%s' "${2-}" | tr -cd '[:alnum:].:_ -' | cut -c1-60)"
  detail="$(printf '%s' "${3-}" | tr -cd '[:alnum:].:_ ,-' | cut -c1-200)"
  room_post_internal /api/internal/miracast \
    "$(printf '{"event":"%s","client":"%s","detail":"%s","backend":"%s"}' \
       "$event" "$client" "$detail" "$BACKEND")"
}

# `blocked` means "running, but cannot receive anything, and here is why". It is
# the difference between a room that explains itself and one that simply never
# appears in the Windows list. Then sleep: exiting would have systemd restart
# us forever over a problem no restart can fix.
block() {
  room_log "miracast.blocked" "reason=$1"
  report blocked "" "$1"
  exec sleep infinity
}

if ! room_is_true "$ENABLED"; then
  room_log "miracast.disabled_by_configuration"
  exec sleep infinity
fi

# ------------------------------------------------------------------- backend

resolve_backend() {
  case "$BACKEND" in
    miraclecast)
      command -v miracle-sinkctl >/dev/null 2>&1 || \
        block "MiracleCast is selected but miracle-sinkctl is not installed"
      ;;
    lazycast)
      [ -x "$LAZYCAST_DIR/all.sh" ] || \
        block "lazycast is selected but $LAZYCAST_DIR/all.sh was not found"
      ;;
    auto)
      if command -v miracle-sinkctl >/dev/null 2>&1; then
        BACKEND=miraclecast
      elif [ -x "$LAZYCAST_DIR/all.sh" ]; then
        BACKEND=lazycast
      else
        block "no Miracast receiver software is installed - run scripts/detect-miracast.sh"
      fi
      ;;
    *)
      block "unknown Miracast backend '$BACKEND'"
      ;;
  esac
}

resolve_backend

# ------------------------------------------------------------------ the radio

# Which interface to use for Wi-Fi Direct. Named explicitly in a room with two
# adapters, since guessing wrong there means taking the room off the network.
pick_interface() {
  [ -n "$INTERFACE" ] && { printf '%s' "$INTERFACE"; return 0; }
  local path name
  for path in /sys/class/net/*; do
    [ -e "$path/phy80211" ] || continue
    name="$(basename "$path")"
    printf '%s' "$name"
    return 0
  done
  return 1
}

WIFI_IFACE="$(pick_interface)" || block "no wireless interface was found"

# Is it currently associated with a normal network? A Miracast receiver has to
# be a Wi-Fi Direct group owner, and a card doing that usually cannot also be a
# client. Taking the room off its own network to find out is not this script's
# decision to make, so unless MIRACAST_FREE_RADIO says otherwise, stop and say
# so.
radio_is_busy() {
  command -v iw >/dev/null 2>&1 || return 1
  iw dev "$WIFI_IFACE" link 2>/dev/null | grep -q "^[[:space:]]*SSID:"
}

wired_is_up() {
  command -v ip >/dev/null 2>&1 || return 1
  ip -brief -4 addr show 2>/dev/null | awk '$1 ~ /^(eth|en)/ && $2 == "UP" {found=1} END {exit !found}'
}

if radio_is_busy; then
  if ! room_is_true "$FREE_RADIO"; then
    block "$WIFI_IFACE is on the room network, and a Miracast receiver needs a free radio - see scripts/detect-miracast.sh"
  fi
  if ! wired_is_up; then
    block "freeing $WIFI_IFACE would take this room offline because there is no wired connection"
  fi
  room_log "miracast.freeing_radio" "interface=$WIFI_IFACE"
  # Only ever done with an explicit setting AND a working wired connection.
  if command -v nmcli >/dev/null 2>&1; then
    nmcli device set "$WIFI_IFACE" managed no >/dev/null 2>&1 || true
  fi
  ip link set "$WIFI_IFACE" down >/dev/null 2>&1 || true
  sleep 1
  ip link set "$WIFI_IFACE" up >/dev/null 2>&1 || true
fi

if command -v iw >/dev/null 2>&1; then
  if ! iw list 2>/dev/null | grep -q "P2P-GO"; then
    block "the driver for $WIFI_IFACE does not support a Wi-Fi Direct group owner"
  fi
fi

# Miracast-over-Infrastructure advertises the room by name, which needs mDNS.
if command -v systemctl >/dev/null 2>&1 && room_is_true "$INFRASTRUCTURE"; then
  systemctl is-active --quiet avahi-daemon 2>/dev/null || \
    room_log "miracast.avahi_not_running" "hint=sudo systemctl enable --now avahi-daemon"
fi

# The sink puts a window on the TV, so it needs the graphical session — the same
# dance start-airplay.sh does for UxPlay.
if [ -z "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ]; then
  for socket in "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"/wayland-*; do
    [ -S "$socket" ] && { WAYLAND_DISPLAY="$(basename "$socket")"; export WAYLAND_DISPLAY; break; }
  done
  [ -z "${WAYLAND_DISPLAY:-}" ] && [ -S /tmp/.X11-unix/X0 ] && { DISPLAY=":0"; export DISPLAY; }
fi

if [ -n "$MIRACAST_PIN" ]; then PIN_STATE=yes; else PIN_STATE=no; fi
room_log "miracast.starting" "backend=$BACKEND" "interface=$WIFI_IFACE" \
         "name=$MIRACAST_NAME" "pin=$PIN_STATE"
report started

# ---------------------------------------------------------------- the command

# Build the sink command for the chosen backend.
sink_command() {
  case "$BACKEND" in
    miraclecast)
      # sinkctl is normally interactive; --interface pins it to one card so it
      # does not adopt the one carrying the room network, and the run loop
      # below feeds it the "run" command on stdin.
      SINK_CMD=(miracle-sinkctl --interface "$WIFI_IFACE")
      [ -n "$MIRACAST_PIN" ] && SINK_CMD+=(--pin "$MIRACAST_PIN")
      ;;
    lazycast)
      # lazycast ships two entry points: mice.sh keeps the video on the room
      # network (lower latency, and what Windows 10 and later prefer), all.sh
      # sends it over the Wi-Fi Direct link.
      if room_is_true "$INFRASTRUCTURE" && [ -x "$LAZYCAST_DIR/mice.sh" ]; then
        SINK_CMD=("$LAZYCAST_DIR/mice.sh")
      else
        SINK_CMD=("$LAZYCAST_DIR/all.sh")
      fi
      ;;
  esac

  if [ -n "$EXTRA_ARGS" ]; then
    read -r -a EXTRA_ARRAY <<< "$EXTRA_ARGS"
    SINK_CMD+=("${EXTRA_ARRAY[@]}")
  fi
}

sink_command

# ------------------------------------------------------------------ run loop

# A heartbeat lets the backend tell "the sink is fine and nobody is sharing"
# from "the supervisor has died", which are very different situations.
heartbeat() {
  while true; do
    sleep 60
    report heartbeat
  done
}
heartbeat &

cleanup() {
  # Take the trap off first, so a signal arriving during cleanup cannot re-enter.
  trap - EXIT INT TERM
  local pid
  for pid in $(jobs -p); do
    kill "$pid" 2>/dev/null || true
  done
  report stopped
}
trap cleanup EXIT INT TERM

RESTARTS=0

# Translate one line of sink output into an event.
#
# Matched as case-insensitive substrings rather than exact strings: these are
# human-readable messages from two projects under active development, and a
# reworded log line must not silently break the dashboard's behaviour.
#
# **The bias is deliberately towards missing a connect rather than inventing
# one**, and the asymmetry is worth understanding. The sink draws its own window
# on the TV, so if a connect is missed the shared screen still appears — the
# dashboard simply does not know, which costs a stale indicator. Whereas a false
# connect makes the room hide the dashboard with nothing behind it, which looks
# exactly like a broken appliance. So only lines that can only mean "a client
# is now attached" count, and anything ambiguous is treated as readiness.
#
# In particular: lazycast's "the display is ready" and wpa_supplicant's
# "groupStarted" both fire when the *receiver* comes up and is waiting — a
# Wi-Fi Direct group owner creates its group before anybody joins it. Neither
# means somebody is mirroring.
handle_line() {
  local line="$1"
  local lowered client
  lowered="$(printf '%s' "$line" | tr '[:upper:]' '[:lower:]')"
  case "$lowered" in
    # The sink is up and waiting to be picked. Not a session.
    *"the display is ready"* | *"groupstarted"* | *"[add] link"*)
      report started
      ;;
    # A client is attached: wpa_supplicant authorises a station, miraclecast
    # marks the peer connected, or RTSP negotiation has begun.
    *"[connected]"* | *"peer connected"* | *"staauthorized"* | *"peerjoined"* | \
    *"rtsp connection"* | *"session established"*)
      client="$(printf '%s' "$line" | sed -n 's/.*\(from\|peer\|client\)[: ]*\([^ ,]*\).*/\2/p')"
      report connected "$client"
      ;;
    # Pixels are actually moving.
    *"start streaming"* | *"video stream"* | *"now playing"*)
      report connected ""
      ;;
    *"[disconnected]"* | *"peer disconnected"* | *"groupfinished"* | \
    *"session closed"* | *"teardown"* | *"connection closed"*)
      report disconnected
      ;;
    # Worth surfacing rather than burying in the journal: the things a room
    # owner can actually act on.
    *"no p2p"* | *"does not support p2p"* | *"resource busy"*)
      report blocked "" "the Wi-Fi interface would not enter Wi-Fi Direct mode"
      ;;
  esac
}

while true; do
  # stdbuf keeps output unbuffered, so a connect event is not stuck in a pipe
  # buffer for several seconds.
  if command -v stdbuf >/dev/null 2>&1; then
    RUN_CMD=(stdbuf -oL -eL "${SINK_CMD[@]}")
  else
    RUN_CMD=("${SINK_CMD[@]}")
  fi

  # miracle-sinkctl waits at a prompt for `run <link>`, so it is given one.
  # `yes` would spin; a single delayed line is enough, and closing stdin
  # afterwards is what makes sinkctl exit if it is going to.
  if [ "$BACKEND" = miraclecast ]; then
    { sleep 3; printf 'run 1\n'; sleep infinity; } | "${RUN_CMD[@]}" 2>&1 |
      while IFS= read -r line; do
        printf '%s\n' "$line"
        handle_line "$line"
      done
    status="${PIPESTATUS[1]}"
  else
    "${RUN_CMD[@]}" 2>&1 |
      while IFS= read -r line; do
        printf '%s\n' "$line"
        handle_line "$line"
      done
    status="${PIPESTATUS[0]}"
  fi

  RESTARTS=$((RESTARTS + 1))
  room_log "miracast.sink_exited" "status=$status" "restarts=$RESTARTS" "backend=$BACKEND"
  report restarted

  # Back off so a persistent failure (no display, radio busy, missing plugin)
  # does not spin. systemd would restart the whole unit anyway; this keeps the
  # journal readable in the meantime.
  sleep 5
done
