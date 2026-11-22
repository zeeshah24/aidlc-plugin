import subprocess
from pathlib import Path

import pytest
import yaml

FAKE_MODELS = {
    "tiers": {
        "fast": {"provider": "fake", "model": "fake-fast"},
        "reasoning": {"provider": "fake", "model": "fake-reason"},
        "coding": {"provider": "fake", "model": "fake-code"},
    },
    "pricing_usd_per_mtok": {"fake-reason": {"input": 10.0, "output": 20.0}},
}


def sh(args: list[str], cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def commit(cwd: Path, message: str = "init") -> None:
    sh(["git", "add", "-A"], cwd)
    sh(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", message], cwd)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    sh(["git", "init", "-q", "-b", "main"], root)
    (root / "README.md").write_text("# demo\n")
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.0.1"\n')
    (root / "tests").mkdir()
    (root / "tests" / "test_smoke.py").write_text("def test_smoke():\n    assert True\n")
    (root / "aidlc.yaml").write_text(
        yaml.safe_dump(
            {
                "pipeline": {"human_gates": [], "max_retries": 1, "max_loops": 2},
                "stack": {"test_command": "python -m pytest -q -p no:cacheprovider"},
                "pr": {"enabled": False},
                "dependencies": {"offline": True},
                "models": FAKE_MODELS,
            }
        )
    )
    commit(root)
    return root


@pytest.fixture
def requirement(tmp_path: Path) -> Path:
    path = tmp_path / "DEMO-1.md"
    path.write_text("# DEMO-1: Add numbers\n\nProvide `calc.add(a, b)` returning the sum.\n")
    return path
