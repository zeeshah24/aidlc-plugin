import json
from pathlib import Path

import pytest
import yaml

from aidlc import intake, stages
from aidlc.adapters import AgentRequest, AgentResult, FakeAdapter
from aidlc.config import load_config
from aidlc.orchestrator import Pipeline, PipelineError, RunOptions

SPEC = """# Spec: DEMO-1 Add numbers
## Requirements
- **REQ-1** (net-new): `calc.add(a, b)` returns the sum.
## Acceptance Criteria
- **AC-1** (REQ-1): Given 2 and 3, when add is called, then it returns 5.
## Open Questions
- None
"""
PLAN = "# Plan: DEMO-1\n## Approach\nAdd calc.py.\n## Dependencies\n```aidlc-dependencies\n[]\n```\n"
TEST = "from calc import add\n\n\ndef test_ac1_add():  # AC-1\n    assert add(2, 3) == 5\n"
IMPL = "def add(a, b):  # REQ-1\n    return a + b\n"


class Script:
    """Scripted agent: per-stage list of responses (callables or text); last one repeats."""

    def __init__(self, **overrides):
        self.steps = {
            "analyze": [SPEC],
            "plan": [PLAN],
            "test": [lambda r: write(r, "tests/test_calc.py", TEST)],
            "implement": [lambda r: write(r, "calc.py", IMPL)],
            "verify": ["# Verification\n- None\n\nVERDICT: PASS"],
            "review": ["# Review\n## Blockers\n- None\n\nVERDICT: APPROVE"],
        }
        self.steps.update(overrides)
        self.count: dict[str, int] = {}

    def __call__(self, req: AgentRequest) -> AgentResult | str:
        n = self.count.get(req.stage, 0)
        self.count[req.stage] = n + 1
        seq = self.steps[req.stage]
        step = seq[min(n, len(seq) - 1)]
        return step(req) if callable(step) else step


def write(req: AgentRequest, rel: str, text: str) -> str:
    path = req.workdir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return f"wrote {rel}"


def pipeline(repo: Path, script: Script, **opts) -> tuple[Pipeline, FakeAdapter]:
    fake = FakeAdapter(script)
    return Pipeline(load_config(repo), {"fake": fake}, RunOptions(**opts), out=lambda _m: None), fake


def test_full_run_end_to_end(repo: Path, requirement: Path):
    pipe, fake = pipeline(repo, Script())
    state, paths = pipe.start(intake.from_file(requirement))
    status = pipe.execute(state, paths, stages.select())
    assert status == "completed", state.history
    assert {n: r.status for n, r in state.stages.items()} == {
        "intake": "passed",
        "analyze": "passed",
        "plan": "passed",
        "test": "passed",
        "implement": "passed",
        "verify": "passed",
        "review": "passed",
        "scan": "passed",
        "pr": "skipped",
        "report": "passed",
    }
    for name in ("spec", "plan", "test_lock", "verification", "review", "scan", "stack_profile"):
        assert paths.artifact(name).exists(), name
    lock = json.loads(paths.artifact("test_lock").read_text())
    assert list(lock["files"]) == ["tests/test_calc.py"] and lock["red_exit_code"] != 0
    report = paths.report_file.read_text()
    assert "fake:fake-reason" in report and "$" in report
    assert state.stages["analyze"].cost_usd > 0
    impl = next(c for c in fake.calls if c.stage == "implement")
    rev = next(c for c in fake.calls if c.stage == "review")
    assert impl.model != rev.model
    assert impl.env["AIDLC_STAGE"] == "implement" and "AWS_SECRET_ACCESS_KEY" not in impl.env
    # work happened on an isolated branch, not on main
    assert state.branch.startswith("aidlc/") and Path(state.workdir) != repo
    assert not (repo / "calc.py").exists() and (Path(state.workdir) / "calc.py").exists()
    events = [json.loads(line)["event"] for line in paths.audit_file.read_text().splitlines()]
    assert "run.start" in events and "agent.start" in events


def test_review_blocker_loops_back_to_implement(repo: Path, requirement: Path):
    review = ["## Blockers\n- calc.py: add a docstring\n\nVERDICT: REQUEST_CHANGES", "VERDICT: APPROVE"]
    script = Script(review=review)
    pipe, fake = pipeline(repo, script)
    state, paths = pipe.start(intake.from_file(requirement))
    assert pipe.execute(state, paths, stages.select()) == "completed"
    assert script.count["implement"] == 2 and state.loops == 1
    second = [c for c in fake.calls if c.stage == "implement"][1]
    assert "add a docstring" in second.prompt


def test_loops_are_bounded(repo: Path, requirement: Path):
    script = Script(verify=["## Gaps\n- AC-1 missing\n\nVERDICT: GAPS"])
    pipe, _ = pipeline(repo, script)
    state, paths = pipe.start(intake.from_file(requirement))
    assert pipe.execute(state, paths, stages.select()) != "completed"
    assert state.loops == 2 and script.count["verify"] == 3
    assert paths.report_file.exists()


def test_red_tests_must_fail_first(repo: Path, requirement: Path):
    passing = lambda r: write(r, "tests/test_calc.py", "def test_ac1():  # AC-1\n    assert True\n")  # noqa: E731
    script = Script(test=[passing, lambda r: write(r, "tests/test_calc.py", TEST)])
    pipe, _ = pipeline(repo, script)
    state, paths = pipe.start(intake.from_file(requirement))
    assert pipe.execute(state, paths, stages.select(end="test")) == "completed"
    assert script.count["test"] == 2
    assert any("must fail" in n for n in state.stages["test"].notes)


def test_red_gate_flags_failing_baseline(repo: Path, requirement: Path):
    from conftest import commit

    (repo / "tests" / "test_broken.py").write_text("def test_broken():\n    assert False\n")
    commit(repo, "broken")
    pipe, fake = pipeline(repo, Script())
    state, paths = pipe.start(intake.from_file(requirement))
    pipe.execute(state, paths, stages.select(end="test"))
    assert state.stages["test"].gate == "red-unverified"
    assert [c for c in fake.calls if c.stage == "test"]


def test_non_test_edits_by_test_writer_are_discarded(repo: Path, requirement: Path):
    def sneaky(r):
        write(r, "calc.py", IMPL)
        return write(r, "tests/test_calc.py", TEST)

    pipe, _ = pipeline(repo, Script(test=[sneaky]))
    state, paths = pipe.start(intake.from_file(requirement))
    assert pipe.execute(state, paths, stages.select(end="test")) == "completed"
    assert not (Path(state.workdir) / "calc.py").exists()


def test_locked_tests_restored_after_implementer_edits(repo: Path, requirement: Path):
    def cheat(r):
        write(r, "tests/test_calc.py", "def test_ac1():  # AC-1\n    assert True\n")
        return "cheated"

    pipe, _ = pipeline(repo, Script(implement=[cheat, lambda r: write(r, "calc.py", IMPL)]))
    state, paths = pipe.start(intake.from_file(requirement))
    assert pipe.execute(state, paths, stages.select(end="implement")) == "completed"
    assert (Path(state.workdir) / "tests" / "test_calc.py").read_text() == TEST
    assert any("restored" in n for n in state.stages["implement"].notes)


def test_human_gate_pauses_and_resumes(repo: Path, requirement: Path):
    cfg_file = repo / "aidlc.yaml"
    cfg = yaml.safe_load(cfg_file.read_text())
    cfg["pipeline"]["human_gates"] = ["analyze"]
    cfg_file.write_text(yaml.safe_dump(cfg))
    pipe, _ = pipeline(repo, Script(), headless=True)
    state, paths = pipe.start(intake.from_file(requirement))
    assert pipe.execute(state, paths, stages.select()) == "awaiting_approval"
    assert state.stages["analyze"].status == "awaiting_approval" and "plan" not in state.stages

    pipe2, _ = pipeline(repo, Script(), headless=True, approve=["analyze"])
    state, paths = pipe2.open(pipe2.current_run_id())
    assert pipe2.execute(state, paths, stages.select(), skip_passed=True) == "completed"
    assert "analyze" in state.approvals

    pipe3, _ = pipeline(repo, Script(), headless=True)
    state, paths = pipe3.open(pipe3.current_run_id())
    assert pipe3.execute(state, paths, stages.select(stage="analyze")) == "awaiting_approval"


def test_missing_prerequisite_fails_fast(repo: Path):
    pipe, fake = pipeline(repo, Script())
    state, paths = pipe.start_adhoc()
    with pytest.raises(PipelineError, match="missing prerequisites: spec"):
        pipe.execute(state, paths, stages.select(stage="verify"))
    assert not fake.calls


def test_in_place_writes_refuse_dirty_checkout(repo: Path):
    (repo / "wip.py").write_text("x = 1\n")
    pipe, fake = pipeline(repo, Script())
    state, paths = pipe.start_adhoc()
    with pytest.raises(PipelineError, match="uncommitted"):
        pipe.execute(state, paths, stages.select(stage="implement"))
    assert (repo / "wip.py").exists() and not fake.calls


def test_unavailable_provider_is_reported(repo: Path, requirement: Path):
    pipe = Pipeline(load_config(repo), {}, RunOptions(), out=lambda _m: None)
    state, paths = pipe.start(intake.from_file(requirement))
    assert pipe.execute(state, paths, stages.select(end="analyze")) == "failed"
    assert "not available" in state.history[-1]["detail"]
