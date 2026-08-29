#!/usr/bin/env bash
#
# One-command installer for the meeting-room appliance.
#
#     ./scripts/install.sh
#
# Safe to run again at any time: it upgrades in place and never overwrites an
# existing configuration. Options:
#
#     --no-apt          skip apt package installation
#     --no-uxplay       skip AirPlay (UxPlay) setup
#     --no-lan-admin    keep the control panel on this Pi only
#     --pin 123456      set the admin PIN instead of generating one
#     --room "Name"     set the room name
#     --calendar URL    set the calendar ICS link
#     --unattended      never prompt; use defaults for everything
#
# What it does, in order:
#   1. checks it is running on something sensible, as the right user
#   2. installs apt packages
#   3. creates the Python virtual environment and installs dependencies
#   4. installs UxPlay for AirPlay (from apt, or builds it if apt has no package)
#   5. writes config/config.yaml if there is not one already
#   6. installs the systemd *user* units and enables lingering
#   7. adds the one sudo rule needed (reboot as a last-resort recovery)
#   8. starts everything and prints what to do next

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT" || { printf 'Cannot enter %s\n' "$ROOT"; exit 1; }

# ------------------------------------------------------------------ options

DO_APT=1
DO_UXPLAY=1
DO_LAN_ADMIN=1
UNATTENDED=0
SET_PIN=""
SET_ROOM=""
SET_CALENDAR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --no-apt) DO_APT=0 ;;
    --no-uxplay) DO_UXPLAY=0 ;;
    --no-lan-admin) DO_LAN_ADMIN=0 ;;
    --unattended) UNATTENDED=1 ;;
    --pin) SET_PIN="${2:-}"; shift ;;
    --room) SET_ROOM="${2:-}"; shift ;;
    --calendar) SET_CALENDAR="${2:-}"; shift ;;
    -h | --help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1"; exit 1 ;;
  esac
  shift
done

# ------------------------------------------------------------------ output

STEP=0
step()  { STEP=$((STEP + 1)); printf '\n\033[1m%d. %s\033[0m\n' "$STEP" "$1"; }
info()  { printf '   %s\n' "$1"; }
good()  { printf '   \033[32m✓\033[0m %s\n' "$1"; }
warn()  { printf '   \033[33m!\033[0m %s\n' "$1"; }
bad()   { printf '   \033[31m✗\033[0m %s\n' "$1"; }
die()   { bad "$1"; printf '\n'; exit 1; }

ask() {
  # ask "Question" "default" -> echoes the answer
  local question="$1" default="$2" answer
  if [ "$UNATTENDED" -eq 1 ] || [ ! -t 0 ]; then
    printf '%s' "$default"
    return 0
  fi
  printf '   %s [%s]: ' "$question" "$default" > /dev/tty
  read -r answer < /dev/tty || answer=""
  printf '%s' "${answer:-$default}"
}

printf '\n\033[1m╭──────────────────────────────────────────────╮\033[0m\n'
printf '\033[1m│  Meeting-room appliance installer             │\033[0m\n'
printf '\033[1m╰──────────────────────────────────────────────╯\033[0m\n'

# --------------------------------------------------------- 1. sanity checks

step "Checking this machine"

if [ "$(id -u)" -eq 0 ]; then
  die "Do not run this with sudo. Run it as the desktop user; it will ask for
     sudo only where it needs to."
fi

ROOM_USER="$(id -un)"
good "Installing for user '$ROOM_USER' in $ROOT"

if [ -r /proc/device-tree/model ]; then
  MODEL="$(tr -d '\0' < /proc/device-tree/model)"
  good "Hardware: $MODEL"
  case "$MODEL" in
    *"Raspberry Pi 5"*) ;;
    *"Raspberry Pi 4"*) warn "A Pi 4 works, but a Pi 5 handles AirPlay much better." ;;
    *) warn "Untested hardware. It will probably be fine." ;;
  esac
else
  warn "Not a Raspberry Pi. Continuing anyway (useful for development)."
fi

if [ "$(getconf LONG_BIT)" != "64" ]; then
  warn "This is a 32-bit system. A 64-bit Raspberry Pi OS is recommended."
fi

command -v python3 >/dev/null 2>&1 || die "python3 is not installed."
PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
good "Python $PY_VERSION"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' ||
  die "Python 3.9 or newer is required."

if ! command -v systemctl >/dev/null 2>&1; then
  warn "systemd was not found. Services cannot be installed; the app can still
     be run by hand with scripts/dev-run.sh."
fi

# ------------------------------------------------------------- 2. apt packages

step "Installing system packages"

APT_PACKAGES=(
  # Python and build essentials (python3-dev is needed to build evdev)
  python3-venv python3-pip python3-dev build-essential
  # Diagnostics and hardware control
  usbutils v4l-utils pulseaudio-utils evtest
  # Browser
  chromium-browser
  # Networking / discovery for AirPlay
  avahi-daemon avahi-utils
  # Generates the room's own certificate, which browsers require before they
  # will let a PC share its screen
  openssl
  # Reading what the Wi-Fi can do, for the Miracast readiness check
  iw
  # Handy in a kiosk
  unclutter curl jq
)

if [ "$DO_APT" -eq 1 ] && command -v apt-get >/dev/null 2>&1; then
  info "This needs sudo and may take a few minutes."
  sudo apt-get update -qq || warn "apt-get update had problems; continuing."

  # Install one at a time so a single unavailable package does not abort
  # everything — chromium is called chromium-browser on some releases and
  # chromium on others, for instance.
  MISSING=()
  for package in "${APT_PACKAGES[@]}"; do
    if dpkg -s "$package" >/dev/null 2>&1; then
      continue
    fi
    if sudo apt-get install -y -qq "$package" >/dev/null 2>&1; then
      good "installed $package"
    else
      MISSING+=("$package")
    fi
  done

  # Chromium fallback name.
  if ! command -v chromium-browser >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1; then
    sudo apt-get install -y -qq chromium >/dev/null 2>&1 && good "installed chromium"
  fi

  if [ "${#MISSING[@]}" -gt 0 ]; then
    warn "Could not install: ${MISSING[*]}"
    info "The appliance still works; diagnostics for those parts will say so."
  fi
else
  if [ "$DO_APT" -eq 0 ]; then
    info "Skipped (--no-apt)."
  else
    warn "apt-get not found; skipping packages."
  fi
fi

# Reading the remote's buttons needs the 'input' group.
if ! id -nG "$ROOM_USER" | tr ' ' '\n' | grep -qx input; then
  if sudo usermod -aG input "$ROOM_USER" 2>/dev/null; then
    good "added $ROOM_USER to the 'input' group (takes effect after reboot)"
  else
    warn "Could not add $ROOM_USER to the 'input' group; the Poly remote will
     not work until you run: sudo usermod -aG input $ROOM_USER"
  fi
else
  good "$ROOM_USER is already in the 'input' group"
fi

if command -v systemctl >/dev/null 2>&1; then
  if sudo systemctl enable --now avahi-daemon >/dev/null 2>&1; then
    good "avahi-daemon enabled (needed for AirPlay discovery)"
  else
    warn "Could not enable avahi-daemon; AirPlay may not be discoverable."
  fi
fi

# --------------------------------------------------- 3. python environment

step "Creating the Python environment"

VENV="$ROOT/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV" || die "Could not create the virtual environment in $VENV"
  good "created $VENV"
else
  good "reusing $VENV"
fi

"$VENV/bin/pip" install --quiet --upgrade pip setuptools wheel >/dev/null 2>&1 || true

if "$VENV/bin/pip" install --quiet -r "$ROOT/requirements.txt"; then
  good "installed Python dependencies"
else
  die "Could not install Python dependencies. Check the network and retry."
fi

# evdev is optional: the remote is optional, and it needs a compiler.
if "$VENV/bin/pip" install --quiet -r "$ROOT/requirements-optional.txt" 2>/dev/null; then
  good "installed evdev (Poly remote support)"
else
  warn "evdev could not be built; the Poly remote will be unavailable."
  info "To fix later: sudo apt install python3-dev build-essential"
  info "              .venv/bin/pip install -r requirements-optional.txt"
fi

# ------------------------------------------------------------- 4. uxplay

step "Setting up AirPlay (UxPlay)"

install_uxplay_from_apt() {
  sudo apt-get install -y -qq uxplay >/dev/null 2>&1
}

build_uxplay() {
  info "Building UxPlay from source (5–10 minutes on a Pi 5)…"
  local deps=(
    cmake build-essential pkg-config libssl-dev libplist-dev
    libavahi-compat-libdnssd-dev
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good
    gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-gl
    gstreamer1.0-gtk3 gstreamer1.0-tools git
  )
  sudo apt-get install -y -qq "${deps[@]}" >/dev/null 2>&1 ||
    { warn "Could not install UxPlay's build dependencies."; return 1; }

  local build_dir="$ROOT/var/build/uxplay"
  rm -rf "$build_dir"
  mkdir -p "$(dirname "$build_dir")"
  git clone --depth 1 https://github.com/FDH2/UxPlay "$build_dir" >/dev/null 2>&1 ||
    { warn "Could not download UxPlay."; return 1; }
  (
    cd "$build_dir" &&
    cmake . >/dev/null 2>&1 &&
    make -j"$(nproc)" >/dev/null 2>&1 &&
    sudo make install >/dev/null 2>&1
  ) || { warn "UxPlay did not build."; return 1; }
  rm -rf "$build_dir"
  return 0
}

if [ "$DO_UXPLAY" -eq 0 ]; then
  info "Skipped (--no-uxplay). AirPlay will report as unavailable."
elif command -v uxplay >/dev/null 2>&1; then
  good "uxplay is already installed ($(uxplay -v 2>&1 | head -1 || echo 'version unknown'))"
elif ! command -v apt-get >/dev/null 2>&1; then
  warn "No apt; install UxPlay by hand for AirPlay support."
elif install_uxplay_from_apt && command -v uxplay >/dev/null 2>&1; then
  good "installed uxplay from apt"
else
  answer="$(ask "apt has no uxplay package. Build it from source? (y/n)" "y")"
  case "$answer" in
    y | Y | yes)
      if build_uxplay && command -v uxplay >/dev/null 2>&1; then
        good "built and installed uxplay"
      else
        warn "AirPlay is unavailable for now. Everything else works."
        info "You can retry later with: ./scripts/install.sh --no-apt"
      fi
      ;;
    *) info "Skipped. AirPlay will report as unavailable." ;;
  esac
fi

# ------------------------------------------------------- 5. configuration

step "Writing the configuration"

mkdir -p "$ROOT/config" "$ROOT/var"
chmod 700 "$ROOT/var" 2>/dev/null || true

CONFIG_FILE="$ROOT/config/config.yaml"
FRESH_INSTALL=0

if [ -f "$CONFIG_FILE" ]; then
  good "keeping the existing $CONFIG_FILE"
else
  FRESH_INSTALL=1

  ROOM_NAME_VALUE="$SET_ROOM"
  [ -n "$ROOM_NAME_VALUE" ] || ROOM_NAME_VALUE="$(ask "Room name" "Meeting Room")"

  PIN_VALUE="$SET_PIN"
  if [ -z "$PIN_VALUE" ] && [ "$DO_LAN_ADMIN" -eq 1 ]; then
    # A generated PIN is better than a memorable one nobody changes.
    PIN_VALUE="$(tr -dc '0-9' < /dev/urandom | head -c 6)"
  fi

  LAN_VALUE="false"
  [ "$DO_LAN_ADMIN" -eq 1 ] && [ -n "$PIN_VALUE" ] && LAN_VALUE="true"

  ROOM_NAME_VALUE="$ROOM_NAME_VALUE" \
  PIN_VALUE="$PIN_VALUE" \
  LAN_VALUE="$LAN_VALUE" \
  CALENDAR_VALUE="$SET_CALENDAR" \
  "$VENV/bin/python" - << 'PYEOF'
import os
import sys

sys.path.insert(0, ".")
from app.config import ConfigManager
from app import paths

manager = ConfigManager(paths.CONFIG_FILE)
values = {
    "ROOM_NAME": os.environ.get("ROOM_NAME_VALUE") or "Meeting Room",
    "ADMIN_PIN": os.environ.get("PIN_VALUE") or "",
    "ADMIN_LAN_ACCESS": os.environ.get("LAN_VALUE") == "true",
}
calendar = os.environ.get("CALENDAR_VALUE") or ""
if calendar:
    values["CALENDAR_SOURCE"] = "ics"
    values["CALENDAR_ICS_URL"] = calendar

changed, errors = manager.update(values)
if errors:
    for key, problem in errors.items():
        print(f"   ! {key}: {problem}", file=sys.stderr)
    sys.exit(1)
print(f"   wrote {len(manager.as_dict())} settings")
PYEOF
  good "created $CONFIG_FILE"
fi

chmod 600 "$CONFIG_FILE" 2>/dev/null || true

# The example file is regenerated so it always documents the real schema.
if "$VENV/bin/python" "$HERE/gen-config-docs.py" --quiet 2>/dev/null; then
  good "refreshed config/config.example.yaml"
fi

# ----------------------------------------------------------- 6. systemd

step "Installing services"

UNIT_DIR="$HOME/.config/systemd/user"

if command -v systemctl >/dev/null 2>&1; then
  mkdir -p "$UNIT_DIR"
  for unit_file in "$ROOT"/systemd/*.service "$ROOT"/systemd/*.timer; do
    name="$(basename "$unit_file")"
    sed -e "s|__ROOM_DIR__|$ROOT|g" -e "s|__ROOM_USER__|$ROOM_USER|g" \
      "$unit_file" > "$UNIT_DIR/$name"
    good "installed $name"
  done

  systemctl --user daemon-reload

  # Lingering makes the user's services start at boot without anyone logging in.
  if sudo loginctl enable-linger "$ROOM_USER" 2>/dev/null; then
    good "enabled lingering for $ROOM_USER (services start at boot)"
  else
    warn "Could not enable lingering. Services will only start after login:
     sudo loginctl enable-linger $ROOM_USER"
  fi

  if systemctl --user enable room-dashboard.service room-kiosk.service \
       room-airplay.service room-remote.service room-watchdog.timer \
       room-update.service >/dev/null 2>&1; then
    good "enabled all services at boot"
  else
    warn "Some services could not be enabled; check: systemctl --user status"
  fi
else
  warn "No systemd; skipping service installation."
fi

# ------------------------------------------------------------ 7. sudo rule

step "Adding the one sudo rule"

# Restarting services needs no privileges (they are user units). The only
# privileged action is rebooting as a last-resort recovery, so that is all the
# rule permits — no general sudo access for the room account.
SUDOERS_FILE="/etc/sudoers.d/room-appliance"
SUDOERS_LINE="$ROOM_USER ALL=(root) NOPASSWD: /bin/systemctl reboot, /usr/bin/systemctl reboot, /sbin/reboot, /sbin/shutdown -r now"

if [ -d /etc/sudoers.d ]; then
  if printf '# Installed by room-appliance scripts/install.sh\n# Lets the room reboot itself if it cannot be recovered any other way.\n%s\n' \
       "$SUDOERS_LINE" | sudo tee "$SUDOERS_FILE" >/dev/null 2>&1; then
    sudo chmod 440 "$SUDOERS_FILE"
    if sudo visudo -cf "$SUDOERS_FILE" >/dev/null 2>&1; then
      good "reboot permission granted (nothing else)"
    else
      sudo rm -f "$SUDOERS_FILE"
      warn "The sudo rule was invalid and has been removed; self-reboot is off."
    fi
  else
    warn "Could not write $SUDOERS_FILE; the watchdog cannot reboot the Pi."
  fi
else
  warn "No /etc/sudoers.d; the watchdog cannot reboot the Pi."
fi

# --------------------------------------------------------------- 8. start

step "Starting the room"

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user restart room-dashboard.service 2>/dev/null || true
  sleep 3
  PORT="$("$VENV/bin/python" -c "
import sys; sys.path.insert(0, '.')
from app.config import get_config
print(get_config().int_('DASHBOARD_PORT'))
" 2>/dev/null || echo 8080)"

  if curl --silent --fail --max-time 8 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    good "the room software is running on port $PORT"
  else
    warn "The backend has not answered yet. Check: journalctl --user -u room-dashboard -n 40"
  fi

  for unit in room-airplay.service room-remote.service room-watchdog.timer; do
    systemctl --user restart "$unit" 2>/dev/null || true
  done
  # The kiosk needs a graphical session, which may not exist over SSH.
  systemctl --user restart room-kiosk.service 2>/dev/null || true
fi

# ------------------------------------------------------------- next steps

CONFIGURED_PIN="$("$VENV/bin/python" -c "
import sys; sys.path.insert(0, '.')
from app.config import get_config
print(get_config().str_('ADMIN_PIN'))
" 2>/dev/null || echo "")"
LAN_ON="$("$VENV/bin/python" -c "
import sys; sys.path.insert(0, '.')
from app.config import get_config
print('yes' if get_config().bool_('ADMIN_LAN_ACCESS') else 'no')
" 2>/dev/null || echo no)"
PORT="${PORT:-8080}"
ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -n "$ADDRESS" ]; then ADDRESS_OR_HOST="$ADDRESS"; else ADDRESS_OR_HOST="$(hostname).local"; fi

printf '\n\033[1m╭──────────────────────────────────────────────╮\033[0m\n'
printf '\033[1m│  Installed                                    │\033[0m\n'
printf '\033[1m╰──────────────────────────────────────────────╯\033[0m\n\n'

if [ "$LAN_ON" = "yes" ]; then
  printf '  \033[1mOpen this on your phone:\033[0m\n'
  printf '     \033[32mhttp://%s:%s/panel\033[0m\n' "$ADDRESS_OR_HOST" "$PORT"
  [ -n "$CONFIGURED_PIN" ] && printf '     PIN: \033[1m%s\033[0m\n' "$CONFIGURED_PIN"
  printf '\n'
else
  printf '  Control panel (this Pi only): http://127.0.0.1:%s/panel\n' "$PORT"
  printf '  To reach it from a phone:      ./scripts/roomctl lan-admin on 123456\n\n'
fi

printf '  \033[1mStill to do:\033[0m\n'
if [ "$FRESH_INSTALL" -eq 1 ] && [ -z "$SET_CALENDAR" ]; then
  printf '   • Add the room calendar (Settings → Calendar), or from a terminal:\n'
  printf '       ./scripts/roomctl calendar "https://…/room.ics"\n'
fi
printf '   • Sign the room accounts into Teams / Google Meet once, on the TV:\n'
printf '       see “First-time account sign-in” in README.md\n'
printf '   • Then reboot to confirm everything comes back on its own:\n'
printf '       \033[1msudo reboot\033[0m\n\n'

printf '  \033[1mUseful commands:\033[0m\n'
printf '   ./scripts/roomctl status      how is the room?\n'
printf '   ./scripts/roomctl doctor      check the conference bar and remote\n'
printf '   ./scripts/roomctl logs -f     watch the logs\n'
printf '   ./scripts/roomctl restart all restart everything\n\n'
