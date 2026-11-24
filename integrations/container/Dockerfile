# Isolated runtime for AI-DLC agents. Build from the plugin repo root:
#   docker build -t aidlc-agent -f integrations/container/Dockerfile .
# Agent CLI versions are pinned below; also pin the base image digest in your org's build.
FROM python:3.12-slim

# Claude Code, Codex and Copilot CLI need Node 22+, newer than Debian ships.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Tested versions; bump deliberately (agent CLI changes can change guard/hook behaviour).
ARG CLAUDE_CODE_VERSION=2.1.330
ARG CODEX_VERSION=0.171.0
ARG COPILOT_VERSION=1.0.114

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates gh \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
      "@github/copilot@${COPILOT_VERSION}" \
 && npm cache clean --force

COPY . /opt/aidlc-plugin
# The dev extra brings pytest and ruff, the default Python test/lint gates. Extend this image with
# your project's own dependencies (FROM aidlc-agent ...) so its test suite can run.
RUN pip install --no-cache-dir "/opt/aidlc-plugin/orchestrator[dev]"

# git pushes to GitHub authenticate through gh with the forwarded GH_TOKEN.
# AIDLC_ISOLATED tells aidlc it may route write stages to agents without pre-tool hooks (Codex).
ENV AIDLC_ISOLATED=1 \
    AIDLC_PLUGIN_ROOT=/opt/aidlc-plugin \
    HOME=/home/agent \
    GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0=credential.https://github.com.helper \
    GIT_CONFIG_VALUE_0="!gh auth git-credential"

ENTRYPOINT ["aidlc"]
