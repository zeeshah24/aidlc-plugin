"""Tool-level guardrail enforcement.

Used in two ways:
* ``aidlc guard pre-tool`` / ``post-tool`` as Claude Code hooks (JSON on stdin, exit 2 = block);
* directly by the orchestrator for post-hoc checks on agents that do not support hooks.

Three tiers: deny (never overridable during a run), ask (a developer may approve it in an interactive
run; headless runs deny it), allow.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from aidlc import permissions
from aidlc.config import load_config
from aidlc.harness import AuditLog, run_guardrails

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
NETWORK_TOOLS = {"WebFetch", "WebSearch"}
GUARDED_MATCHER = "Bash|Write|Edit|MultiEdit|NotebookEdit|Read|Grep|Glob|WebFetch|WebSearch|mcp__.*"
READ_TOOLS = {"Read", "Grep", "Glob", "NotebookRead"}
_SEGMENT_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    ask: bool = False  # ask-tier: a developer may approve it
    subject: str = ""  # what an approval covers, e.g. "command:<cmd>" or "write:<path>"


def _match(value: str, patterns: list[str]) -> str | None:
    return next(
        (
            p
            for p in patterns
            if fnmatch.fnmatchcase(value, p) or (p.startswith("**/") and fnmatch.fnmatchcase(value, p[3:]))
        ),
        None,
    )


def approved_packages(run_dir: Path | None) -> dict[str, str | None]:
    if not run_dir:
        return {}
    path = run_dir / "dependencies.json"
    if not path.exists():
        return {}
    deps = json.loads(path.read_text()).get("dependencies", [])
    return {d["name"].lower(): d.get("version") for d in deps if d.get("status") == "approved"}


def locked_paths(run_dir: Path | None) -> set[str]:
    if not run_dir:
        return set()
    path = run_dir / "test-lock.json"
    return set(json.loads(path.read_text()).get("files", {})) if path.exists() else set()


def _parse_spec(word: str) -> tuple[str, str | None]:
    """`name==1.2`, `name@1.2`, `@scope/name@1.2` -> (name, exact version or None)."""
    if word.startswith("@"):
        name, _, version = word[1:].partition("@")
        return "@" + name.lower(), version.lstrip("=") or None
    m = re.match(r"^([^=<>~!@\[\s]+)(?:\[[^\]]*\])?(?:(==|@)([^,;\s]+))?", word)
    if not m:
        return word.lower(), None
    return m.group(1).lower(), m.group(3).lstrip("=") if m.group(2) else None


def _install_targets(segment: str) -> list[tuple[str, str | None]]:
    try:
        words = shlex.split(segment)
    except ValueError:
        return []
    idx = next((i for i, w in enumerate(words) if w in {"install", "add", "get", "i"}), None)
    if idx is None:
        return []
    return [_parse_spec(w) for w in words[idx + 1 :] if not w.startswith("-")]


def check_command(command: str, guardrails: dict, run_dir: Path | None = None) -> Decision:
    cmd_cfg = guardrails.get("commands", {})
    command = command.strip()
    deny_rules = cmd_cfg.get("deny", [])
    deny = _match(command, deny_rules)
    if deny:
        return Decision(False, f"command matches deny rule '{deny}'")
    segments = [s for s in _SEGMENT_SPLIT.split(command) if s]
    for segment in segments:
        if _match(segment, deny_rules):
            return Decision(False, f"'{segment}' matches a deny rule")
    approved = approved_packages(run_dir)
    installs = guardrails.get("dependencies", {}).get("install_commands", [])
    unlisted_ask = cmd_cfg.get("unlisted", "deny") == "ask"
    asks: list[str] = []
    for segment in segments:
        pkgs = _install_targets(segment) if _match(segment, installs) else []
        if pkgs:
            missing = [n for n, _ in pkgs if n not in approved]
            if missing:
                asks.append(f"dependency install not approved in the plan's dependency gate: {missing}")
                continue
            wrong = [f"{n} (approved {approved[n]})" for n, v in pkgs if approved[n] and v != approved[n]]
            if wrong:
                asks.append(f"install must pin the approved version: {', '.join(wrong)}")
            continue
        if _match(segment, cmd_cfg.get("allow", [])):
            continue
        if not (unlisted_ask or _match(segment, cmd_cfg.get("ask", []))):
            return Decision(False, f"'{segment}' is not in the command allowlist")
        asks.append(f"'{segment}' is not in the command allowlist")
    if asks:
        return Decision(False, "; ".join(asks), ask=True, subject=f"command:{command}")
    return Decision(True)


def check_write(file_path: str, guardrails: dict, jail: Path, run_dir: Path | None = None) -> Decision:
    target = Path(file_path)
    if not target.is_absolute():
        target = jail / target
    target = Path(os.path.realpath(target))
    jail = Path(os.path.realpath(jail))
    if target != jail and jail not in target.parents:
        return Decision(False, f"{file_path} is outside the workspace jail {jail}")
    rel = target.relative_to(jail).as_posix()
    rule = _match(rel, guardrails.get("paths", {}).get("protected", []))
    if rule:
        return Decision(False, f"{rel} is protected by '{rule}'")
    if rel in locked_paths(run_dir):
        return Decision(False, f"{rel} is a locked red test; make the implementation satisfy it instead")
    rule = _match(rel, guardrails.get("paths", {}).get("ask", []))
    if rule:
        return Decision(False, f"writing {rel} needs developer approval ('{rule}')", ask=True, subject=f"write:{rel}")
    return Decision(True)


def check_read(file_path: str, guardrails: dict, jail: Path) -> Decision:
    target = Path(file_path)
    if not target.is_absolute():
        target = jail / target
    target = Path(os.path.realpath(target))
    jail = Path(os.path.realpath(jail))
    if target != jail and jail not in target.parents:
        return Decision(False, f"{file_path} is outside the workspace jail {jail}")
    rel = target.relative_to(jail).as_posix()
    rule = _match(rel, guardrails.get("paths", {}).get("no_read", []))
    if rule:
        return Decision(False, f"{rel} may not be read ('{rule}')")
    return Decision(True)


def evaluate(tool_name: str, tool_input: dict, guardrails: dict, jail: Path, run_dir: Path | None) -> Decision:
    if tool_name in NETWORK_TOOLS or tool_name.startswith("mcp__"):
        if _match(tool_name, guardrails.get("tools", {}).get("allow", [])):
            return Decision(True)
        return Decision(False, f"{tool_name} is not in tools.allow (network and MCP tools are denied by default)")
    if tool_name == "Bash":
        return check_command(tool_input.get("command", ""), guardrails, run_dir)
    if tool_name in WRITE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return check_write(path, guardrails, jail, run_dir)
    if tool_name in READ_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path") or ""
        return check_read(path, guardrails, jail) if path else Decision(True)
    return Decision(True)


def _active_context() -> tuple[Path, Path | None] | None:
    run_dir = os.environ.get("AIDLC_RUN_DIR")
    jail = os.environ.get("AIDLC_JAIL")
    if run_dir and jail:
        return Path(jail), Path(run_dir)
    return None


def hook_main(event: str) -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    ctx = _active_context()
    if ctx is None:
        return 0
    jail, run_dir = ctx
    tool, tool_input = payload.get("tool_name", ""), payload.get("tool_input", {})
    audit = AuditLog(run_dir / "audit.jsonl", actor=f"agent:{os.environ.get('AIDLC_STAGE', 'interactive')}")
    if event == "post-tool":
        audit.log("tool.used", tool=tool, input=_summarize(tool_input))
        return 0
    cfg = load_config(Path(os.environ.get("AIDLC_PROJECT_ROOT") or jail))
    rails = run_guardrails(run_dir, cfg.guardrails)
    decision = evaluate(tool, tool_input, rails, jail, run_dir)
    if decision.ask:
        ok, message = permissions.check(
            run_dir,
            decision.subject,
            os.environ.get("AIDLC_STAGE", ""),
            tool,
            decision.reason,
            os.environ.get("AIDLC_INTERACTIVE") == "1",
            audit,
        )
        decision = Decision(ok, message or decision.reason)
    audit.log(
        "tool.allowed" if decision.allowed else "tool.blocked",
        tool=tool,
        input=_summarize(tool_input),
        reason=decision.reason,
    )
    if not decision.allowed:
        print(f"AI-DLC guardrail blocked {tool}: {decision.reason}", file=sys.stderr)
        return 2
    return 0


def _summarize(tool_input: dict) -> dict:
    return {
        k: (v[:300] if isinstance(v, str) else v)
        for k, v in tool_input.items()
        if k in ("command", "file_path", "notebook_path", "pattern", "path")
    }
