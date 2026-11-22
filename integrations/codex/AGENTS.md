# AI-DLC (Codex CLI)

Copy this section into your repository's `AGENTS.md`.

This repository uses the AI-DLC pipeline. For any requirement, ticket or feature request:

- Run `aidlc run <JIRA-KEY | path/to/requirement.md> --headless` instead of implementing directly.
- Single stages: `aidlc run --stage <analyze|plan|test|implement|verify|review|scan|pr|report>`.
- Pending human gates: `aidlc approve <stage>` then `aidlc run --resume`.
- Never edit locked tests listed in `.aidlc/runs/<run-id>/test-lock.json`, CI workflows, or `.aidlc/`.

To route stages to Codex, set the provider in `aidlc.yaml`:

```yaml
models:
  tiers:
    coding: {provider: codex, model: gpt-5-codex}
```
