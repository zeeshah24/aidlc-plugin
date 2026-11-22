"""Agent adapters: run one stateless sub-agent call on a given assistant CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

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
        proc = subprocess.run(
            self.command(req),
            input=req.prompt,
            cwd=req.workdir,
            env=req.env,
            capture_output=True,
            text=True,
            timeout=req.timeout_s,
        )
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
            req.prompt,
        ]

    def run(self, req: AgentRequest) -> AgentResult:
        start = time.monotonic()
        proc = subprocess.run(
            self.command(req),
            cwd=req.workdir,
            env=req.env,
            capture_output=True,
            text=True,
            timeout=req.timeout_s,
        )
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


def default_adapters() -> dict[str, AgentAdapter]:
    return {"claude": ClaudeCodeAdapter(), "codex": CodexAdapter()}
