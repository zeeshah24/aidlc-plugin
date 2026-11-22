"""Build minimal-sufficient-context prompts for stateless sub-agents."""

from __future__ import annotations

from aidlc.config import Config
from aidlc.resources import agent_prompt
from aidlc.util import head

AGENT_FOR_STAGE = {
    "analyze": "aidlc-analyst",
    "plan": "aidlc-planner",
    "test": "aidlc-test-writer",
    "implement": "aidlc-implementer",
    "verify": "aidlc-verifier",
    "review": "aidlc-reviewer",
    "pr": "aidlc-ci-triage",
}


def build(cfg: Config, stage: str, sections: list[tuple[str, str]], feedback: list[str] | None = None) -> str:
    max_chars = int(cfg.get("pipeline.max_context_chars", 60000))
    parts = [agent_prompt(AGENT_FOR_STAGE[stage])]
    rules = cfg.guardrails.get("prompt_rules", [])
    if rules:
        parts.append(
            "## Guardrails (enforced by the harness; violations are blocked)\n" + "\n".join(f"- {r}" for r in rules)
        )
    if cfg.conventions:
        conv = "\n\n".join(f"### {layer} conventions\n{text}" for layer, text in cfg.conventions)
        parts.append("## Conventions (later layers override earlier ones)\n" + conv)
    budget = max_chars
    for title, body in sections:
        if not body:
            continue
        body = head(body, max(budget, 2000))
        budget -= len(body)
        parts.append(f"## {title}\n\n{body}")
    if feedback:
        parts.append("## Feedback to address (from previous gates)\n" + "\n".join(f"- {f}" for f in feedback))
    return "\n\n".join(parts) + "\n"
