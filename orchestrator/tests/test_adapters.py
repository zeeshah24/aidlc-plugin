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


def test_copilot_run_parses_jsonl_and_session_usage(tmp_path: Path):
    home = tmp_path / "copilot-home"
    fake = tmp_path / "copilot"
    fake.write_text(
        "#!/bin/sh\n"
        'sid=$(printf "%s\\n" "$@" | sed -n "s/^--session-id=//p")\n'
        'mkdir -p "$COPILOT_HOME/session-state/$sid"\n'
        'echo \'{"type":"assistant.usage","data":{"inputTokens":100,"cacheReadTokens":20,'
        '"outputTokens":7}}\' > "$COPILOT_HOME/session-state/$sid/events.jsonl"\n'
        "cat >/dev/null\n"
        'echo \'{"type":"assistant.message","data":{"content":"done"}}\'\n'
        'echo \'{"type":"assistant.message","data":{"content":""}}\'\n'
        'echo \'{"type":"result","exitCode":0}\'\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {"PATH": os.environ["PATH"], "COPILOT_HOME": str(home)}
    result = CopilotAdapter(binary=str(fake)).run(_req(tmp_path, writable=False, env=env))
    assert result.ok and result.text == "done"
    assert (result.tokens_in, result.tokens_out) == (120, 7)


def test_copilot_failed_result_is_not_ok(tmp_path: Path):
    fake = tmp_path / "copilot"
    fake.write_text(
        '#!/bin/sh\ncat >/dev/null\necho \'Error: not logged in\' >&2\necho \'{"type":"result","exitCode":1}\'\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    result = CopilotAdapter(binary=str(fake)).run(_req(tmp_path, writable=False, env={"HOME": str(tmp_path)}))
    assert not result.ok and "not logged in" in result.error


def test_guard_checks_copilot_path_argument(tmp_path: Path):
    rails = {"paths": {"protected": [".git/**"]}}
    decision = guardrails.evaluate("Write", {"path": str(tmp_path / ".git" / "config")}, rails, tmp_path, None)
    assert not decision.allowed
