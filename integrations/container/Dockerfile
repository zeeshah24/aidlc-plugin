# Isolated runtime for AI-DLC agents. Build from the plugin repo root:
#   docker build -t aidlc-agent -f integrations/container/Dockerfile .
# Pin the CLI versions (and ideally the base image digest) in your org's build.
FROM python:3.12-slim

# Claude Code and Codex need Node 22+, newer than Debian ships.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

ARG CLAUDE_CODE_VERSION=latest
ARG CODEX_VERSION=latest

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates gh \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force

COPY . /opt/aidlc-plugin
RUN pip install --no-cache-dir /opt/aidlc-plugin/orchestrator

# AIDLC_ISOLATED tells aidlc it may route write stages to agents without pre-tool hooks (Codex).
ENV AIDLC_ISOLATED=1 \
    AIDLC_PLUGIN_ROOT=/opt/aidlc-plugin \
    HOME=/home/agent

ENTRYPOINT ["aidlc"]
