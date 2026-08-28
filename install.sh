#!/usr/bin/env bash
# Convenience wrapper so the documented `./install.sh` works from the repository
# root. The real installer lives in scripts/install.sh.
set -uo pipefail
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/install.sh" "$@"
