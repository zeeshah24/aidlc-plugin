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
| `integrations/` | Codex CLI and Copilot instructions, headless GitHub Actions workflow, agent container |

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

## Permissions and isolation

Guardrail rules in `guardrails.yaml` have three tiers:

| Tier | Examples | Who can approve |
|---|---|---|
| **deny** | `sudo`, `rm -rf`, `git push`, network tools, writing `.github/workflows/`, reading `.env` | Nobody during a run; change the org policy |
| **ask** | Commands outside the allowlist (`commands.unlisted: ask`), `commands.ask`, writes to `paths.ask`, unapproved dependency installs | The developer, in an interactive run |
| **allow** | `commands.allow`, edits inside the worktree | Pre-approved |

In an interactive run, an ask-tier action pauses the stage and asks: allow once, allow for the run, or deny.
Without a terminal, the run stops with `awaiting_permission`; decide with `aidlc allow <id> [--for-run]` or
`aidlc deny <id>`, then `aidlc run --resume`. With `--headless`, ask-tier actions are denied and nothing is asked.
Requests and decisions go to the audit log and the run report. Project config can tighten the org tiers but
not widen them.

The guard sees what an agent asks for, not what a program does, so run agents isolated for team use:

```bash
scripts/aidlc-container.sh run requirements/x.md   # builds integrations/container/Dockerfile on first use
```

The container mounts only the repository, runs as your uid with no capabilities and a read-only root, and
gets only model keys, `GH_TOKEN` and Jira credentials. Set `AIDLC_NETWORK` to a Docker network whose egress
only reaches your model API, git host and registry; the default `bridge` network is not restricted.
Codex has no pre-tool hooks, so `aidlc` refuses to route `test` or `implement` to Codex outside the container
(or another runtime you mark with `harness.isolated: true`).

## Telemetry and outcomes

Runs are recorded locally. Export is off by default and goes only to your own OpenTelemetry collector or JSONL
file when `telemetry.enabled` is set; records hold counts, durations, costs, models and outcomes, never prompts,
code or command output. Commits and PRs carry an `AIDLC-Run: <run-id>` trailer, and `aidlc outcomes` joins
runs with merge, review, rework and revert data from GitHub. See [docs/TELEMETRY.md](docs/TELEMETRY.md).

## Development

```bash
pip install -e "orchestrator[dev]"
cd orchestrator && ruff check . && ruff format --check . && pytest -q
```
