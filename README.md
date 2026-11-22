# aidlc-plugin

An AI-DLC plugin for coding assistants (Claude Code, Codex CLI, GitHub Copilot). It takes requirements — markdown files or Jira stories — and drives them through an agent pipeline (analysis, planning, red tests, implementation, verification, review, static analysis) to a reviewed pull request, with enterprise guardrails, traceability, and cost reporting.

See [docs/DESIGN.md](docs/DESIGN.md) for the full design.

## Layout

| Path | What it is |
|---|---|
| `orchestrator/` | `aidlc` CLI (Python): deterministic state machine, guardrail hooks, scanners, reports |
| `agents/` | Stateless phase prompts (analyst, planner, test-writer, implementer, verifier, reviewer, CI triage) |
| `commands/`, `hooks/`, `.claude-plugin/` | Claude Code plugin: slash commands and `PreToolUse`/`PostToolUse` guardrail hooks |
| `guardrails.yaml`, `models.yaml`, `mcp.json` | Default guardrails, model-tier routing, MCP servers (Jira, GitHub) |
| `integrations/` | Codex CLI and Copilot instructions, headless GitHub Actions workflow |

## Quick start

```bash
scripts/install.sh                 # installs the aidlc CLI (pipx, or pip --user)
cd /path/to/your/repo
aidlc init                         # .aidlc/ + aidlc.yaml (--org <path|git-url> pulls enterprise standards)
aidlc doctor                       # check tools and model routing
aidlc run requirements/x.md        # or: aidlc run PROJ-123 (Jira)
aidlc approve plan && aidlc run --resume   # continue after a human gate
```

Standalone stages: `aidlc run --stage review`, `aidlc run --from verify --to scan`, `aidlc run --only test,implement`.
CI: `aidlc run PROJ-123 --headless --approve analyze,plan` (see `integrations/ci/github-actions.yml`).
In Claude Code, install this repo as a plugin and use `/aidlc-run PROJ-123`.

Each run works on its own branch in `.aidlc/worktrees/<run-id>/`. It writes artifacts to `.aidlc/runs/<run-id>/`
and a cost/time report to `.aidlc/reports/<run-id>.md`.

## Development

```bash
pip install -e "orchestrator[dev]"
cd orchestrator && ruff check . && ruff format --check . && pytest -q
```
