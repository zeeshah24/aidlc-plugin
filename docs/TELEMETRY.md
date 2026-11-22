# Telemetry and outcome measurement

AI-DLC records every run locally (`.aidlc/reports/<run-id>.md`, `report.json`, `audit.jsonl`). Sending
anything elsewhere is **off by default** and controlled entirely by the adopting organisation.

## Rules

- **No phone-home.** The plugin has no endpoint of its own. With `telemetry.enabled: false` (the default)
  nothing is sent, even if `OTEL_EXPORTER_OTLP_ENDPOINT` is set in the environment.
- **Adopter-owned sinks only.** Records go to the OTLP/HTTP collector in `telemetry.endpoint` (or
  `OTEL_EXPORTER_OTLP_ENDPOINT`) and/or a JSONL file in `telemetry.file`, for example for Fluent Bit or Vector.
  Retention, residency and access are the adopter's.
- **Only AI-DLC's own activity.** No host, application or other process logs or traces are read or forwarded.
- **No content.** No prompts, model output, code, diffs, file paths, file contents, command text or output,
  environment variables or requirement text.
- **Pseudonymous identifiers.** The repository is a salted SHA-256 prefix (`h:…`, salt from
  `AIDLC_TELEMETRY_SALT`) unless `raw_identifiers: true`. Developer identity is not exported unless
  `include_developer: true`, and is hashed the same way.
- **Org policy wins.** If `.aidlc/org/aidlc.yaml` defines `telemetry`, a project can only switch it off.
- **Never fails a run.** Delivery errors are written to the audit log as `telemetry.failed`.

`aidlc telemetry show [--run ID]` prints the exact record that would be exported, whether or not export is on.

## Configuration

```yaml
telemetry:
  enabled: false
  endpoint: ""            # e.g. https://otel-collector.internal:4318 (POSTs to /v1/traces)
  headers: {}             # extra OTLP headers; OTEL_EXPORTER_OTLP_HEADERS is also honoured
  file: ""                # JSONL sink, relative to the project root or absolute
  team: ""
  raw_identifiers: false
  include_developer: false
  ask_rating: true
```

## Run record (`aidlc.run/1`)

| Field | Meaning |
|---|---|
| `run_id`, `aidlc_version`, `created`, `status`, `loops` | Run identity and result (`run_id` hashed by default; it embeds the requirement id) |
| `team`, `repo` | Reporting dimensions (`repo` hashed by default) |
| `developer` | Only with `include_developer: true`; hashed by default |
| `rating` | Optional 1-5 developer rating, or `null` |
| `totals.duration_s`, `tokens_in`, `tokens_out`, `cost_usd` | Sums over stages |
| `stages.<name>.status`, `attempts`, `duration_s`, `tokens_in`, `tokens_out`, `cost_usd`, `models`, `gate`, `started`, `ended` | Per stage |
| `friction.human_gates_approved`, `permission_requests`, `permissions_granted`, `permissions_denied`, `permission_wait_s`, `guardrail_blocks` | Developer friction |

Over OTLP a run is one trace: an `aidlc.run` root span and one `aidlc.stage.<name>` span per stage, with
`gen_ai.request.model`, `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens` attributes. The trace ID
is derived from the run ID, so later outcome spans join the same trace.

## Joining run data with outcomes

Each run stamps `AIDLC-Run: <run-id>` as a trailer on its commits and as the last line of the PR body, and
labels the PR `aidlc` (`pr.label`). `aidlc outcomes` lists that repository's AI-DLC PRs with the adopter's own
`gh` credentials and prints one `aidlc.outcome/1` JSON line each (run ids hashed like run records, so they still join); `--export` also sends them to the configured
sinks as `aidlc.outcome` spans.

| Field | Meaning |
|---|---|
| `run_id`, `pr`, `state`, `merged`, `hours_to_merge` | PR result |
| `reviews`, `changes_requested` | Review rounds |
| `agent_commits`, `human_commits` | Commits with and without the run trailer (human rework before merge) |
| `reverted` | A later PR body says `Reverts owner/repo#<pr>` |

`integrations/ci/aidlc-outcomes.yml` runs the collector daily in GitHub Actions.

## Developer rating

When telemetry is enabled, an interactive run ends with an optional "How useful was this run? 1-5" prompt
(Enter skips; `ask_rating: false` turns it off). Headless runs never ask. `aidlc rate <1-5> [--run ID]` records
or changes a rating later. The rating appears in the run report and in the run record.

## Reporting guidance

Report by team, repository, stage and model: adoption, completion and failing gates, cost per merged PR,
friction, merge rate, rework and reverts. Avoid per-developer rankings; they distort behaviour and may need
employee notice or works-council approval. Whether to collect developer identity at all is the adopter's decision.
