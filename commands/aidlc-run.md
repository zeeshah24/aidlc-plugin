---
description: Run the AI-DLC pipeline for a Jira key or requirement file (e.g. /aidlc-run PROJ-123)
argument-hint: "<JIRA-KEY | path/to/requirement.md> [--from STAGE] [--to STAGE] [--yes]"
allowed-tools: Bash(aidlc:*), Read
---
Run the deterministic AI-DLC orchestrator; do not perform the stages yourself.

1. If `aidlc` is not on PATH, tell the user to run `${CLAUDE_PLUGIN_ROOT}/scripts/install.sh` and stop.
2. If there is no `aidlc.yaml` in the repository root, run `aidlc init` first.
3. Run `aidlc run` followed by the arguments (not `--headless`: in a Claude Code session the developer can approve).
4. If the run stops at `awaiting_approval`, show the user the artifact it names (spec.md or plan.md),
   summarize it, and ask whether to approve. On approval run `aidlc approve <stage>` and then
   `aidlc run --resume`.
5. If it stops at `awaiting_permission`, list the pending requests with `aidlc allow` (no ids) and ask the user
   to allow once, allow for the run, or deny. Run `aidlc allow <id>` (add `--for-run`) or
   `aidlc deny <id>`, then `aidlc run --resume`. Never approve a request yourself.
6. When finished, show the run report path and the stage summary table from `aidlc status`.
