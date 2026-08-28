#!/usr/bin/env bash
#
# Remove the appliance's services and system changes.
#
#     ./scripts/uninstall.sh              # stop and remove services
#     ./scripts/uninstall.sh --purge      # also delete config, logs and profile
#
# Without --purge, your configuration, background images and browser profile are
# left alone, so re-running scripts/install.sh restores the room exactly as it was.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"

PURGE=0
[ "${1-}" = "--purge" ] && PURGE=1
case "${1-}" in -h | --help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;; esac

good() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }

UNITS=(room-kiosk.service room-airplay.service room-remote.service
       room-watchdog.timer room-watchdog.service room-dashboard.service)

printf '\n\033[1mRemoving the meeting-room appliance\033[0m\n\n'

if command -v systemctl >/dev/null 2>&1; then
  for unit in "${UNITS[@]}"; do
    if systemctl --user stop "$unit" 2>/dev/null; then good "stopped $unit"; fi
    systemctl --user disable "$unit" 2>/dev/null >/dev/null || true
  done

  UNIT_DIR="$HOME/.config/systemd/user"
  for unit in "${UNITS[@]}"; do
    if [ -f "$UNIT_DIR/$unit" ]; then
      rm -f "$UNIT_DIR/$unit"
      good "removed $unit"
    fi
  done
  systemctl --user daemon-reload 2>/dev/null || true
  good "services removed"
else
  warn "systemd not found; nothing to remove."
fi

if [ -f /etc/sudoers.d/room-appliance ]; then
  if sudo rm -f /etc/sudoers.d/room-appliance; then
    good "removed the sudo rule"
  else
    warn "Could not remove /etc/sudoers.d/room-appliance"
  fi
fi

printf '\n'
printf 'Lingering is left enabled (it is harmless). To turn it off:\n'
printf '  sudo loginctl disable-linger %s\n\n' "$(id -un)"

if [ "$PURGE" -eq 1 ]; then
  printf 'This will delete your configuration, background images, browser profile\n'
  printf 'and cached meetings from %s.\n' "$ROOT"
  printf 'Type DELETE to confirm: '
  read -r answer
  if [ "$answer" = "DELETE" ]; then
    # ${ROOT:?} aborts rather than deleting /var if ROOT is somehow empty.
    rm -rf "${ROOT:?}/var" "${ROOT:?}/config/config.yaml" \
           "${ROOT:?}/config/config.yaml.bak" \
           "${ROOT:?}/config/config.yaml.broken" "${ROOT:?}/.venv"
    good "removed configuration, working files and the virtual environment"
  else
    warn "Not confirmed; nothing was deleted."
  fi
else
  printf 'Your configuration and data are still in %s\n' "$ROOT"
  printf 'Re-run ./scripts/install.sh to bring the room back.\n\n'
fi
