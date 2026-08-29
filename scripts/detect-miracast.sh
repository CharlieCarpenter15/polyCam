#!/usr/bin/env bash
#
# Can this Raspberry Pi be a Miracast receiver?
#
# Run this before turning Miracast on:
#     ./scripts/detect-miracast.sh
#
# Miracast is what Windows calls "Connect to a wireless display" (Win+K). It is
# the closest thing to AirPlay: the room appears in a list the operating system
# draws, and nothing is installed on the laptop. Whether it can work here is a
# hardware question, and it has exactly one difficult requirement.
#
# **The Wi-Fi radio has to be able to run a Wi-Fi Direct group owner.** That is
# how a Miracast receiver announces itself — even in "over Infrastructure" mode,
# where the video then travels over the ordinary network, the announcement is
# still a Wi-Fi Direct one. A card acting as a group owner usually cannot also
# be associated with a normal network, so the practical shapes are:
#
#   * the Pi on Ethernet, leaving its built-in Wi-Fi free    ← the easy one
#   * the Pi on Wi-Fi plus a USB adapter for Wi-Fi Direct
#   * a card whose driver allows both at once (uncommon)
#
# This script answers which of those you have. It changes nothing and is safe to
# run at any time.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib-room.sh
. "$HERE/lib-room.sh"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
dim()  { printf '\033[2m%s\033[0m\n' "$1"; }
good() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
info() { printf '    %s\n' "$1"; }

usage() {
  cat <<'EOF'
Report whether this machine can act as a Miracast receiver, so a Windows laptop
can mirror to it with Win+K ("Connect to a wireless display").

Usage: detect-miracast.sh [--help]

Checks the Pi model and OS, whether the Wi-Fi radio is free, whether its driver
supports a Wi-Fi Direct group owner, whether the video decoder and the sink
software are present, and prints a verdict with the next step.

Nothing is changed. Safe to run on a room that is in use.
EOF
}

case "${1:-}" in
  -h | --help) usage; exit 0 ;;
  "") ;;
  *) printf 'Unknown option: %s\n\n' "$1"; usage; exit 2 ;;
esac

# Findings that decide the verdict at the end.
RADIO_FREE=0        # a wireless interface exists and is not associated
GO_SUPPORTED=0      # its driver advertises P2P-GO
CONCURRENT=0        # managed + P2P-GO at the same time
SINK=""             # which sink implementation is installed
WIRED=0
BLOCKERS=()

note_blocker() { BLOCKERS+=("$1"); }

printf '\n'
bold "Miracast receiver readiness"
dim  "Windows: Win+K → the room appears in the list. Nothing to install."
printf '\n'

# ------------------------------------------------------------------ 1. machine
bold "1. This machine"
MODEL="unknown"
[ -r /proc/device-tree/model ] && MODEL="$(tr -d '\0' < /proc/device-tree/model)"
info "Model      $MODEL"

OS_NAME="unknown"
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091 # not present at lint time on a developer machine
  OS_NAME="$(. /etc/os-release && printf '%s' "${PRETTY_NAME:-$NAME}")"
fi
info "OS         $OS_NAME ($(getconf LONG_BIT)-bit)"
info "Kernel     $(uname -r)"

case "$MODEL" in
  *"Raspberry Pi 5"*)
    warn "Pi 5: the built-in Wi-Fi is the hardest case for Wi-Fi Direct."
    info "     A USB Wi-Fi adapter is the reliable answer if the checks below fail."
    ;;
  *"Raspberry Pi 3"*)
    warn "Pi 3: decoding 1080p Miracast is at the edge of what this can do."
    info "     Expect to cap the receiver at 720p."
    ;;
esac
printf '\n'

# ------------------------------------------------------------------ 2. network
bold "2. Network, and whether the radio is free"

if command -v ip >/dev/null 2>&1; then
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    good "Wired: $line"
    WIRED=1
  done < <(ip -brief -4 addr show 2>/dev/null | awk '$1 ~ /^(eth|en)/ && $2 == "UP" {print $1" "$3}')
  [ "$WIRED" -eq 1 ] || warn "No wired connection is up."
else
  warn "The 'ip' command is missing; cannot inspect interfaces."
fi

# Every wireless interface, and what it is doing.
WIRELESS=()
if [ -d /sys/class/net ]; then
  for path in /sys/class/net/*; do
    [ -d "$path/wireless" ] || [ -e "$path/phy80211" ] || continue
    WIRELESS+=("$(basename "$path")")
  done
fi

if [ "${#WIRELESS[@]}" -eq 0 ]; then
  bad "No wireless interface found. Miracast needs one."
  note_blocker "no wireless interface"
else
  for iface in "${WIRELESS[@]}"; do
    ssid=""
    if command -v iw >/dev/null 2>&1; then
      ssid="$(iw dev "$iface" link 2>/dev/null | sed -n 's/^[[:space:]]*SSID:[[:space:]]*//p')"
    fi
    if [ -n "$ssid" ]; then
      warn "$iface is associated with SSID $ssid - not free for Wi-Fi Direct."
    else
      good "$iface is not associated — free for Wi-Fi Direct."
      RADIO_FREE=1
    fi
  done

  if [ "$RADIO_FREE" -eq 0 ]; then
    if [ "${#WIRELESS[@]}" -gt 1 ]; then
      info "Two radios are present but both are in use. Freeing one is enough."
    elif [ "$WIRED" -eq 1 ]; then
      info "This Pi has Ethernet, so the Wi-Fi can be freed:"
      info "  sudo nmcli device set ${WIRELESS[0]} managed no"
      info "  sudo ip link set ${WIRELESS[0]} down"
    else
      info "This Pi is on Wi-Fi and has no wired connection, so freeing the"
      info "radio would take the room offline. Add Ethernet, or a USB Wi-Fi"
      info "adapter for Wi-Fi Direct to use."
    fi
  fi
fi
printf '\n'

# ------------------------------------------------------ 3. Wi-Fi Direct support
bold "3. Wi-Fi Direct (the requirement that decides this)"

if ! command -v iw >/dev/null 2>&1; then
  warn "iw is not installed, so the driver cannot be questioned:"
  info "  sudo apt install iw"
  note_blocker "iw not installed (cannot confirm Wi-Fi Direct support)"
else
  IW_LIST="$(iw list 2>/dev/null)"
  if [ -z "$IW_LIST" ]; then
    bad "'iw list' returned nothing — no wireless driver is loaded."
    note_blocker "no wireless driver"
  else
    # The modes a driver will admit to. P2P-GO is the one that matters: a
    # Miracast receiver announces itself as a Wi-Fi Direct group owner.
    MODES="$(printf '%s' "$IW_LIST" | sed -n '/Supported interface modes/,/^[[:space:]]*[A-Za-z]/p' \
             | sed -n 's/^[[:space:]]*\*[[:space:]]*//p' | tr '\n' ' ')"
    [ -n "$MODES" ] && info "Modes      $MODES"

    case "$MODES" in
      *"P2P-GO"*)
        good "The driver supports P2P-GO (Wi-Fi Direct group owner)."
        GO_SUPPORTED=1
        ;;
      *"P2P-client"* | *"P2P-device"*)
        bad "P2P is partly supported but P2P-GO is missing."
        info "     A receiver has to be the group owner, so this is not enough."
        note_blocker "the Wi-Fi driver cannot be a Wi-Fi Direct group owner"
        ;;
      *)
        bad "The driver advertises no Wi-Fi Direct support at all."
        note_blocker "the Wi-Fi driver has no Wi-Fi Direct support"
        ;;
    esac

    # Whether it can be a group owner *while* staying on the room network. Rare,
    # and worth knowing: it is the difference between needing Ethernet and not.
    COMBOS="$(printf '%s' "$IW_LIST" | sed -n '/valid interface combinations/,/^[[:space:]]*[A-Za-z]/p')"
    if printf '%s' "$COMBOS" | grep -q "P2P-GO"; then
      if printf '%s' "$COMBOS" | grep -q "managed"; then
        good "It also lists managed + P2P-GO together: both at once may work."
        CONCURRENT=1
        info "     Reported combinations:"
        printf '%s' "$COMBOS" | sed -n 's/^[[:space:]]*\*[[:space:]]*/       /p' | head -6
      fi
    else
      dim  "     No combination lists P2P-GO, so the radio has to be free."
    fi
  fi
fi

if command -v wpa_supplicant >/dev/null 2>&1; then
  good "wpa_supplicant: $(wpa_supplicant -v 2>&1 | head -1)"
else
  bad "wpa_supplicant is missing — Wi-Fi Direct is negotiated through it."
  note_blocker "wpa_supplicant not installed"
fi

if command -v rfkill >/dev/null 2>&1 && rfkill list 2>/dev/null | grep -q "Soft blocked: yes"; then
  warn "Something is soft-blocked in rfkill; check 'rfkill list'."
fi
printf '\n'

# ------------------------------------------------------------------ 4. decoding
bold "4. Decoding the incoming video"

if command -v gst-inspect-1.0 >/dev/null 2>&1; then
  MISSING=()
  for plugin in h264parse avdec_h264 v4l2h264dec glimagesink autovideosink; do
    gst-inspect-1.0 "$plugin" >/dev/null 2>&1 || MISSING+=("$plugin")
  done
  # Either decoder will do: v4l2h264dec is the Pi's hardware path, avdec_h264
  # the software fallback.
  if ! printf '%s\n' "${MISSING[@]}" | grep -qx "v4l2h264dec"; then
    good "Hardware H.264 decoding is available (v4l2h264dec)."
  elif ! printf '%s\n' "${MISSING[@]}" | grep -qx "avdec_h264"; then
    good "Software H.264 decoding is available (avdec_h264)."
    warn "     No hardware decoder; 1080p may not keep up on a Pi."
  else
    bad "No H.264 decoder found."
    info "  sudo apt install gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \\"
    info "                   gstreamer1.0-plugins-ugly gstreamer1.0-libav"
    note_blocker "no H.264 decoder for the incoming stream"
  fi
  if [ "${#MISSING[@]}" -gt 0 ]; then
    dim  "     Not present: ${MISSING[*]}"
  fi
else
  bad "GStreamer is not installed; the receiver has nothing to draw with."
  info "  sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-libav"
  note_blocker "GStreamer not installed"
fi
printf '\n'

# --------------------------------------------------------------- 5. sink software
bold "5. Receiver software"

if command -v miracle-sinkctl >/dev/null 2>&1; then
  good "MiracleCast is installed ($(command -v miracle-sinkctl))."
  SINK="miraclecast"
elif [ -x "${LAZYCAST_DIR:-/opt/lazycast}/all.sh" ]; then
  good "lazycast is installed (${LAZYCAST_DIR:-/opt/lazycast})."
  SINK="lazycast"
else
  bad "No Miracast receiver software is installed."
  info "Neither is in the Raspberry Pi OS archive, so both are built from source."
  info "MiracleCast   https://github.com/albfan/miraclecast"
  info "lazycast      https://github.com/homeworkc/lazycast"
  info ""
  info "Then set it in Settings → Miracast → Receiver backend, or leave it on"
  info "'auto' and this room will use whichever it finds."
  note_blocker "no receiver software installed"
fi

if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-active --quiet avahi-daemon 2>/dev/null; then
    good "avahi-daemon is running (Miracast-over-Infrastructure uses it)."
  else
    warn "avahi-daemon is not running:  sudo systemctl enable --now avahi-daemon"
  fi
fi
printf '\n'

# ------------------------------------------------------------------- 6. verdict
bold "Verdict"

if [ "${#BLOCKERS[@]}" -eq 0 ] && { [ "$RADIO_FREE" -eq 1 ] || [ "$CONCURRENT" -eq 1 ]; }; then
  good "This machine can be a Miracast receiver."
  info "Backend:  $SINK"
  if [ "$CONCURRENT" -eq 1 ] && [ "$RADIO_FREE" -eq 0 ]; then
    info "Using the radio for both the room network and Wi-Fi Direct at once."
    info "That is the uncommon case; if it proves flaky, free the radio."
  fi
  info ""
  info "Turn it on:   ./scripts/roomctl set MIRACAST_ENABLED true"
  info "              ./scripts/roomctl restart miracast"
  info "Then on the laptop: Win+K, and pick this room."
  printf '\n'
  exit 0
fi

if [ "$GO_SUPPORTED" -eq 1 ] && [ "$RADIO_FREE" -eq 0 ] && [ "$CONCURRENT" -eq 0 ]; then
  note_blocker "the Wi-Fi radio is in use, and this driver cannot do both at once"
fi

bad "Not ready. What is in the way:"
for blocker in "${BLOCKERS[@]}"; do
  info "• $blocker"
done
info ""
if [ "$GO_SUPPORTED" -eq 0 ]; then
  info "The Wi-Fi Direct problem is the one that cannot be fixed in software."
  info "A USB Wi-Fi adapter that supports P2P-GO solves it for a few pounds;"
  info "that is what most Raspberry Pi Miracast builds end up using."
  info ""
fi
info "Until then, a Windows laptop can still share its screen through the"
info "browser path, which needs no special hardware:"
info "  ./scripts/roomctl set CAST_ENABLED true"
info "and the address appears on the TV. See the README section"
info "'Screen sharing from a Windows PC'."
printf '\n'
exit 1
