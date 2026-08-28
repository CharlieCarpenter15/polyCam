#!/usr/bin/env bash
#
# Installs the meeting-minutes engines and the model files they need.
#
#     ./scripts/install-minutes.sh
#
# Safe to run again at any time: anything already downloaded and intact is
# checked, reported and left alone, and nothing is replaced without --force.
# Options:
#
#     --whisper SIZE    tiny.en | base.en | small.en (default: by hardware)
#     --models-only     only the model files and the whisper.cpp binary
#     --pip-only        only the Python packages
#     --venv PATH       the virtual environment to install into (default .venv)
#     --force           re-download model files that are already present
#     --quiet           report only problems (used by install.sh --with-minutes)
#
# What it does, in order:
#   1. asks the appliance what kind of machine this is and picks a speech
#      model to match — or, on a Pi 3, refuses to install one at all
#   2. installs the pip extras from requirements-minutes.txt, one group at a
#      time, so a failure in one does not lose the others
#   3. downloads the face, voice and speech models into var/minutes/models
#   4. installs the whisper.cpp command-line binary
#
# Nothing here is fetched at the moment somebody needs it: this script is the
# only part of the appliance that reaches out to the internet on purpose.
#
# Every download goes over HTTPS with certificate checking, lands under a
# temporary name in the destination directory, and is renamed into place only
# once it is the expected size, is not an HTML error page, and starts with the
# right magic bytes — so a half-finished file or a redirect to a login page is
# never left behind. The SHA-256 of whatever arrived is recorded in
# var/minutes/models/manifest.sha256, which a later run compares against and two
# appliances can be compared with. If scripts/minutes-models.sha256 exists and
# names a file, that checksum is enforced on it instead. To pin the models to
# what is on an appliance you trust, copy that appliance's manifest over:
#
#     cp var/minutes/models/manifest.sha256 scripts/minutes-models.sha256

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT" || { printf 'Cannot enter %s\n' "$ROOT"; exit 1; }

# shellcheck source=scripts/lib-room.sh
. "$HERE/lib-room.sh"

# ------------------------------------------------------------------ options

QUIET=0
FORCE=0
DO_PIP=1
DO_MODELS=1
VENV="$ROOT/.venv"
WHISPER_SIZE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --quiet) QUIET=1 ;;
    --force) FORCE=1 ;;
    --models-only) DO_PIP=0 ;;
    --pip-only) DO_MODELS=0 ;;
    --venv) VENV="${2:-}"; shift ;;
    --whisper) WHISPER_SIZE="${2:-}"; shift ;;
    -h | --help) sed -n '2,39p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$1"; exit 1 ;;
  esac
  shift
done

# ------------------------------------------------------------------ output

STEP=0
step()   { [ "$QUIET" -eq 1 ] && return 0; STEP=$((STEP + 1)); printf '\n\033[1m%d. %s\033[0m\n' "$STEP" "$1"; }
info()   { [ "$QUIET" -eq 1 ] && return 0; printf '   %s\n' "$1"; }
good()   { [ "$QUIET" -eq 1 ] && return 0; printf '   \033[32m✓\033[0m %s\n' "$1"; }
notice() { printf '   %s\n' "$1"; }
warn()   { printf '   \033[33m!\033[0m %s\n' "$1"; }
bad()    { printf '   \033[31m✗\033[0m %s\n' "$1"; }
die()    { bad "$1"; printf '\n'; exit 1; }

# ------------------------------------------------------------------ settings

# The writable tree, exactly as app/paths.py works it out.
VAR_DIR="${ROOM_APPLIANCE_VAR:-$ROOT/var}"
VAR_DIR="${VAR_DIR/#\~/$HOME}"
MODELS_DIR="$VAR_DIR/minutes/models"
MANIFEST="$MODELS_DIR/manifest.sha256"
PINS_FILE="$HERE/minutes-models.sha256"
REQUIREMENTS="$ROOT/requirements-minutes.txt"
BUILD_LOG="$VAR_DIR/whisper-build.log"
PIP="$VENV/bin/pip"

# Every question about this machine is put to the appliance's own Python, so
# that --venv steers the hardware probe as well as the packages. lib-room.sh
# falls back to the system python3 when there is no virtual environment yet.
if [ -x "$VENV/bin/python3" ]; then
  ROOM_PYTHON="$VENV/bin/python3"
fi

# app/minutes/paths.py creates every directory under var/minutes mode 0700 and
# writes every file mode 0600, because a recording holds people's voices. The
# models are not private, but they live in that tree, so they follow its rules.
DIR_MODE=700
FILE_MODE=600

#: The names whisper.cpp's CLI has had, newest first. This must stay the same
#: list as _BINARY_ALIASES["whisper-cpp"] in app/minutes/deps.py — installing a
#: binary under a name the appliance does not look for helps nobody. A test
#: compares the two.
WHISPER_ALIASES=(whisper-cli whisper-cpp whisper main)

#: Where the OpenCV Zoo publishes the face models. The same URL as
#: app/minutes/faces.py shows on the Settings page.
ZOO_URL="https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"

#: sherpa-onnx's speaker-embedding models. TitaNet-small is the one worth
#: having on a Pi: 40 MB, and no PyTorch anywhere near it.
SHERPA_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models"

#: The ggml speech models, as whisper.cpp's own models/download-ggml-model.sh
#: fetches them.
GGML_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main"

#: Where whisper.cpp lives now. github.com/ggerganov/whisper.cpp redirects here.
WHISPER_REPO="https://github.com/ggml-org/whisper.cpp"
WHISPER_API="https://api.github.com/repos/ggml-org/whisper.cpp/releases/latest"

FAILURES=0
fail() { FAILURES=$((FAILURES + 1)); }

# ------------------------------------------------------------------ helpers

have() { command -v "$1" >/dev/null 2>&1; }

# human 38696353 -> "37 MB"
human() {
  local bytes="${1:-0}"
  case "$bytes" in '' | *[!0-9]*) printf 'an unknown size'; return 0 ;; esac
  if [ "$bytes" -ge 1048576 ]; then
    printf '%d MB' "$(((bytes + 524288) / 1048576))"
  elif [ "$bytes" -ge 1024 ]; then
    printf '%d KB' "$(((bytes + 512) / 1024))"
  else
    printf '%d bytes' "$bytes"
  fi
}

file_size() { wc -c < "$1" 2>/dev/null | tr -d ' '; }

# The first COUNT bytes of a file, as lower-case hex with no separators.
first_bytes() {
  od -An -v -tx1 -N "${2:-4}" "$1" 2>/dev/null | tr -d ' \n'
}

# Does this file start the way that kind of file starts?
#
#   onnx  a protobuf ModelProto: field 1 (ir_version) is a varint, so the first
#         byte is 0x08 and the second is a small number with the top bit clear.
#   ggml  whisper.cpp's GGML_FILE_MAGIC, 0x67676d6c, written little-endian —
#         "lmgg" on disk — or the newer "GGUF".
magic_ok() {
  local hex second
  hex="$(first_bytes "$1" 4)"
  case "${2:-}" in
    onnx)
      [ "${hex:0:2}" = "08" ] || return 1
      second="${hex:2:2}"
      [ -n "$second" ] || return 1
      [ "$((16#$second))" -lt 128 ]
      ;;
    ggml)
      [ "${hex:0:8}" = "6c6d6767" ] || [ "${hex:0:8}" = "47475546" ]
      ;;
    *) return 1 ;;
  esac
}

magic_description() {
  case "${1:-}" in
    onnx) printf 'an ONNX model (a protobuf header)' ;;
    ggml) printf 'a ggml or gguf model' ;;
    *) printf 'the expected format' ;;
  esac
}

# A server that wants a login, or one that lost the file, answers with a page.
looks_like_html() {
  head -c 512 "$1" 2>/dev/null | LC_ALL=C tr -d '\000' |
    LC_ALL=C grep -qiE '<(!doctype|html|head|body|title)'
}

sha_of() {
  if have sha256sum; then
    sha256sum "$1" 2>/dev/null | cut -d' ' -f1
  elif have shasum; then
    shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1
  fi
}

# The recorded checksum for one file, from a `sha256sum` format list.
sha_from_list() {
  local list="$1" want="$2"
  [ -r "$list" ] || return 0
  awk -v want="$want" '
    /^[[:space:]]*#/ { next }
    NF >= 2 { name = $2; sub(/^\*/, "", name); if (name == want) { print $1; exit } }
  ' "$list" 2>/dev/null
}

# Replace one line of the manifest, atomically, the way app/store.py writes.
record_sha() {
  local name="$1" sha="$2" tmp
  [ -n "$sha" ] || return 0
  tmp="$(mktemp "$MODELS_DIR/.manifest.XXXXXX" 2>/dev/null)" || return 0
  if [ -r "$MANIFEST" ]; then
    awk -v want="$name" '
      NF >= 2 { name = $2; sub(/^\*/, "", name); if (name == want) next }
      { print }
    ' "$MANIFEST" > "$tmp" 2>/dev/null
  fi
  printf '%s  %s\n' "$sha" "$name" >> "$tmp"
  LC_ALL=C sort -k2,2 -o "$tmp" "$tmp" 2>/dev/null
  chmod "$FILE_MODE" "$tmp" 2>/dev/null
  mv -f "$tmp" "$MANIFEST" 2>/dev/null || rm -f "$tmp"
}

# Free megabytes on the filesystem holding DIR (or its nearest existing
# parent). Empty when df cannot say, which is never treated as "full".
free_mb() {
  local dir="$1" kilobytes
  while [ ! -d "$dir" ] && [ "$dir" != "/" ] && [ -n "$dir" ]; do
    dir="$(dirname "$dir")"
  done
  kilobytes="$(df -Pk "$dir" 2>/dev/null | awk 'NR == 2 { print $4 }')"
  case "$kilobytes" in
    '' | *[!0-9]*) printf '' ;;
    *) printf '%d' "$((kilobytes / 1024))" ;;
  esac
}

# require_space DIR NEEDED_MB WHAT — 1 and a sentence when there is not room.
require_space() {
  local dir="$1" needed="$2" what="$3" available
  available="$(free_mb "$dir")"
  [ -n "$available" ] || return 0
  [ "$available" -lt "$needed" ] || return 0
  warn "There is not enough disk space for $what."
  info "$dir has $available MB free and this needs about $needed MB."
  info "Free some up and run this again. The usual culprits:"
  info "    sudo apt clean"
  info "    sudo journalctl --vacuum-size=50M"
  info "    ./scripts/roomctl minutes prune      (old recordings)"
  return 1
}

# Files written as root would be unreadable by the account the appliance runs
# as, since everything in this tree is mode 0600.
ROOT_OWNER="$(stat -c '%U:%G' "$ROOT" 2>/dev/null || true)"
fix_ownership() {
  [ "$(id -u)" -eq 0 ] || return 0
  [ -n "$ROOT_OWNER" ] || return 0
  chown "$ROOT_OWNER" "$@" 2>/dev/null || true
}

ensure_models_dir() {
  mkdir -p "$MODELS_DIR" 2>/dev/null || return 1
  chmod "$DIR_MODE" "$VAR_DIR/minutes" "$MODELS_DIR" 2>/dev/null || true
  return 0
}

# --------------------------------------------------------------- verification

#: Set by verify_file to the reason the file was rejected.
VERIFY_REASON=""

# verify_file PATH NAME MIN MAX KIND — is this really the model we asked for?
verify_file() {
  local path="$1" name="$2" min="$3" max="$4" kind="$5"
  local size pin sha
  VERIFY_REASON=""

  size="$(file_size "$path")"
  if [ -z "$size" ] || [ "$size" -eq 0 ]; then
    VERIFY_REASON="it is empty"
    return 1
  fi
  if looks_like_html "$path"; then
    VERIFY_REASON="the server sent a web page (a login or error page), not a model"
    return 1
  fi
  if [ "$size" -lt "$min" ] || [ "$size" -gt "$max" ]; then
    VERIFY_REASON="it is $(human "$size") and this file should be between $(human "$min") and $(human "$max")"
    return 1
  fi
  if ! magic_ok "$path" "$kind"; then
    VERIFY_REASON="it does not start like $(magic_description "$kind")"
    return 1
  fi

  pin="$(sha_from_list "$PINS_FILE" "$name")"
  if [ -n "$pin" ]; then
    sha="$(sha_of "$path")"
    if [ -z "$sha" ]; then
      VERIFY_REASON="its checksum is pinned but sha256sum is not installed to check it"
      return 1
    fi
    if [ "$sha" != "$pin" ]; then
      VERIFY_REASON="its checksum is $sha, and $(basename "$PINS_FILE") pins it to $pin"
      return 1
    fi
  fi
  return 0
}

# ------------------------------------------------------------------ downloads

# fetch_model NAME URL MIN MAX KIND PURPOSE
fetch_model() {
  local name="$1" url="$2" min="$3" max="$4" kind="$5" purpose="$6"
  local dest="$MODELS_DIR/$name"
  local tmp size sha recorded curl_error

  if [ -f "$dest" ] && [ "$FORCE" -eq 0 ]; then
    if verify_file "$dest" "$name" "$min" "$max" "$kind"; then
      size="$(file_size "$dest")"
      good "$name is already here ($(human "$size")); left alone"
      sha="$(sha_of "$dest")"
      recorded="$(sha_from_list "$MANIFEST" "$name")"
      if [ -n "$sha" ] && [ -n "$recorded" ] && [ "$sha" != "$recorded" ]; then
        warn "$name has changed since it was installed."
        info "recorded $recorded"
        info "now      $sha"
        info "Replace it with: scripts/install-minutes.sh --force"
      elif [ -n "$sha" ] && [ -z "$recorded" ]; then
        record_sha "$name" "$sha"
      fi
      return 0
    fi
    warn "$name is here but $VERIFY_REASON."
    info "It has been left where it is rather than deleted. Replace it with:"
    info "    scripts/install-minutes.sh --force"
    fail
    return 1
  fi

  require_space "$MODELS_DIR" "$((max / 1048576 + 64))" "$name" || { fail; return 1; }

  if [ -f "$dest" ]; then
    info "replacing $name (--force)"
  fi
  info "fetching $name — $purpose"

  if ! have curl; then
    warn "curl is not installed, so $name cannot be downloaded."
    info "Install it with: sudo apt install curl"
    fail
    return 1
  fi

  tmp="$(mktemp "$MODELS_DIR/.$name.XXXXXX" 2>/dev/null)" || {
    warn "Could not create a temporary file in $MODELS_DIR."
    info "Check that it exists and is writable by $(id -un)."
    fail
    return 1
  }
  chmod "$FILE_MODE" "$tmp" 2>/dev/null || true

  # --proto '=https' refuses a redirect down to plain HTTP; certificates are
  # verified because curl verifies them unless it is told not to, and it is not.
  if ! curl_error="$(
    curl --fail --location --silent --show-error \
      --proto '=https' --tlsv1.2 \
      --connect-timeout 20 --max-time 3600 \
      --retry 2 --retry-delay 3 \
      --output "$tmp" "$url" 2>&1
  )"; then
    rm -f "$tmp"
    warn "Could not download $name."
    [ -n "$curl_error" ] && info "curl: $curl_error"
    info "It comes from $url"
    info "Check the room's internet connection and any proxy, then run this"
    info "again — nothing already installed will be downloaded twice."
    info "It can also be staged by hand: download it on a machine that can"
    info "reach that address and copy it into $MODELS_DIR"
    fail
    return 1
  fi

  if ! verify_file "$tmp" "$name" "$min" "$max" "$kind"; then
    rm -f "$tmp"
    warn "What arrived for $name was rejected: $VERIFY_REASON."
    info "Nothing was written. The file came from $url"
    info "If this room is behind a filtering proxy, that is the usual cause."
    fail
    return 1
  fi

  size="$(file_size "$tmp")"
  if ! mv -f "$tmp" "$dest"; then
    rm -f "$tmp"
    warn "Could not move $name into $MODELS_DIR."
    info "Check the free space and the permissions on that directory."
    fail
    return 1
  fi
  chmod "$FILE_MODE" "$dest" 2>/dev/null || true
  fix_ownership "$dest"
  record_sha "$name" "$(sha_of "$dest")"
  good "downloaded $name ($(human "$size"))"
  return 0
}

# ------------------------------------------------- 1. what machine is this?

PROFILE=""
PI_GENERATION=0
MACHINE="unknown hardware"

detect_hardware() {
  local python probe
  python="$(room_python 2>/dev/null)" || return 1
  probe="$(
    cd "$ROOT" && "$python" - << 'PYEOF' 2>/dev/null
import sys

sys.path.insert(0, ".")

configured = "auto"
try:
    # An administrator who has pinned PERFORMANCE_PROFILE means it, and
    # app/minutes/transcribe.py obeys the same value when it decides whether
    # this machine may transcribe at all.
    from app.config import get_config

    configured = get_config().str_("PERFORMANCE_PROFILE") or "auto"
except Exception:  # noqa: BLE001 - no config yet is normal during install
    pass

try:
    from app import hardware_profile

    profile, machine = hardware_profile.resolve(configured)
    print(f"profile={profile}")
    print(f"generation={machine.pi_generation}")
    print(f"description={machine.describe()}")
except Exception as exc:  # noqa: BLE001 - never stop the installer
    print(f"error={exc.__class__.__name__}")
PYEOF
  )" || return 1
  [ -n "$probe" ] || return 1
  PROFILE="$(printf '%s\n' "$probe" | sed -n 's/^profile=//p' | head -1)"
  PI_GENERATION="$(printf '%s\n' "$probe" | sed -n 's/^generation=//p' | head -1)"
  MACHINE="$(printf '%s\n' "$probe" | sed -n 's/^description=//p' | head -1)"
  case "$PI_GENERATION" in '' | *[!0-9]*) PI_GENERATION=0 ;; esac
  [ -n "$MACHINE" ] || MACHINE="unknown hardware"
  [ -n "$PROFILE" ]
}

# base.en on a Pi 5 or better, tiny.en on a Pi 4, nothing on a Pi 3.
whisper_for_hardware() {
  if [ "$PI_GENERATION" -ge 5 ]; then
    printf 'base.en'
  elif [ "$PI_GENERATION" -eq 4 ]; then
    printf 'tiny.en'
  elif [ "$PROFILE" = "high" ]; then
    printf 'base.en'
  else
    printf 'tiny.en'
  fi
}

# ------------------------------------------------------------ 2. pip extras

# The pinned line for one distribution, read out of requirements-minutes.txt so
# that this script never becomes a second place where versions are decided.
requirement_for() {
  local want="$1" line spec name
  [ -r "$REQUIREMENTS" ] || return 0
  while IFS= read -r line; do
    spec="$(printf '%s' "${line%%#*}" | tr -d '[:space:]')"
    [ -n "$spec" ] || continue
    name="${spec%%[<>=!~;[]*}"
    name="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | tr '_.' '--')"
    if [ "$name" = "$want" ]; then
      printf '%s' "$spec"
      return 0
    fi
  done < "$REQUIREMENTS"
  return 0
}

# install_group LABEL NAME... — one logical group, reported on its own, so that
# a wheel that will not build cannot take the rest of the feature down with it.
install_group() {
  local label="$1"
  shift
  local specs=() name spec output
  for name in "$@"; do
    spec="$(requirement_for "$name")"
    if [ -z "$spec" ]; then
      warn "$name is not listed in $(basename "$REQUIREMENTS"); skipping it."
      continue
    fi
    specs+=("$spec")
  done
  if [ "${#specs[@]}" -eq 0 ]; then
    return 1
  fi
  if output="$("$PIP" install --quiet "${specs[@]}" 2>&1)"; then
    good "$label (${specs[*]})"
    return 0
  fi
  warn "$label could not be installed."
  [ -n "$output" ] && info "pip: $(printf '%s\n' "$output" | tail -1)"
  info "Everything else carries on without it. To retry:"
  info "    $PIP install ${specs[*]}"
  return 1
}

# --------------------------------------------------- 4. the whisper.cpp binary

BIN_DIR="${HOME:-$ROOT}/.local/bin"

whisper_already_installed() {
  local name found
  for name in "${WHISPER_ALIASES[@]}"; do
    found="$(command -v "$name" 2>/dev/null)"
    if [ -n "$found" ]; then
      printf '%s' "$found"
      return 0
    fi
  done
  return 1
}

# The URL of a prebuilt release for this architecture, or nothing — which also
# means "nothing, and the release list could not be read", because the answer is
# the same either way: build it. Upstream has published Windows builds only for
# years, so this normally finds nothing; it costs one request to find out, and
# the day a Linux ARM64 build appears this picks it up without a change here.
whisper_prebuilt_url() {
  local arch release
  arch="$(uname -m 2>/dev/null || true)"
  case "$arch" in
    aarch64 | arm64) arch='(aarch64|arm64)' ;;
    x86_64 | amd64) arch='(x86_64|amd64|x64)' ;;
    armv7l | armv6l) arch='(armv7|armhf|arm)' ;;
    *) return 1 ;;
  esac
  have curl || return 1
  release="$(curl --fail --location --silent --show-error --proto '=https' \
    --connect-timeout 15 --max-time 60 "$WHISPER_API" 2>/dev/null)" || return 1
  printf '%s\n' "$release" |
    grep -oE '"browser_download_url": *"[^"]+"' |
    sed -e 's/.*"browser_download_url": *"//' -e 's/"$//' |
    grep -iE 'linux' | grep -iE "$arch" | grep -iE '\.(tar\.gz|tgz|zip)$' |
    head -1
}

install_binary_at() {
  local built="$1"
  mkdir -p "$BIN_DIR" 2>/dev/null || return 1
  install -m 0755 "$built" "$BIN_DIR/whisper-cli" 2>/dev/null || return 1
  fix_ownership "$BIN_DIR/whisper-cli"
  return 0
}

whisper_from_prebuilt() {
  local url="$1" work archive found
  work="$(mktemp -d "$VAR_DIR/.whisper-release.XXXXXX" 2>/dev/null)" || return 1
  archive="$work/release"
  if ! curl --fail --location --silent --show-error --proto '=https' \
    --connect-timeout 20 --max-time 900 --output "$archive" "$url" 2>/dev/null; then
    rm -rf "$work"
    return 1
  fi
  case "$url" in
    *.zip) have unzip && unzip -q -o "$archive" -d "$work" 2>/dev/null ;;
    *) tar -xzf "$archive" -C "$work" 2>/dev/null ;;
  esac || { rm -rf "$work"; return 1; }
  found="$(find "$work" -type f -name 'whisper-cli' 2>/dev/null | head -1)"
  [ -n "$found" ] || found="$(find "$work" -type f -name 'main' 2>/dev/null | head -1)"
  if [ -z "$found" ]; then
    rm -rf "$work"
    return 1
  fi
  install_binary_at "$found" || { rm -rf "$work"; return 1; }
  rm -rf "$work"
  return 0
}

whisper_build_dependencies() {
  local missing=() tool
  for tool in git cmake make c++; do
    have "$tool" || missing+=("$tool")
  done
  [ "${#missing[@]}" -eq 0 ] && return 0
  # systemd/room-minutes-models.service runs this with nobody watching, so only
  # reach for sudo when it will not sit there waiting for a password.
  if have apt-get && have sudo; then
    if sudo -n true 2>/dev/null || [ -t 0 ]; then
      info "installing the build tools (${missing[*]}) — this needs sudo"
      sudo apt-get install -y -qq git cmake build-essential > /dev/null 2>&1
    else
      info "the build tools are missing and sudo would need a password that"
      info "there is nobody here to type"
    fi
  fi
  missing=()
  for tool in git cmake make c++; do
    have "$tool" || missing+=("$tool")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    warn "whisper.cpp cannot be built here: ${missing[*]} not found."
    info "Install them and run this again:"
    info "    sudo apt install git cmake build-essential"
    return 1
  fi
  return 0
}

whisper_from_source() {
  # $VAR_DIR, not $ROOT/var: the writable tree can be moved with
  # ROOM_APPLIANCE_VAR, and the reason a room moves it is usually to keep
  # hundreds of megabytes of churn off the SD card. Building here would put it
  # straight back.
  local source_dir="$VAR_DIR/build/whisper.cpp" built
  whisper_build_dependencies || return 1
  require_space "$VAR_DIR" 900 "building whisper.cpp" || return 1

  notice "Building whisper.cpp from source — 5–10 minutes on a Pi 5."
  info "The build log is $BUILD_LOG"
  mkdir -p "$(dirname "$BUILD_LOG")" "$(dirname "$source_dir")" 2>/dev/null || true
  rm -rf "$source_dir"

  if ! {
    git clone --depth 1 "$WHISPER_REPO" "$source_dir" &&
      cmake -S "$source_dir" -B "$source_dir/build" \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        -DWHISPER_BUILD_TESTS=OFF \
        -DWHISPER_BUILD_SERVER=OFF \
        -DWHISPER_BUILD_EXAMPLES=ON &&
      cmake --build "$source_dir/build" --config Release \
        -j "$(nproc 2>/dev/null || echo 2)" --target whisper-cli
  } > "$BUILD_LOG" 2>&1; then
    warn "whisper.cpp did not build."
    info "The last few lines of $BUILD_LOG:"
    tail -5 "$BUILD_LOG" 2>/dev/null | while IFS= read -r line; do info "    $line"; done
    return 1
  fi

  # BUILD_SHARED_LIBS=OFF above is what makes this one file worth copying: with
  # the default shared build the binary needs libwhisper.so beside it, and the
  # copy on PATH would not start.
  built="$(find "$source_dir/build" -type f -name 'whisper-cli' 2>/dev/null | head -1)"
  if [ -z "$built" ]; then
    warn "whisper.cpp built, but no whisper-cli binary came out of it."
    info "Look in $source_dir/build for what it did produce."
    return 1
  fi
  install_binary_at "$built" || {
    warn "Could not copy whisper-cli into $BIN_DIR."
    info "Copy it by hand: cp $built $BIN_DIR/"
    return 1
  }
  rm -rf "$source_dir"
  return 0
}

# The binary is no use if the room's services cannot see it. systemd's user
# manager does not put ~/.local/bin on PATH on Debian, so say so — and offer
# the fix that needs no root, which is a drop-in for environment.d.
report_binary_visibility() {
  local unit_path="" drop_in="${HOME:-$ROOT}/.config/environment.d/10-room-appliance.conf"
  case ":${PATH}:" in
    *":$BIN_DIR:"*) ;;
    *)
      warn "$BIN_DIR is not on your PATH."
      info "Add it for your shell: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.profile"
      ;;
  esac
  have systemctl || return 0
  unit_path="$(systemctl --user show-environment 2>/dev/null | sed -n 's/^PATH=//p')"
  [ -n "$unit_path" ] || return 0
  case ":${unit_path}:" in
    *":$BIN_DIR:"*) return 0 ;;
  esac
  if [ -e "$drop_in" ]; then
    warn "The room's services do not have $BIN_DIR on PATH, and $drop_in already exists."
    info "Add this line to it, then reboot:"
    info "    PATH=\${HOME}/.local/bin:\${PATH}"
    return 0
  fi
  mkdir -p "$(dirname "$drop_in")" 2>/dev/null || return 0
  # ${HOME} and ${PATH} below are expanded by systemd when it reads the file,
  # not by this shell, so they are deliberately left alone here.
  # shellcheck disable=SC2016
  if printf '%s\n%s\n%s\n' \
    '# Installed by scripts/install-minutes.sh so the room services can find' \
    '# whisper-cli. systemd does not search ~/.local/bin on its own.' \
    'PATH=${HOME}/.local/bin:${PATH}' > "$drop_in" 2>/dev/null; then
    fix_ownership "$drop_in"
    good "told the room's services where to find it ($drop_in)"
    info "That takes effect at the next reboot."
  else
    warn "The room's services will not find whisper-cli: $BIN_DIR is not on their PATH."
    info "Either move it: sudo mv $BIN_DIR/whisper-cli /usr/local/bin/"
    info "or create $drop_in containing: PATH=\${HOME}/.local/bin:\${PATH}"
  fi
}

install_whisper_binary() {
  local existing url
  existing="$(whisper_already_installed)"
  if [ -n "$existing" ]; then
    good "whisper.cpp is already installed ($existing); left alone"
    return 0
  fi

  url="$(whisper_prebuilt_url)"
  if [ -n "$url" ]; then
    info "a prebuilt release for $(uname -m) is published; fetching it"
    if whisper_from_prebuilt "$url"; then
      good "installed the prebuilt whisper-cli into $BIN_DIR"
      report_binary_visibility
      return 0
    fi
    warn "The prebuilt release could not be used; building instead."
  else
    info "no prebuilt whisper.cpp for $(uname -m) Linux was found, so it is built here"
  fi

  if whisper_from_source; then
    good "built and installed whisper-cli into $BIN_DIR"
    if ! "$BIN_DIR/whisper-cli" --help > /dev/null 2>&1; then
      warn "whisper-cli was installed but would not run."
      info "Try it by hand: $BIN_DIR/whisper-cli --help"
    fi
    report_binary_visibility
    return 0
  fi

  warn "whisper.cpp is not installed, so there is no speech-to-text engine yet."
  info "Two ways out, neither of which needs this script:"
  info "  • install faster-whisper instead (slower, more accurate, already in"
  info "    requirements-minutes.txt — nothing else to do), or"
  info "  • build whisper.cpp on another machine and copy its whisper-cli to"
  info "    $BIN_DIR, then reboot."
  info "Recording, faces, voices and the Claude summary all work without it."
  return 1
}

# ==========================================================================
# The install
# ==========================================================================

if [ "$DO_PIP" -eq 0 ] && [ "$DO_MODELS" -eq 0 ]; then
  die "--models-only and --pip-only cannot both be used; that would do nothing."
fi

case "$WHISPER_SIZE" in
  '' | tiny.en | base.en | small.en) ;;
  *) die "--whisper must be tiny.en, base.en or small.en, not: $WHISPER_SIZE" ;;
esac

if [ "$QUIET" -eq 0 ]; then
  printf '\n\033[1m╭──────────────────────────────────────────────╮\033[0m\n'
  printf '\033[1m│  Meeting minutes: engines and models          │\033[0m\n'
  printf '\033[1m╰──────────────────────────────────────────────╯\033[0m\n'
fi

if [ "$(id -u)" -eq 0 ]; then
  warn "Running as root. The appliance runs as an ordinary user, and everything"
  info "under var/minutes is mode 0600 — so anything written here is chowned to"
  info "${ROOT_OWNER:-the owner of $ROOT} afterwards. Better to run it as that user."
fi

# --------------------------------------------------- 1. hardware

step "Working out what this machine is"

if detect_hardware; then
  good "$MACHINE"
  good "performance profile: $PROFILE"
else
  PROFILE="unknown"
  warn "Could not ask the appliance what this machine is."
  info "Its Python environment may not be built yet. Run scripts/install.sh"
  info "first if this is a new appliance; a speech model is picked cautiously"
  info "in the meantime."
fi

TOO_SLOW=0
if [ "$PROFILE" = "low" ]; then
  TOO_SLOW=1
fi

if [ "$TOO_SLOW" -eq 1 ]; then
  WHISPER_SIZE=""
  warn "No local speech-to-text engine will be installed on this machine."
  info "$MACHINE is on the low performance profile. An hour-long meeting"
  info "would take most of a working day to transcribe here, on the same slow"
  info "cores that are holding the video call together — and the appliance"
  info "refuses to run a local engine on this profile in any case, so the"
  info "model and the binary would sit there unused."
  info "Everything else is still installed: recording, face and voice"
  info "recognition, and the Claude summary, which does its writing remotely."
  info "To overrule this, set the performance profile in Settings to balanced"
  info "and run the script again."
elif [ -n "$WHISPER_SIZE" ]; then
  good "speech model: $WHISPER_SIZE (asked for with --whisper)"
else
  WHISPER_SIZE="$(whisper_for_hardware)"
  good "speech model: $WHISPER_SIZE (chosen for this hardware)"
fi

# --------------------------------------------------- 2. pip extras

if [ "$DO_PIP" -eq 1 ]; then
  step "Installing the Python extras"

  if [ ! -x "$PIP" ]; then
    die "There is no virtual environment at $VENV.
     Run ./scripts/install.sh first, or point this at one with --venv PATH."
  fi
  if [ ! -r "$REQUIREMENTS" ]; then
    die "$REQUIREMENTS is missing, so there are no versions to install."
  fi

  require_space "$VENV" 1200 "the Python extras" ||
    warn "Carrying on anyway — the smaller packages may still fit."

  GROUPS_OK=0
  install_group "the Claude summary writer" anthropic && GROUPS_OK=$((GROUPS_OK + 1))
  install_group "vector maths" numpy && GROUPS_OK=$((GROUPS_OK + 1))
  install_group "face recognition" opencv-python-headless && GROUPS_OK=$((GROUPS_OK + 1))
  install_group "voice recognition" sherpa-onnx webrtcvad-wheels && GROUPS_OK=$((GROUPS_OK + 1))
  if [ "$TOO_SLOW" -eq 1 ]; then
    info "skipped the Python speech engines (see above)"
  else
    install_group "Python speech engines" faster-whisper vosk &&
      GROUPS_OK=$((GROUPS_OK + 1))
  fi

  if [ "$GROUPS_OK" -eq 0 ]; then
    warn "None of the Python extras could be installed."
    info "Check the network and try one by hand: $PIP install anthropic"
    fail
  fi
else
  info "Python extras skipped (--models-only)."
fi

# --------------------------------------------------- 3. model files

if [ "$DO_MODELS" -eq 1 ]; then
  step "Downloading the model files"

  if ! ensure_models_dir; then
    die "Could not create $MODELS_DIR.
     Check that $VAR_DIR exists and is writable by $(id -un)."
  fi
  info "into $MODELS_DIR"

  # Which YuNet? Two are published: 2023mar has a fixed input shape and was
  # built for OpenCV 4, 2026may has a dynamic one and needs OpenCV 5. Either
  # loads under 5; only the older one is safe under 4. app/minutes/faces.py
  # picks between them by the OpenCV it finds, so install the one that matches
  # the OpenCV that is actually here.
  CV_MAJOR=""
  if [ -x "$VENV/bin/python3" ]; then
    CV_MAJOR="$("$VENV/bin/python3" -c 'import cv2; print(str(cv2.__version__).split(".")[0])' 2>/dev/null)"
  fi
  if [ -z "$CV_MAJOR" ]; then
    # Not installed (yet). Fall back to the version this repository pins.
    CV_MAJOR="$(requirement_for opencv-python-headless | sed -n 's/.*==\([0-9]*\).*/\1/p')"
  fi
  case "$CV_MAJOR" in '' | *[!0-9]*) CV_MAJOR=0 ;; esac

  if [ "$CV_MAJOR" -ge 5 ]; then
    YUNET="face_detection_yunet_2026may.onnx"
  else
    YUNET="face_detection_yunet_2023mar.onnx"
  fi

  # Everything to fetch, as NAME|URL|MIN|MAX|KIND|PURPOSE, so the disk can be
  # measured against the whole job before a single byte is written.
  WANTED=(
    "$YUNET|$ZOO_URL/face_detection_yunet/$YUNET|150000|500000|onnx|finding the faces in a frame (YuNet, MIT licence)"
    "face_recognition_sface_2021dec.onnx|$ZOO_URL/face_recognition_sface/face_recognition_sface_2021dec.onnx|30000000|50000000|onnx|turning a face into numbers (SFace, Apache-2.0 licence)"
    "nemo_en_titanet_small.onnx|$SHERPA_URL/nemo_en_titanet_small.onnx|30000000|55000000|onnx|putting a name to a voice (TitaNet-small)"
  )
  if [ -n "$WHISPER_SIZE" ]; then
    case "$WHISPER_SIZE" in
      tiny.en) GGML_MIN=50000000; GGML_MAX=110000000 ;;
      base.en) GGML_MIN=100000000; GGML_MAX=220000000 ;;
      *) GGML_MIN=350000000; GGML_MAX=650000000 ;;
    esac
    WANTED+=(
      "ggml-$WHISPER_SIZE.bin|$GGML_URL/ggml-$WHISPER_SIZE.bin|$GGML_MIN|$GGML_MAX|ggml|turning speech into text (whisper.cpp $WHISPER_SIZE)"
    )
  fi

  # Refusing halfway through a 400 MB download and leaving the room with two
  # of the four files it needs helps nobody, so the whole job is costed first.
  NEEDED_MB=0
  for WANT in "${WANTED[@]}"; do
    IFS='|' read -r M_NAME M_URL M_MIN M_MAX M_KIND M_PURPOSE <<< "$WANT"
    if [ -f "$MODELS_DIR/$M_NAME" ] && [ "$FORCE" -eq 0 ]; then
      continue
    fi
    NEEDED_MB=$((NEEDED_MB + M_MAX / 1048576))
  done

  ENOUGH_ROOM=1
  if [ "$NEEDED_MB" -gt 0 ]; then
    require_space "$MODELS_DIR" "$((NEEDED_MB + 64))" "the model files" || {
      ENOUGH_ROOM=0
      fail
    }
  fi

  if [ "$ENOUGH_ROOM" -eq 1 ]; then
    for WANT in "${WANTED[@]}"; do
      IFS='|' read -r M_NAME M_URL M_MIN M_MAX M_KIND M_PURPOSE <<< "$WANT"
      fetch_model "$M_NAME" "$M_URL" "$M_MIN" "$M_MAX" "$M_KIND" "$M_PURPOSE"
    done
  fi

  fix_ownership "$MODELS_DIR" "$MANIFEST"
  [ -f "$MANIFEST" ] && chmod "$FILE_MODE" "$MANIFEST" 2>/dev/null

  # --------------------------------------------------- 4. whisper.cpp

  step "Installing whisper.cpp"

  if [ "$TOO_SLOW" -eq 1 ]; then
    info "Skipped: this machine is too slow to transcribe locally (see above)."
  elif ! install_whisper_binary; then
    fail
  fi
else
  info "Model files skipped (--pip-only)."
fi

# --------------------------------------------------------------- next steps

if [ "$QUIET" -eq 0 ]; then
  printf '\n'
  if [ "$FAILURES" -eq 0 ]; then
    printf '\033[1m╭──────────────────────────────────────────────╮\033[0m\n'
    printf '\033[1m│  Meeting minutes is ready to switch on         │\033[0m\n'
    printf '\033[1m╰──────────────────────────────────────────────╯\033[0m\n\n'
  else
    printf '\033[1m╭──────────────────────────────────────────────╮\033[0m\n'
    printf '\033[1m│  Finished, with %d thing(s) to sort out        │\033[0m\n' "$FAILURES"
    printf '\033[1m╰──────────────────────────────────────────────╯\033[0m\n\n'
    printf '  Every problem above names the next thing to try. This script is\n'
    printf '  safe to run again: it will not re-download what is already here.\n\n'
  fi
  printf '  \033[1mStill to do:\033[0m\n'
  printf '   • Settings → Meeting minutes → “Record and write up meetings”\n'
  printf '   • For the summary, paste an API key from console.anthropic.com\n'
  printf '   • Enrol the people you want recognised (Settings → People)\n'
  printf '   • The /minutes page names anything still missing\n\n'
  printf '  \033[1mWhat is installed:\033[0m\n'
  printf '   %s\n' "$MODELS_DIR"
  if [ -f "$MANIFEST" ]; then
    printf '   checksums in %s\n' "$MANIFEST"
    printf '   verify them any time with: cd %s && sha256sum -c manifest.sha256\n' "$MODELS_DIR"
  fi
  printf '\n'
fi

[ "$FAILURES" -eq 0 ] || exit 1
exit 0
