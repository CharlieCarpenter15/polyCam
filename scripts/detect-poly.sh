#!/usr/bin/env bash
#
# Report what the Raspberry Pi can see of the conference bar.
#
# Run this first when the room has no picture or no sound:
#     ./scripts/detect-poly.sh
#
# It prints, in order: the USB device, the camera nodes, the audio sources and
# sinks, which ones are currently the system default, and what is missing.
# Nothing here changes anything — it is safe to run at any time.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib-room.sh
. "$HERE/lib-room.sh"

room_load_config || true

MATCH_WORDS="$(room_config_list POLY_USB_MATCH)"
[ -n "$MATCH_WORDS" ] || MATCH_WORDS=$'poly\nplantronics\npolycom\nstudio'

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
dim()  { printf '\033[2m%s\033[0m\n' "$1"; }
good() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }

matches_bar() {
  local haystack
  haystack="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  local word
  while IFS= read -r word; do
    [ -n "$word" ] || continue
    case "$haystack" in
      *"$(printf '%s' "$word" | tr '[:upper:]' '[:lower:]')"*) return 0 ;;
    esac
  done <<< "$MATCH_WORDS"
  return 1
}

printf '\n'
bold "Conference bar detection"
dim  "Matching on: $(printf '%s' "$MATCH_WORDS" | tr '\n' ' ')"
printf '\n'

# ------------------------------------------------------------------ 1. USB
bold "1. USB device (lsusb)"
if command -v lsusb >/dev/null 2>&1; then
  found=0
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    if matches_bar "$line"; then
      good "$line"
      found=1
    fi
  done < <(lsusb)
  if [ "$found" -eq 0 ]; then
    bad "No matching USB device found."
    dim  "     All connected USB devices:"
    lsusb | sed 's/^/       /'
    dim  "     If your bar is listed above, add a word from its name to"
    dim  "     Settings → Poly conference bar → USB name matches."
  fi
else
  warn "lsusb is not installed:  sudo apt install usbutils"
fi
printf '\n'

# --------------------------------------------------------------- 2. camera
bold "2. Camera (video4linux)"
if command -v v4l2-ctl >/dev/null 2>&1; then
  if v4l2-ctl --list-devices 2>/dev/null | grep -q .; then
    v4l2-ctl --list-devices 2>/dev/null | sed 's/^/    /'
  else
    bad "v4l2-ctl found no cameras."
  fi
else
  warn "v4l2-ctl is not installed:  sudo apt install v4l-utils"
fi

if compgen -G "/sys/class/video4linux/video*" >/dev/null; then
  printf '\n'
  dim "  Kernel view:"
  for node in /sys/class/video4linux/video*; do
    name="$(cat "$node/name" 2>/dev/null || echo unknown)"
    device="/dev/$(basename "$node")"
    if matches_bar "$name"; then
      good "$device  $name"
    else
      printf '    %s  %s\n' "$device" "$name"
    fi
  done
else
  bad "No /dev/video* devices exist. Is the bar plugged in?"
fi

# Prove the camera can actually deliver frames, not just that it exists.
if command -v v4l2-ctl >/dev/null 2>&1 && [ -e /dev/video0 ]; then
  printf '\n'
  dim "  Formats offered by /dev/video0:"
  v4l2-ctl -d /dev/video0 --list-formats 2>/dev/null | sed 's/^/    /' | head -12
fi
printf '\n'

# ---------------------------------------------------------------- 3. audio
bold "3. Audio (PipeWire / PulseAudio)"
if command -v pactl >/dev/null 2>&1; then
  if ! pactl info >/dev/null 2>&1; then
    bad "pactl cannot reach the audio server."
    dim  "     If you are over SSH, the audio server belongs to the desktop"
    dim  "     session. Try:  XDG_RUNTIME_DIR=/run/user/\$(id -u) pactl info"
  else
    server="$(pactl info 2>/dev/null | sed -n 's/^Server Name: //p')"
    dim "  Server: ${server:-unknown}"
    default_sink="$(pactl get-default-sink 2>/dev/null || true)"
    default_source="$(pactl get-default-source 2>/dev/null || true)"

    printf '\n'
    dim "  Microphones (sources):"
    while IFS=$'\t' read -r _index name _rest; do
      [ -n "${name:-}" ] || continue
      case "$name" in *.monitor) continue ;; esac
      label="$name"
      [ "$name" = "$default_source" ] && label="$label  [system default]"
      if matches_bar "$name"; then good "$label"; else printf '    %s\n' "$label"; fi
    done < <(pactl list short sources 2>/dev/null)

    printf '\n'
    dim "  Speakers (sinks):"
    while IFS=$'\t' read -r _index name _rest; do
      [ -n "${name:-}" ] || continue
      label="$name"
      [ "$name" = "$default_sink" ] && label="$label  [system default]"
      if matches_bar "$name"; then good "$label"; else printf '    %s\n' "$label"; fi
    done < <(pactl list short sinks 2>/dev/null)

    if [ -n "$default_sink" ]; then
      printf '\n'
      dim "  Current volume: $(pactl get-sink-volume "$default_sink" 2>/dev/null | head -1)"
      dim "  Speaker muted:  $(pactl get-sink-mute "$default_sink" 2>/dev/null)"
    fi
    if [ -n "$default_source" ]; then
      dim "  Mic muted:      $(pactl get-source-mute "$default_source" 2>/dev/null)"
    fi
  fi
else
  warn "pactl is not installed:  sudo apt install pulseaudio-utils"
fi

if command -v wpctl >/dev/null 2>&1; then
  printf '\n'
  dim "  wpctl status (PipeWire's own view):"
  wpctl status 2>/dev/null | sed -n '/Audio/,/Video/p' | sed 's/^/    /' | head -30
fi
printf '\n'

# ------------------------------------------------------- 4. what the app sees
bold "4. What the room software has selected"
PY="$(room_python || true)"
if [ -n "$PY" ]; then
  (cd "$ROOM_ROOT" && "$PY" - <<'PYEOF'
import json
import sys

sys.path.insert(0, ".")
try:
    from app.config import get_config
    from app.poly_service import PolyService

    service = PolyService(get_config())
    service.detect()
    status = service.status()
    for role in ("camera", "microphone", "speaker"):
        info = status.get(role) or {}
        mark = {"ok": "\033[32m✓\033[0m", "warning": "\033[33m!\033[0m"}.get(
            info.get("status"), "\033[31m✗\033[0m"
        )
        name = info.get("description") or info.get("name") or info.get("path") or "not found"
        print(f"  {mark} {role:<11} {name}")
    missing = status.get("tools_missing") or []
    if missing:
        print("\n  Missing tools: " + ", ".join(missing))
except Exception as exc:  # noqa: BLE001
    print(f"  Could not ask the room software: {exc.__class__.__name__}: {exc}")
PYEOF
  ) || true
else
  warn "Python was not found, so the room software's own view is unavailable."
fi

printf '\n'
dim "Tip: after plugging the bar in, give it five seconds and run this again."
printf '\n'
