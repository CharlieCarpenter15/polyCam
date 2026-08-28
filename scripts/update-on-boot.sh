#!/usr/bin/env bash
#
# Pull the latest room software, then get out of the way.
#
# Run once at boot by room-update.service, before the backend and the kiosk
# start, so the room comes up on the current version without anyone visiting it.
# It can also be run by hand:
#
#     ./scripts/update-on-boot.sh --restart     # update now and restart the room
#     ./scripts/roomctl update                  # the same thing, friendlier
#
# The rules it follows, in order of importance:
#
#   1. **Never leave the room worse off.** Every failure path exits 0 and logs
#      why. A room that cannot reach GitHub at 07:58 must still show the 08:00
#      meeting, on yesterday's code.
#   2. **Never throw away someone's work.** A checkout with local modifications
#      is left completely alone, and only a fast-forward is ever taken — no
#      merge commits, no rebases, no resets.
#   3. **Finish the job.** If the pull brought new Python dependencies or new
#      systemd units, those are applied too; a half-updated appliance is worse
#      than one that never updated.
#
# Switched off with AUTO_UPDATE_ON_BOOT, and pointed at a particular branch
# with AUTO_UPDATE_BRANCH.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"

# shellcheck source=scripts/lib-room.sh
. "$HERE/lib-room.sh"

RESTART_AFTER=0
for arg in "$@"; do
  case "$arg" in
    --restart) RESTART_AFTER=1 ;;
    --help | -h)
      sed -n '3,26p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
  esac
done

room_load_config || true

ENABLED="$(room_config AUTO_UPDATE_ON_BOOT true)"
BRANCH_SETTING="$(room_config AUTO_UPDATE_BRANCH '')"

if [ "$RESTART_AFTER" -eq 0 ] && ! room_is_true "$ENABLED"; then
  room_log "update.disabled" "note=AUTO_UPDATE_ON_BOOT is off"
  exit 0
fi

command -v git >/dev/null 2>&1 || {
  room_log "update.no_git"
  exit 0
}
[ -d "$ROOT/.git" ] || {
  room_log "update.not_a_checkout" "dir=$ROOT"
  exit 0
}

cd "$ROOT" || exit 0

# Someone edited a file on the Pi. Pulling over the top of that would destroy
# work and produce a version that matches no commit anywhere.
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  room_log "update.local_changes" "note=staying on the current version"
  exit 0
fi

BRANCH="$BRANCH_SETTING"
if [ -z "$BRANCH" ]; then
  BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
fi
if [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; then
  room_log "update.detached_head" "note=set AUTO_UPDATE_BRANCH to update anyway"
  exit 0
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  room_log "update.no_remote"
  exit 0
fi

# At boot the network is usually a few seconds behind us.
room_wait_for_network 60 || room_log "update.network_slow" "note=trying anyway"

BEFORE="$(git rev-parse HEAD 2>/dev/null)"

fetched=0
for attempt in 1 2 3; do
  if timeout 120 git fetch --quiet origin "$BRANCH" 2>/dev/null; then
    fetched=1
    break
  fi
  room_log "update.fetch_retry" "attempt=$attempt" "branch=$BRANCH"
  sleep $((attempt * 5))
done

if [ "$fetched" -eq 0 ]; then
  room_log "update.fetch_failed" "branch=$BRANCH" "note=the room keeps its current version"
  exit 0
fi

if ! timeout 60 git merge --ff-only --quiet "origin/$BRANCH" 2>/dev/null; then
  room_log "update.not_fast_forward" "branch=$BRANCH" \
    "note=this checkout has diverged; fix it by hand with git"
  exit 0
fi

AFTER="$(git rev-parse HEAD 2>/dev/null)"

if [ "$BEFORE" = "$AFTER" ]; then
  room_log "update.already_current" "branch=$BRANCH" "commit=${AFTER:0:8}"
  exit 0
fi

CHANGED="$(git diff --name-only "$BEFORE" "$AFTER" 2>/dev/null)"
room_log "update.updated" "branch=$BRANCH" "from=${BEFORE:0:8}" "to=${AFTER:0:8}" \
  "files=$(printf '%s\n' "$CHANGED" | grep -c . || true)"

# ------------------------------------------------------ new dependencies
if printf '%s\n' "$CHANGED" | grep -q '^requirements'; then
  if [ -x "$ROOT/.venv/bin/pip" ]; then
    room_log "update.installing_dependencies"
    if timeout 600 "$ROOT/.venv/bin/pip" install --quiet --upgrade \
         -r "$ROOT/requirements.txt" 2>/dev/null; then
      room_log "update.dependencies_installed"
    else
      room_log "update.dependencies_failed" "note=run scripts/install.sh by hand"
    fi
  else
    room_log "update.no_venv" "note=run scripts/install.sh by hand"
  fi
fi

# ------------------------------------------------------------- new units
if printf '%s\n' "$CHANGED" | grep -q '^systemd/'; then
  UNIT_DIR="$HOME/.config/systemd/user"
  if command -v systemctl >/dev/null 2>&1 && [ -d "$UNIT_DIR" ]; then
    room_log "update.refreshing_units"
    for unit_file in "$ROOT"/systemd/*.service "$ROOT"/systemd/*.timer; do
      [ -f "$unit_file" ] || continue
      sed -e "s|__ROOM_DIR__|$ROOT|g" -e "s|__ROOM_USER__|${USER:-$(id -un)}|g" \
        "$unit_file" > "$UNIT_DIR/$(basename "$unit_file")" 2>/dev/null || true
    done
    systemctl --user daemon-reload 2>/dev/null || true
  fi
fi

# ------------------------------------------------------------- restart
#
# At boot there is nothing to restart: this unit is ordered before the backend
# and the kiosk, so they start on the new code by themselves. Any other time —
# an update triggered from the control panel, or by hand — the running backend
# is still on the old code, so it has to be restarted or the update is
# invisible until the next reboot. "Is the dashboard already up?" distinguishes
# the two without the script needing to be told which one it is.
if command -v systemctl >/dev/null 2>&1; then
  if [ "$RESTART_AFTER" -eq 1 ] || systemctl --user is-active --quiet room-dashboard.service; then
    room_log "update.restarting_room"
    systemctl --user restart room-dashboard.service 2>/dev/null || true
    systemctl --user restart room-kiosk.service 2>/dev/null || true
  fi
fi

exit 0
