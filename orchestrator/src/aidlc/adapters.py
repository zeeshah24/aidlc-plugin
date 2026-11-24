"""Agent adapters: run one stateless sub-agent call on a given assistant CLI."""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from aidlc.util import run_killable

READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]
WRITE_TOOLS = ["Read", "Grep", "Glob", "Write", "Edit", "MultiEdit", "Bash"]


@dataclass
class AgentRequest:
    stage: str
    prompt: str
    model: str
    workdir: Path
    writable: bool
    env: dict[str, str] = field(default_factory=dict)
    settings_file: Path | None = None
    timeout_s: int = 1800


@dataclass
class AgentResult:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    duration_s: float = 0.0
    ok: bool = True
    error: str = ""


class AgentAdapter(Protocol):
    name: str

    def available(self) -> bool: ...

    def run(self, req: AgentRequest) -> AgentResult: ...


class ClaudeCodeAdapter:
    """Claude Code headless mode (`claude -p`). Guardrail hooks are wired through --settings."""

    name = "claude"

    def __init__(self, binary: str = "claude"):
        self.binary = binary

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def command(self, req: AgentRequest) -> list[str]:
        tools = WRITE_TOOLS if req.writable else READ_ONLY_TOOLS
        cmd = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--model",
            req.model,
            "--allowedTools",
            ",".join(tools),
        ]
        if not req.writable:
            cmd += ["--disallowedTools", "Write,Edit,MultiEdit,Bash,NotebookEdit"]
        else:
            cmd += ["--permission-mode", "acceptEdits"]
        if req.settings_file:
            # only the run's MCP config (none by default), not the developer's servers
            mcp = req.settings_file.with_name("agent-mcp.json")
            mcp.write_text('{"mcpServers": {}}\n')
            cmd += ["--settings", str(req.settings_file), "--strict-mcp-config", "--mcp-config", str(mcp)]
        return cmd

    def run(self, req: AgentRequest) -> AgentResult:
        start = time.monotonic()
        proc = run_killable(self.command(req), input=req.prompt, cwd=req.workdir, env=req.env, timeout=req.timeout_s)
        elapsed = time.monotonic() - start
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return AgentResult("", duration_s=elapsed, ok=False, error=(proc.stderr or proc.stdout)[-2000:])
        usage = data.get("usage", {})
        tokens_in = sum(
            usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        return AgentResult(
            text=data.get("result", ""),
            tokens_in=tokens_in,
            tokens_out=usage.get("output_tokens", 0),
            cost_usd=data.get("total_cost_usd"),
            duration_s=elapsed,
            ok=proc.returncode == 0 and not data.get("is_error", False),
            error="" if proc.returncode == 0 else proc.stderr[-2000:],
        )


class CodexAdapter:
    """OpenAI Codex CLI (`codex exec --json`). Enforcement relies on Codex's sandbox plus
    the orchestrator's post-hoc checks (locked-test hashes, protected paths)."""

    name = "codex"

    def __init__(self, binary: str = "codex"):
        self.binary = binary

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def command(self, req: AgentRequest) -> list[str]:
        sandbox = "workspace-write" if req.writable else "read-only"
        return [
            self.binary,
            "exec",
            "--json",
            "--model",
            req.model,
            "--sandbox",
            sandbox,
            "--skip-git-repo-check",
            "-",  # read the prompt from stdin: keeps it out of argv/ps and avoids argument-length limits
        ]

    def run(self, req: AgentRequest) -> AgentResult:
        start = time.monotonic()
        proc = run_killable(self.command(req), input=req.prompt, cwd=req.workdir, env=req.env, timeout=req.timeout_s)
        text, tin, tout = "", 0, 0
        for line in proc.stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = ev.get("item", {})
            if ev.get("type") == "item.completed" and item.get("type") in (
                "agent_message",
                "assistant_message",
            ):
                text = item.get("text", text)
            if ev.get("type") == "turn.completed":
                usage = ev.get("usage", {})
                tin += usage.get("input_tokens", 0)
                tout += usage.get("output_tokens", 0)
        return AgentResult(
            text,
            tin,
            tout,
            None,
            time.monotonic() - start,
            proc.returncode == 0,
            proc.stderr[-2000:] if proc.returncode else "",
        )


class CopilotAdapter:
    """GitHub Copilot CLI (`copilot --output-format json`, prompt on stdin). Copilot runs Claude-format
    PreToolUse hooks from a plugin directory (fail-closed on exit 2), so the guard is wired through a
    generated plugin; token usage is read from the session's events.jsonl."""

    name = "copilot"
    # apply_patch edits carry no file path the guard can check; MCP servers are not inherited.
    EXCLUDED_TOOLS = ["apply_patch", "web_fetch", "web_search"]

    def __init__(self, binary: str = "copilot"):
        self.binary = binary

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def command(self, req: AgentRequest, session_id: str) -> list[str]:
        cmd = [
            self.binary,
            "--output-format=json",
            f"--model={req.model}",
            f"--session-id={session_id}",
            "--no-ask-user",
            "--no-auto-update",
            "--disable-builtin-mcps",
            "--disallow-temp-dir",
            "--excluded-tools=" + ",".join(self.EXCLUDED_TOOLS),
        ]
        if req.writable:
            cmd += ["--allow-tool=write", "--allow-tool=shell"]
        else:
            cmd += ["--deny-tool=write", "--deny-tool=shell"]
        if req.settings_file:
            settings = json.loads(req.settings_file.read_text())
            cmd += [f"--plugin-dir={self._guard_plugin(req.settings_file, settings)}"]
            for rule in settings.get("permissions", {}).get("deny", []):
                kind, _, path = rule.partition("(")
                cmd.append(f"--deny-tool={'read' if kind == 'Read' else 'write'}({path}")
        return cmd

    @staticmethod
    def _guard_plugin(settings_file: Path, settings: dict) -> Path:
        root = settings_file.with_name("copilot-guard")
        (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        (root / "hooks.json").write_text(json.dumps({"hooks": settings.get("hooks", {})}, indent=2))
        manifest = {"name": "aidlc-guard", "version": "0.0.0", "hooks": "./hooks.json"}
        (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest, indent=2))
        return root

    @staticmethod
    def _usage(env: dict[str, str], session_id: str) -> tuple[int, int]:
        home = env.get("COPILOT_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".copilot")
        events = Path(home) / "session-state" / session_id / "events.jsonl"
        tin = tout = 0
        if not events.exists():
            return 0, 0
        for line in events.read_text().splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "assistant.usage":
                data = ev.get("data", {})
                tin += sum(data.get(k) or 0 for k in ("inputTokens", "cacheReadTokens", "cacheWriteTokens"))
                tout += data.get("outputTokens") or 0
        return tin, tout

    def run(self, req: AgentRequest) -> AgentResult:
        start = time.monotonic()
        session_id = str(uuid.uuid4())
        proc = run_killable(
            self.command(req, session_id), input=req.prompt, cwd=req.workdir, env=req.env, timeout=req.timeout_s
        )
        text, exit_code = "", proc.returncode
        for line in proc.stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "assistant.message" and (content := ev.get("data", {}).get("content")):
                text = content
            elif ev.get("type") == "result":
                exit_code = ev.get("exitCode", exit_code)
        tin, tout = self._usage(req.env, session_id)
        ok = proc.returncode == 0 and exit_code == 0
        error = "" if ok else (proc.stderr or proc.stdout)[-2000:]
        return AgentResult(text, tin, tout, None, time.monotonic() - start, ok, error)


class FakeAdapter:
    """Scripted adapter for tests and dry runs: handler(request) -> AgentResult | str."""

    name = "fake"

    def __init__(self, handler: Callable[[AgentRequest], AgentResult | str]):
        self.handler = handler
        self.calls: list[AgentRequest] = []

    def available(self) -> bool:
        return True

    def run(self, req: AgentRequest) -> AgentResult:
        self.calls.append(req)
        out = self.handler(req)
        return (
            AgentResult(out, tokens_in=len(req.prompt) // 4, tokens_out=len(out) // 4) if isinstance(out, str) else out
        )


# What each provider can enforce; `aidlc doctor` prints this so adopters see the trade-offs.
CAPABILITIES = {
    "claude": {"pre-tool hooks": True, "reports cost": True, "os sandbox": False},
    "codex": {"pre-tool hooks": False, "reports cost": False, "os sandbox": True},
    "copilot": {"pre-tool hooks": True, "reports cost": False, "os sandbox": False},
}


def default_adapters() -> dict[str, AgentAdapter]:
    return {"claude": ClaudeCodeAdapter(), "codex": CodexAdapter(), "copilot": CopilotAdapter()}
