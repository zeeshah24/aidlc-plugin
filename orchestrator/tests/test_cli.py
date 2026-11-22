import json
import subprocess
import sys
from pathlib import Path

import pytest

from aidlc.cli import main


@pytest.fixture
def in_repo(repo: Path, monkeypatch) -> Path:
    monkeypatch.chdir(repo)
    return repo


def test_init_creates_runtime_dir(in_repo: Path, tmp_path: Path, capsys):
    org = tmp_path / "org"
    org.mkdir()
    (org / "guardrails.yaml").write_text("commands: {deny: ['*terraform*']}\n")
    (in_repo / "aidlc.yaml").unlink()
    assert main(["init", "--org", str(org)]) == 0
    assert (in_repo / "aidlc.yaml").exists()
    for sub in ("requirements", "runs", "reports"):
        assert (in_repo / ".aidlc" / sub).is_dir()
    assert (in_repo / ".aidlc" / "org" / "guardrails.yaml").exists()
    assert "guardrails.yaml" in capsys.readouterr().out


def test_stages_and_dry_run(in_repo: Path, capsys):
    assert main(["stages"]) == 0
    assert "[7] scan" in capsys.readouterr().out
    assert main(["run", "--from", "verify", "--to", "scan", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "verify" in out and "review" in out and "fake:fake-reason" in out and "plan" not in out


def test_standalone_scan_stage(in_repo: Path, capsys):
    assert main(["run", "--stage", "scan", "--headless"]) == 0
    out = capsys.readouterr().out
    assert "completed" in out
    assert main(["status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["stages"]["scan"] == "passed"
    assert main(["report"]) == 0
    assert "scan" in capsys.readouterr().out


def test_bad_inputs(in_repo: Path, capsys):
    assert main(["run", "not-a-key"]) == 2
    assert main(["run", "--stage", "nope"]) == 2
    assert main(["run"]) == 2


def test_guard_hook_blocks_with_exit_2(repo: Path, tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    env = {"AIDLC_RUN_DIR": str(run_dir), "AIDLC_JAIL": str(repo), "AIDLC_PROJECT_ROOT": str(repo)}
    env["PATH"] = "/usr/bin:/bin"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    def hook(payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "aidlc", "guard", "pre-tool"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
        )

    blocked = hook({"tool_name": "Bash", "tool_input": {"command": "git push origin main"}})
    assert blocked.returncode == 2 and "deny rule" in blocked.stderr
    assert hook({"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}).returncode == 0
    assert hook({"tool_name": "Write", "tool_input": {"file_path": "/etc/passwd"}}).returncode == 2
    lines = [json.loads(x)["event"] for x in (run_dir / "audit.jsonl").read_text().splitlines()]
    assert lines.count("tool.blocked") == 2 and "tool.allowed" in lines


def test_guard_hook_ask_tier(repo: Path, tmp_path: Path):
    from aidlc import permissions

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    env = {"AIDLC_RUN_DIR": str(run_dir), "AIDLC_JAIL": str(repo), "AIDLC_PROJECT_ROOT": str(repo)}
    env |= {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    env |= {"AIDLC_STAGE": "implement"}
    payload = json.dumps({"cwd": str(repo), "tool_name": "Bash", "tool_input": {"command": "terraform plan"}})

    def hook(**extra) -> subprocess.CompletedProcess:
        cmd = [sys.executable, "-m", "aidlc", "guard", "pre-tool"]
        return subprocess.run(cmd, input=payload, capture_output=True, text=True, env=env | extra)

    headless = hook()
    assert headless.returncode == 2 and "headless" in headless.stderr
    assert not permissions.load(run_dir)

    assert hook(AIDLC_INTERACTIVE="1").returncode == 2
    [req] = permissions.pending(run_dir)
    assert req["stage"] == "implement" and req["subject"] == "command:terraform plan"
    permissions.decide(run_dir, req["id"], "once")
    assert hook(AIDLC_INTERACTIVE="1").returncode == 0
    assert hook(AIDLC_INTERACTIVE="1").returncode == 2  # once means once
    permissions.decide(run_dir, req["id"], "deny")
    denied = hook(AIDLC_INTERACTIVE="1")
    assert denied.returncode == 2 and "denied by the developer" in denied.stderr
    events = [json.loads(line)["event"] for line in (run_dir / "audit.jsonl").read_text().splitlines()]
    assert {"permission.requested", "permission.granted", "permission.used", "permission.denied"} <= set(events)

    sudo = json.dumps({"cwd": str(repo), "tool_name": "Bash", "tool_input": {"command": "sudo ls"}})
    proc = subprocess.run(
        [sys.executable, "-m", "aidlc", "guard", "pre-tool"],
        input=sudo,
        capture_output=True,
        text=True,
        env=env | {"AIDLC_INTERACTIVE": "1"},
    )
    assert proc.returncode == 2 and len(permissions.load(run_dir)) == 1  # deny tier never asks


def test_guard_hook_noop_without_active_run(tmp_path: Path):
    proc = subprocess.run(
        [sys.executable, "-m", "aidlc", "guard", "pre-tool"],
        input=json.dumps({"cwd": str(tmp_path), "tool_name": "Bash", "tool_input": {"command": "sudo x"}}),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert proc.returncode == 0
