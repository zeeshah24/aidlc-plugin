#!/usr/bin/env bash
# Run aidlc with its agents inside a container that sees only the current repository.
# Usage: scripts/aidlc-container.sh run --file requirements/DEMO-1.md
#   AIDLC_IMAGE    image tag (default aidlc-agent:<content hash>; built on first use)
#   AIDLC_NETWORK  docker network (default bridge); point it at a network whose egress
#                  only reaches your model API, git host and package registry.
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Tag the default image by the content it is built from, so upgrading the plugin rebuilds it.
# Hash every file the build context sends (paths and contents); keep the prune list in step with
# integrations/container/Dockerfile.dockerignore.
BUILD_HASH="$(cd "$PLUGIN_ROOT" && find . \( -name .git -o -name .aidlc -o -name __pycache__ -o -name .venv -o -name .pytest_cache \
  -o -name .ruff_cache \) -prune -o -type f -print \
  | LC_ALL=C sort | while IFS= read -r f; do printf '%s\n' "$f"; cat "$f"; done \
  | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-12)"
IMAGE="${AIDLC_IMAGE:-aidlc-agent:$BUILD_HASH}"
ROOT="$(git rev-parse --show-toplevel)"
# A linked worktree keeps its git metadata in the main repository; mount that too.
COMMON="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)"
mounts=(-v "$ROOT:$ROOT")
case "$COMMON/" in "$ROOT"/*) ;; *) mounts+=(-v "$COMMON:$COMMON") ;; esac

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker build -t "$IMAGE" -f "$PLUGIN_ROOT/integrations/container/Dockerfile" "$PLUGIN_ROOT"
fi

# Only model keys, a scoped git token, Jira credentials and the adopter's own telemetry collector settings are
# forwarded; never cloud/deploy credentials.
args=()
for var in ANTHROPIC_API_KEY OPENAI_API_KEY COPILOT_GITHUB_TOKEN GH_TOKEN JIRA_BASE_URL JIRA_EMAIL JIRA_API_TOKEN JIRA_PAT \
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
  "${mounts[@]}" \
  -w "$ROOT" \
  "$IMAGE" "$@"
