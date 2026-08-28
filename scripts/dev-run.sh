#!/usr/bin/env bash
#
# Run the appliance on a normal computer, with no room hardware.
#
#     ./scripts/dev-run.sh                 # mock calendar, no Chromium, port 8080
#     ./scripts/dev-run.sh --port 5000
#     ./scripts/dev-run.sh --real-calendar # use the configured ICS feed
#
# Development mode simulates the Poly bar and the AirPlay receiver and does not
# start Chromium, so the dashboard, the control panel and the settings page can
# all be worked on from a laptop. Open:
#
#     http://127.0.0.1:8080/          the TV dashboard
#     http://127.0.0.1:8080/panel     the phone control panel
#     http://127.0.0.1:8080/settings  settings
#
# Overrides are applied in memory only — your config.yaml is never modified.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT" || { printf 'Cannot enter %s\n' "$ROOT"; exit 1; }

PORT=8080
CALENDAR="mock"

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:-8080}"; shift ;;
    --real-calendar) CALENDAR="keep" ;;
    -h | --help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1"; exit 1 ;;
  esac
  shift
done

PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  if command -v python3 >/dev/null 2>&1; then
    printf 'No .venv found; using the system python3.\n'
    printf 'For a proper setup:  python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt\n\n'
    PYTHON="$(command -v python3)"
  else
    printf 'python3 is not installed.\n'
    exit 1
  fi
fi

# Keep development state out of the way of a real installation on the same box.
export ROOM_APPLIANCE_VAR="${ROOM_APPLIANCE_VAR:-$ROOT/var/dev}"
mkdir -p "$ROOM_APPLIANCE_VAR"

printf '\033[1mDevelopment mode\033[0m\n'
printf '  dashboard      http://127.0.0.1:%s/\n' "$PORT"
printf '  control panel  http://127.0.0.1:%s/panel\n' "$PORT"
printf '  settings       http://127.0.0.1:%s/settings\n' "$PORT"
printf '  calendar       %s\n' "$([ "$CALENDAR" = mock ] && echo 'mock (invented meetings)' || echo 'the configured ICS feed, if any')"
printf '  state          %s\n\n' "$ROOM_APPLIANCE_VAR"

# --dev uses the configured ICS feed when there is one, and the mock calendar
# when there is not. --real-calendar therefore only needs to *not* clear the URL.
if [ "$CALENDAR" = "mock" ]; then
  export ROOM_APPLIANCE_CALENDAR_SOURCE=mock
fi

ARGS=(--dev --port "$PORT" --host 127.0.0.1)

exec "$PYTHON" -m app.main "${ARGS[@]}"
