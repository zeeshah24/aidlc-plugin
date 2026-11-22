#!/usr/bin/env sh
# Install the aidlc CLI from this plugin checkout. Usage: scripts/install.sh [--dev]
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if command -v pipx >/dev/null 2>&1 && [ "${1:-}" != "--dev" ]; then
  pipx install --force "$ROOT/orchestrator"
elif [ "${1:-}" = "--dev" ]; then
  python3 -m pip install -e "$ROOT/orchestrator[dev]"
else
  python3 -m pip install --user "$ROOT/orchestrator"
fi
aidlc --version
