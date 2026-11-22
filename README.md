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
scripts/install.sh                 # installs the aidlc CLI (pipx, or pip --user); Windows: scripts\install.ps1
cd /path/to/your/repo
aidlc init                         # .aidlc/ + aidlc.yaml (--org <path|git-url> pulls enterprise standards)
aidlc doctor                       # check tools and model routing
aidlc run requirements/x.md        # or: aidlc run PROJ-123 (Jira)
aidlc approve plan && aidlc run --resume   # continue after a human gate
```

Standalone stages: `aidlc run --stage review`, `aidlc run --from verify --to scan`, `aidlc run --only test,implement`.
CI: `aidlc run PROJ-123 --headless --approve analyze,plan` (see `integrations/ci/github-actions.yml`).

## Install the plugin

This repository is a plugin marketplace (`.claude-plugin/marketplace.json`) that Claude Code, GitHub Copilot CLI
and Codex CLI all read. The plugin adds the `aidlc-*` commands, agents and guardrail hooks; it calls the `aidlc`
CLI, so install that first (`scripts/install.sh`, or `scripts\install.ps1` on Windows).

| Assistant | Install | Use |
|---|---|---|
| Claude Code | `/plugin marketplace add zeeshah24/aidlc-plugin` then `/plugin install aidlc@aidlc-plugin` | `/aidlc-run PROJ-123` |
| GitHub Copilot CLI | `copilot plugin marketplace add zeeshah24/aidlc-plugin` then `copilot plugin install aidlc@aidlc-plugin` | `/aidlc-run PROJ-123` |
| Codex CLI | `codex plugin marketplace add zeeshah24/aidlc-plugin` then `codex plugin add aidlc@aidlc-plugin` | `$source-command-aidlc-run PROJ-123` (Codex turns plugin commands into skills) |
| Copilot in VS Code | Command Palette: **Chat: Install Plugin from Source**, `https://github.com/zeeshah24/aidlc-plugin` | ask Copilot Chat (Agent mode) to run AI-DLC for a ticket |

Add `--ref <tag>` (Codex) or `owner/repo#<tag>` (Copilot) to pin a release. For local development, Claude Code
and Copilot CLI accept `--plugin-dir /path/to/aidlc-plugin`. Assistants without plugin support can use the
instruction files in `integrations/` (`copilot/copilot-instructions.md`, `codex/AGENTS.md`).

Whichever assistant starts the run, `aidlc` routes stage work to the providers in `models.yaml` (Claude Code or
Codex CLI), so their CLIs and API keys are still required.

### macOS and Windows

The plugin, the `aidlc` CLI and the container work the same on macOS, Linux and Windows (CI runs on all three).

| | macOS | Windows (PowerShell) |
|---|---|---|
| Prerequisites | `brew install python@3.12 pipx node git gh` | `winget install Python.Python.3.12 Git.Git OpenJS.NodeJS.LTS GitHub.cli` then `py -m pip install --user pipx` and `py -m pipx ensurepath` |
| `aidlc` CLI | `pipx install "git+https://github.com/zeeshah24/aidlc-plugin#subdirectory=orchestrator"` | same command |
| Agent CLIs | `npm install -g @anthropic-ai/claude-code` (and/or `@openai/codex`) | same command |
| Container (`test`, `implement`) | Docker Desktop, then `aidlc container run requirements/x.md` | Docker Desktop (WSL 2 backend), then `aidlc container run requirements/x.md` |

Notes for Windows:
- Claude Code runs its Bash tool and hooks in Git Bash; the guardrail hook is the plain command `aidlc guard`, so it
  also works when Claude falls back to PowerShell, and the guard checks the `PowerShell` tool like `Bash`.
- `stack.test_command` and the other project commands run in `cmd.exe` when `aidlc` runs natively, so keep them
  portable (`python -m pytest`, `npm test`); inside `aidlc container` they run in Linux.
- WSL 2 also works: install everything inside the Linux distribution and follow the macOS/Linux column.

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

The guard is defence in depth, not the security boundary: it sees what an agent asks for, not what a program
does, and allowed runners (`pytest`, `npm run`, `make test`) execute code the agent wrote. So `test` and
`implement` require an isolated runtime by default (`harness.require_isolation`; an org can make it mandatory):

```bash
aidlc container run requirements/x.md   # builds integrations/container/Dockerfile on first use
```

The container mounts only the repository, runs as your uid with no capabilities and a read-only root, and
gets only model keys, `GH_TOKEN` and Jira credentials. Set `AIDLC_NETWORK` to a Docker network whose egress
only reaches your model API, git host and registry; the default `bridge` network is not restricted.
Codex has no pre-tool hooks, so `aidlc` refuses to route `test` or `implement` to Codex outside the container
(or another runtime you mark with `harness.isolated: true`) even when isolation is otherwise relaxed.

Other controls:
- Agents get no MCP servers from the developer's setup, and `WebFetch`, `WebSearch` and MCP tools are denied
  unless listed under `tools.allow`.
- `aidlc.yaml` and `CONVENTIONS.md` are protected, and the hook uses the guardrails frozen at run start.
- Requirement text, artifacts and diffs reach agents fenced as data; `VERDICT:` lines inside them are removed.
- `pipeline.max_cost_usd` / `max_tokens` stop a run that overspends; `verify` fails while an `AC-` ID has no test.
- `aidlc doctor` checks config keys, plugin/CLI versions, isolation and each provider's capabilities.

## Telemetry and outcomes

Runs are recorded locally. Export is off by default and goes only to your own OpenTelemetry collector or JSONL
file when `telemetry.enabled` is set; records hold counts, durations, costs, models and outcomes, never prompts,
code or command output. Commits and PRs carry an `AIDLC-Run: <run-id>` trailer, and `aidlc outcomes` joins
runs with merge, review, rework and revert data from GitHub. See [docs/TELEMETRY.md](docs/TELEMETRY.md).

## Development

```bash
pip install -e "orchestrator[dev]"   # releases: tag vX.Y.Z (must match plugin.json) -> PyPI with provenance
cd orchestrator && ruff check . && ruff format --check . && pytest -q
```
