# AI-DLC (GitHub Copilot)

Copy into `.github/copilot-instructions.md`.

This repository uses the AI-DLC pipeline (`aidlc` CLI). When asked to implement a requirement or ticket,
run `aidlc run <JIRA-KEY | requirement.md> --headless` in the terminal and report the outcome from
`aidlc status`. Do not modify tests locked by AI-DLC, CI workflows, or files under `.aidlc/`.
