#!/usr/bin/env sh
# Run aidlc with its agents inside a container that sees only the current repository.
# Usage: scripts/aidlc-container.sh run requirements/DEMO-1.md   (same as: aidlc container run ...)
# See `aidlc container --help` for AIDLC_IMAGE and AIDLC_NETWORK.
set -eu
AIDLC_PLUGIN_ROOT="${AIDLC_PLUGIN_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
export AIDLC_PLUGIN_ROOT
exec aidlc container "$@"
