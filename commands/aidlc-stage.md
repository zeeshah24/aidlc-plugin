---
description: Run one or more AI-DLC stages standalone (e.g. /aidlc-stage review)
argument-hint: "<stage> | --from STAGE --to STAGE | --only a,b"
allowed-tools: Bash(aidlc:*), Read
---
Stages: intake, analyze, plan, test, implement, verify, review, scan, pr, report.

If the arguments are a single stage name run `aidlc run --stage <stage>`, otherwise
`aidlc run` followed by the arguments. Stages run against the current run when one exists, or the current
checkout (`--adhoc`) for stages that need no earlier artifacts (review, scan, report).
If a stage reports missing prerequisites, tell the user which earlier stage to run.
Summarize the stage outcome and link the artifact it produced.
