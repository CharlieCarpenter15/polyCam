#!/usr/bin/env bash
#
# Shared shell helpers. Sourced by the other scripts in this directory:
#   . "$(dirname "${BASH_SOURCE[0]}")/lib-room.sh"
#
# Reading YAML in shell is normally a mistake, so this does not try: it asks
# Python (which is installed anyway, and owns the configuration schema) for one
# value at a time and caches the answers. That means the scripts and the
# application can never disagree about what a setting means or what its default
# is.

# shellcheck shell=bash

ROOM_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOM_ROOT="$(dirname "$ROOM_LIB_DIR")"
ROOM_PYTHON="${ROOM_PYTHON:-$ROOM_ROOT/.venv/bin/python3}"
ROOM_CONFIG_CACHE=""

# ---------------------------------------------------------------- logging

# Structured, journald-friendly output: `event key=value key=value`.
room_log() {
  local event="$1"
  shift || true
  printf '%s' "$event" >&2
  local pair
  for pair in "$@"; do
    printf ' %s' "$pair" >&2
  done
  printf '\n' >&2
}

room_die() {
  room_log "$@"
  exit 1
}

# ---------------------------------------------------------------- python

room_python() {
  if [ -x "$ROOM_PYTHON" ]; then
    printf '%s\n' "$ROOM_PYTHON"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    return 1
  fi
}

# ---------------------------------------------------------------- config

# Load every setting once, as `KEY=value` lines, into a cache variable.
room_load_config() {
  local python
  python="$(room_python)" || {
    room_log "config.python_missing"
    return 1
  }
  ROOM_CONFIG_CACHE="$(
    cd "$ROOM_ROOT" && "$python" - <<'PYEOF' 2>/dev/null
import sys
sys.path.insert(0, ".")
try:
    from app.config import get_config
    config = get_config()
    for key, value in sorted(config.as_dict().items()):
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, list):
            rendered = "\x1f".join(str(item) for item in value)
        else:
            rendered = str(value)
        # Values are single-line by construction; guard anyway.
        print(f"{key}={rendered.splitlines()[0] if rendered else ''}")
except Exception as exc:  # noqa: BLE001 - scripts must not die on a bad config
    print(f"_ERROR={exc.__class__.__name__}")
PYEOF
  )"
  if [ -z "$ROOM_CONFIG_CACHE" ] || printf '%s' "$ROOM_CONFIG_CACHE" | grep -q '^_ERROR='; then
    room_log "config.load_failed" "note=using built-in defaults"
    ROOM_CONFIG_CACHE=""
    return 1
  fi
  return 0
}

# room_config KEY [DEFAULT]
room_config() {
  local key="$1"
  local fallback="${2-}"
  local line
  line="$(printf '%s\n' "$ROOM_CONFIG_CACHE" | grep -m1 "^${key}=" || true)"
  if [ -z "$line" ]; then
    printf '%s\n' "$fallback"
    return 0
  fi
  printf '%s\n' "${line#*=}"
}

# room_config_list KEY -> one item per line
room_config_list() {
  room_config "$1" "" | tr '\037' '\n'
}

room_is_true() {
  case "$(printf '%s' "${1-}" | tr '[:upper:]' '[:lower:]')" in
    1 | true | yes | on | y | enabled) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------- internal API

room_internal_token() {
  local token_file="${ROOM_APPLIANCE_VAR:-$ROOM_ROOT/var}/internal-token"
  if [ -r "$token_file" ]; then
    tr -d '\n' < "$token_file"
    return 0
  fi
  local python
  python="$(room_python)" || return 1
  (cd "$ROOM_ROOT" && "$python" -m app.main --print-internal-token 2>/dev/null)
}

# room_post_internal PATH JSON  — best effort; never fails the caller.
room_post_internal() {
  local path="$1"
  local body="$2"
  local port token
  port="$(room_config DASHBOARD_PORT 8080)"
  token="$(room_internal_token || true)"
  [ -n "$token" ] || return 0
  command -v curl >/dev/null 2>&1 || return 0
  curl --silent --show-error --max-time 4 \
    --header "Content-Type: application/json" \
    --header "X-Room-Internal-Token: $token" \
    --data "$body" \
    "http://127.0.0.1:${port}${path}" >/dev/null 2>&1 || return 0
}

# ---------------------------------------------------------------- waiting

# room_wait_for_url URL TIMEOUT_SECONDS
room_wait_for_url() {
  local url="$1"
  local timeout="${2:-60}"
  local waited=0
  command -v curl >/dev/null 2>&1 || return 0
  while [ "$waited" -lt "$timeout" ]; do
    if curl --silent --fail --max-time 2 --output /dev/null "$url"; then
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  return 1
}

# room_wait_for_network TIMEOUT_SECONDS — returns 0 once a name resolves.
room_wait_for_network() {
  local timeout="${1:-60}"
  local waited=0
  while [ "$waited" -lt "$timeout" ]; do
    if getent hosts deb.debian.org >/dev/null 2>&1 ||
       ping -c1 -W1 1.1.1.1 >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  return 1
}
