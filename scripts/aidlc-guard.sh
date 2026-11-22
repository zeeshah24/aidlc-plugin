#!/usr/bin/env sh
# Claude Code hook shim: enforce guardrails.yaml through `aidlc guard`.
# Only acts while an AI-DLC run is active (AIDLC_RUN_DIR set by the orchestrator).
if command -v aidlc >/dev/null 2>&1; then
  exec aidlc guard "$1"
fi
if [ -n "$AIDLC_RUN_DIR" ]; then
  echo "AI-DLC guardrails unavailable: install the aidlc CLI (scripts/install.sh)" >&2
  exit 2
fi
exit 0
