"""Execution harness: run paths, worktree isolation, scoped env, audit log."""

from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from aidlc import gitutil
from aidlc.config import Config
from aidlc.util import now_iso

ARTIFACTS = {
    "requirement": "requirement.md",
    "stack_profile": "stack-profile.json",
    "spec": "spec.md",
    "plan": "plan.md",
    "dependencies": "dependencies.json",
    "test_lock": "test-lock.json",
    "implementation": "implementation.json",
    "verification": "verification.md",
    "review": "review.md",
    "scan": "scan.json",
    "pr": "pr.json",
}


@dataclass
class RunPaths:
    root: Path
    run_dir: Path
    workdir: Path

    def artifact(self, key: str) -> Path:
        return self.run_dir / ARTIFACTS[key]

    @property
    def audit_file(self) -> Path:
        return self.run_dir / "audit.jsonl"

    @property
    def report_file(self) -> Path:
        return self.root / ".aidlc" / "reports" / f"{self.run_dir.name}.md"


class AuditLog:
    def __init__(self, path: Path, actor: str = "orchestrator"):
        self.path = path
        self.actor = actor

    def log(self, event: str, **data) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now_iso(), "actor": self.actor, "event": event, **data}) + "\n")


def create_worktree(root: Path, run_id: str, branch: str, base: str) -> Path:
    path = root / ".aidlc" / "worktrees" / run_id
    path.parent.mkdir(parents=True, exist_ok=True)
    gitutil.git(["worktree", "add", "-q", "-b", branch, str(path), base], root)
    return path


def _env_key(name: str) -> str:
    return name.upper() if os.name == "nt" else name  # Windows environment names ignore case


def scoped_env(cfg: Config, paths: RunPaths, stage: str) -> dict[str, str]:
    """Only pass through allowlisted environment variables (scoped tokens)."""
    env_rules = cfg.guardrails.get("env", {})
    allowed = {_env_key(k) for k in env_rules.get("passthrough", [])}
    env = {k: v for k, v in os.environ.items() if _env_key(k) in allowed}
    if stage == "tests":
        excluded = {_env_key(k) for k in env_rules.get("test_exclude", [])}
        env = {k: v for k, v in env.items() if _env_key(k) not in excluded}
    env.update(
        AIDLC_RUN_DIR=str(paths.run_dir),
        AIDLC_JAIL=str(paths.workdir),
        AIDLC_STAGE=stage,
        AIDLC_PROJECT_ROOT=str(paths.root),
        AIDLC_PLUGIN_ROOT=os.environ.get("AIDLC_PLUGIN_ROOT", ""),
    )
    if not env["AIDLC_PLUGIN_ROOT"]:
        del env["AIDLC_PLUGIN_ROOT"]
    return env


GUARDRAILS_SNAPSHOT = "guardrails.json"


def snapshot_guardrails(cfg: Config, paths: RunPaths) -> None:
    """Freeze the policy at run start so an agent editing project config mid-run cannot widen it."""
    (paths.run_dir / GUARDRAILS_SNAPSHOT).write_text(json.dumps(cfg.guardrails, indent=2), encoding="utf-8")


def run_guardrails(run_dir: Path, fallback: dict) -> dict:
    snap = run_dir / GUARDRAILS_SNAPSHOT
    return json.loads(snap.read_text(encoding="utf-8")) if snap.exists() else fallback


def hook_executable(executable: str = sys.executable, windows: bool = os.name == "nt") -> str:
    """This Python, quoted for the shell Claude Code runs hooks in: sh, or Git Bash/PowerShell on Windows."""
    if not windows:
        return shlex.quote(executable)
    exe = executable.replace("\\", "/")
    return f'"{exe}"' if " " in exe else exe


def hook_settings(paths: RunPaths, guardrails: dict | None = None) -> Path:
    """Claude Code settings: guard hooks plus native deny rules, which Claude also applies inside
    Grep/Glob searches the hook only sees as a directory."""
    from aidlc.guardrails import GUARDED_MATCHER

    cmd = f"{hook_executable()} -m aidlc guard"
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": GUARDED_MATCHER,
                    "hooks": [{"type": "command", "command": f"{cmd} pre-tool"}],
                }
            ],
            "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": f"{cmd} post-tool"}]}],
        }
    }
    rails = (guardrails or {}).get("paths", {})
    settings["permissions"] = {
        "deny": [f"Read({p})" for p in rails.get("no_read", [])] + [f"Edit({p})" for p in rails.get("protected", [])]
    }
    path = paths.run_dir / "agent-settings.json"
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return path
