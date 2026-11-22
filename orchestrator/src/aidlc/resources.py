"""Locate plugin resources (agents, default guardrails/models) and package templates."""

from __future__ import annotations

import os
from pathlib import Path

_PKG = Path(__file__).resolve().parent


def plugin_root() -> Path:
    env = os.environ.get("AIDLC_PLUGIN_ROOT")
    if env:
        return Path(env)
    bundled = _PKG / "_plugin"
    if (bundled / "models.yaml").exists():
        return bundled
    repo = _PKG.parents[2]
    if (repo / "models.yaml").exists():
        return repo
    raise FileNotFoundError("Cannot locate AI-DLC plugin resources; set AIDLC_PLUGIN_ROOT")


def template(name: str) -> str:
    return (_PKG / "templates" / name).read_text()


def agent_prompt(name: str) -> str:
    """Return the body of agents/<name>.md without its YAML frontmatter."""
    text = (plugin_root() / "agents" / f"{name}.md").read_text()
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]
    return text.strip() + "\n"
