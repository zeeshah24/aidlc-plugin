---
description: Initialize AI-DLC in this repository (.aidlc/ and aidlc.yaml)
argument-hint: "[--org <path-or-git-url>]"
allowed-tools: Bash(aidlc:*), Read
---
Run `aidlc init` with the arguments, if any, then `aidlc doctor`. Show the detected stack and model routing, and point
the user at `aidlc.yaml` (human gates, test command, Jira) and `.aidlc/conventions.md`.
