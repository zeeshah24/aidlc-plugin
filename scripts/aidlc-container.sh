#!/usr/bin/env bash
# Run aidlc with its agents inside a container that sees only the current repository.
# Usage: scripts/aidlc-container.sh run --file requirements/DEMO-1.md
#   AIDLC_IMAGE    image tag (default aidlc-agent:local; built on first use)
#   AIDLC_NETWORK  docker network (default bridge); point it at a network whose egress
#                  only reaches your model API, git host and package registry.
set -euo pipefail

IMAGE="${AIDLC_IMAGE:-aidlc-agent:local}"
PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(git rev-parse --show-toplevel)"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker build -t "$IMAGE" -f "$PLUGIN_ROOT/integrations/container/Dockerfile" "$PLUGIN_ROOT"
fi

# Only model keys, a scoped git token, Jira credentials and the adopter's own telemetry collector settings are
# forwarded; never cloud/deploy credentials.
args=()
for var in ANTHROPIC_API_KEY OPENAI_API_KEY GH_TOKEN JIRA_BASE_URL JIRA_EMAIL JIRA_API_TOKEN JIRA_PAT \
           HTTPS_PROXY HTTP_PROXY NO_PROXY \
           OTEL_EXPORTER_OTLP_ENDPOINT OTEL_EXPORTER_OTLP_HEADERS AIDLC_TELEMETRY_SALT; do
  if [[ -n "${!var:-}" ]]; then args+=(-e "$var"); fi
done
if [[ -t 0 && -t 1 ]]; then args+=(-it); fi

# The repo is mounted at the same path so git worktree paths stay valid.
exec docker run --rm "${args[@]}" \
  --user "$(id -u):$(id -g)" \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --read-only \
  --tmpfs /tmp:exec \
  --tmpfs /home/agent:exec \
  --network "${AIDLC_NETWORK:-bridge}" \
  -v "$ROOT:$ROOT" \
  -w "$ROOT" \
  "$IMAGE" "$@"
