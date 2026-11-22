# AI-DLC (Codex CLI)

Copy this section into your repository's `AGENTS.md`.

This repository uses the AI-DLC pipeline. For any requirement, ticket or feature request:

- Run `aidlc run <JIRA-KEY | path/to/requirement.md> --headless` instead of implementing directly.
- Single stages: `aidlc run --stage <analyze|plan|test|implement|verify|review|scan|pr|report>`.
- Pending human gates: `aidlc approve <stage>` then `aidlc run --resume`.
- Pending guardrail approvals: `aidlc allow` lists them; the developer decides with `aidlc allow <id>` or `aidlc deny <id>`.
- Never edit locked tests listed in `.aidlc/runs/<run-id>/test-lock.json`, CI workflows, or `.aidlc/`.

To route stages to Codex, set the provider in `aidlc.yaml`:

```yaml
models:
  tiers:
    coding: {provider: codex, model: gpt-5-codex}
```

Codex CLI has no pre-tool hooks, so `aidlc` refuses to route the writing stages (`test`, `implement`) to Codex
unless agents run isolated: use `scripts/aidlc-container.sh`, or set `harness.isolated: true` on a runtime that
is already an isolated container or VM. Read-only stages such as `review` can use Codex anywhere.
