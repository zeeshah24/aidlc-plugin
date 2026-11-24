import json
import os
import stat
from pathlib import Path

from aidlc import guardrails, harness
from aidlc.adapters import AgentRequest, CopilotAdapter


def _req(tmp_path: Path, writable: bool, settings: Path | None = None, env: dict | None = None) -> AgentRequest:
    return AgentRequest("implement", "do it", "auto", tmp_path, writable, env=env or {}, settings_file=settings)


def _settings(tmp_path: Path) -> Path:
    paths = harness.RunPaths(tmp_path, tmp_path, tmp_path)
    return harness.hook_settings(paths, {"paths": {"no_read": ["**/.env"], "protected": [".git/**"]}})


def test_copilot_command_read_only_denies_write_and_shell(tmp_path: Path):
    cmd = CopilotAdapter().command(_req(tmp_path, writable=False), "sid")
    assert cmd[0] == "copilot"
    assert "--deny-tool=write" in cmd and "--deny-tool=shell" in cmd
    assert "--allow-tool=write" not in cmd
    assert "--model=auto" in cmd and "--session-id=sid" in cmd and "--disable-builtin-mcps" in cmd
    assert not any(a.startswith("-p") for a in cmd)  # prompt goes on stdin


def test_copilot_command_wires_guard_plugin_and_native_denies(tmp_path: Path):
    cmd = CopilotAdapter().command(_req(tmp_path, writable=True, settings=_settings(tmp_path)), "sid")
    assert "--allow-tool=write" in cmd and "--allow-tool=shell" in cmd
    assert "--deny-tool=read(**/.env)" in cmd and "--deny-tool=write(.git/**)" in cmd
    plugin = Path(next(a for a in cmd if a.startswith("--plugin-dir=")).split("=", 1)[1])
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
    hooks = json.loads((plugin / manifest["hooks"]).read_text())["hooks"]
    assert "aidlc guard pre-tool" in hooks["PreToolUse"][0]["hooks"][0]["command"]


MCP_LIST = (
    '{"mcpServers": {"dev-db": {"source": "user"}, "jira": {"source": "plugin"}, "github": {"source": "builtin"}}}'
)


def _fake_copilot(tmp_path: Path, body: str) -> Path:
    fake = tmp_path / "copilot"
    fake.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = mcp ]; then echo '{MCP_LIST}'; exit 0; fi\n"
        'printf "%s\\n" "$@" > "$(dirname "$0")/args"\n'
        "cat >/dev/null\n" + body
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def test_copilot_run_parses_streamed_jsonl(tmp_path: Path):
    fake = _fake_copilot(
        tmp_path,
        'echo \'{"type":"assistant.usage","ephemeral":true,"data":{"inputTokens":100,'
        '"cacheReadTokens":20,"outputTokens":7}}\'\n'
        'echo \'{"type":"assistant.message","data":{"content":"done"}}\'\n'
        'echo \'{"type":"assistant.message","data":{"content":""}}\'\n'
        'echo \'{"type":"result","exitCode":0}\'\n',
    )
    result = CopilotAdapter(binary=str(fake)).run(_req(tmp_path, writable=False, env={"PATH": os.environ["PATH"]}))
    assert result.ok and result.text == "done"
    assert (result.tokens_in, result.tokens_out) == (120, 7)
    args = (tmp_path / "args").read_text().splitlines()
    assert "--disable-mcp-server=dev-db" in args and "--disable-mcp-server=jira" in args
    assert "--disable-mcp-server=github" not in args
    assert "--secret-env-vars=COPILOT_GITHUB_TOKEN" in args


def test_copilot_failed_result_is_not_ok(tmp_path: Path):
    fake = _fake_copilot(tmp_path, 'echo \'Error: not logged in\' >&2\necho \'{"type":"result","exitCode":1}\'\n')
    result = CopilotAdapter(binary=str(fake)).run(_req(tmp_path, writable=False, env={"PATH": os.environ["PATH"]}))
    assert not result.ok and "not logged in" in result.error


def test_copilot_fails_closed_when_mcp_servers_cannot_be_listed(tmp_path: Path):
    fake = tmp_path / "copilot"
    fake.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    result = CopilotAdapter(binary=str(fake)).run(_req(tmp_path, writable=False, env={"PATH": os.environ["PATH"]}))
    assert not result.ok and "mcp list" in result.error


def test_guard_checks_copilot_path_argument(tmp_path: Path):
    rails = {"paths": {"protected": [".git/**"]}}
    decision = guardrails.evaluate("Write", {"path": str(tmp_path / ".git" / "config")}, rails, tmp_path, None)
    assert not decision.allowed
