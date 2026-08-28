#!/usr/bin/env bash
#
# Discover the key codes a Poly remote or controller actually sends.
#
#     ./scripts/diagnose-remote.sh            # guided: pick a device, press buttons
#     ./scripts/diagnose-remote.sh --list     # just list input devices
#     ./scripts/diagnose-remote.sh --all      # watch every device at once
#
# Poly models differ, so nothing is assumed. Press the button you care about,
# note the KEY_… name printed, and paste it into the matching field in
# Settings → Poly remote / controller. The same thing is available without a
# terminal from Settings → Checks → Discover buttons.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib-room.sh
. "$HERE/lib-room.sh"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
dim()  { printf '\033[2m%s\033[0m\n' "$1"; }

MODE="guided"
case "${1-}" in
  --list) MODE="list" ;;
  --all)  MODE="all" ;;
  -h | --help)
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
esac

# ------------------------------------------------------------ permissions

CURRENT_USER="${USER:-$(id -un)}"

if [ ! -d /dev/input ]; then
  bold "There is no /dev/input on this system"
  dim "Input devices appear there once a remote or keyboard is connected."
  dim "This is normal on a machine with no HID devices at all."
  exit 1
fi

if [ ! -r /dev/input ]; then
  bold "Cannot read /dev/input"
  dim "Add your user to the 'input' group, then log out and back in:"
  dim "    sudo usermod -aG input $CURRENT_USER"
  exit 1
fi

# ------------------------------------------------------------------ listing

list_devices() {
  if [ -r /proc/bus/input/devices ]; then
    awk '
      /^N: Name=/ { name = $0; sub(/^N: Name="/, "", name); sub(/"$/, "", name) }
      /^H: Handlers=/ {
        if (match($0, /event[0-9]+/)) {
          handler = substr($0, RSTART, RLENGTH)
          printf "  /dev/input/%-12s %s\n", handler, name
        }
      }
    ' /proc/bus/input/devices
  else
    for device in /dev/input/event*; do
      [ -e "$device" ] && printf '  %s\n' "$device"
    done
  fi
}

printf '\n'
bold "Input devices"
if list_devices | grep -q .; then
  list_devices
else
  dim "  None found. Is the remote paired / plugged in?"
fi
printf '\n'

# Highlight the ones that look like a conference remote.
bold "Likely remotes"
if list_devices | grep -iE 'poly|plantronics|polycom|studio|remote|consumer|cec|ir-recv' ; then
  :
else
  dim "  Nothing obviously a remote. A conference bar's own buttons often appear"
  dim "  as 'Consumer Control'; a Poly remote may appear by model name."
fi
printf '\n'

[ "$MODE" = "list" ] && exit 0

# ------------------------------------------------------------------ evtest

if ! command -v evtest >/dev/null 2>&1; then
  bold "evtest is not installed"
  dim "  sudo apt install evtest"
  printf '\n'
  dim "Falling back to the room software's own key capture…"
  PY="$(room_python || true)"
  if [ -n "$PY" ]; then
    (cd "$ROOM_ROOT" && "$PY" - <<'PYEOF'
import json
import sys

sys.path.insert(0, ".")
try:
    from app.config import get_config
    from app.remote_service import RemoteService

    service = RemoteService(get_config(), lambda action: None)
    print("Press buttons on the remote now (20 seconds)…", flush=True)
    result = service.capture_keys(20)
    if not result.get("ok"):
        print("  " + str(result.get("error")))
    elif not result.get("keys"):
        print("  No buttons were seen.")
    else:
        print("\n  Buttons detected:")
        for entry in result["keys"]:
            print(f"    {entry['key']:<22} from {entry['device']}")
except Exception as exc:  # noqa: BLE001
    print(f"  Failed: {exc.__class__.__name__}: {exc}")
PYEOF
    ) || true
  fi
  exit 0
fi

run_evtest() {
  local device="$1"
  bold "Watching $device"
  dim "  Press the buttons you want to map. Ctrl+C when finished."
  dim "  Look for lines like:  EV_KEY  KEY_ENTER  value 1   ← 1 means pressed"
  printf '\n'
  # Only show key presses; drop the capability dump and release/repeat noise.
  evtest "$device" 2>&1 | grep --line-buffered -E 'EV_KEY|Testing|Input device name' |
    grep --line-buffered -vE 'value 0$'
}

if [ "$MODE" = "all" ]; then
  bold "Watching every input device"
  dim "  Ctrl+C to stop."
  printf '\n'
  pids=()
  for device in /dev/input/event*; do
    [ -r "$device" ] || continue
    ( evtest "$device" 2>/dev/null | grep --line-buffered -E 'EV_KEY.*value 1' |
        sed "s|^|$(basename "$device"): |" ) &
    pids+=($!)
  done
  # shellcheck disable=SC2064  # expand pids now, on purpose
  trap "kill ${pids[*]} 2>/dev/null || true" EXIT INT TERM
  wait
  exit 0
fi

# Guided: offer a choice.
mapfile -t DEVICES < <(list_devices | awk '{print $1}')
if [ "${#DEVICES[@]}" -eq 0 ]; then
  exit 1
fi

bold "Which device is the remote?"
index=1
for device in "${DEVICES[@]}"; do
  label="$(list_devices | grep -m1 "^  $device " | sed "s|^  $device *||")"
  printf '  %2d) %-22s %s\n' "$index" "$device" "$label"
  index=$((index + 1))
done
printf '   a) all of them at once\n'
printf '\n'
printf 'Choice [1]: '
read -r choice
choice="${choice:-1}"

if [ "$choice" = "a" ]; then
  exec "$0" --all
fi

if ! printf '%s' "$choice" | grep -qE '^[0-9]+$' ||
   [ "$choice" -lt 1 ] || [ "$choice" -gt "${#DEVICES[@]}" ]; then
  bold "Not a valid choice."
  exit 1
fi

printf '\n'
run_evtest "${DEVICES[$((choice - 1))]}"
