from pathlib import Path

import yaml

from aidlc import models
from aidlc.config import load_config, merge_guardrails


def test_project_guardrails_only_tighten():
    base = {"commands": {"allow": ["ls*", "pytest*"], "deny": ["sudo*"]}, "paths": {"protected": [".git/**"]}}
    override = {"commands": {"allow": ["pytest*", "curl *"], "deny": ["*rm *"]}, "paths": {"protected": ["db/**"]}}
    merged, warnings = merge_guardrails(base, override)
    assert merged["commands"]["allow"] == ["pytest*"]
    assert set(merged["commands"]["deny"]) == {"sudo*", "*rm *"}
    assert set(merged["paths"]["protected"]) == {".git/**", "db/**"}
    assert any("curl" in w for w in warnings)


def test_layers_org_then_project(repo: Path):
    org = repo / ".aidlc" / "org"
    org.mkdir(parents=True)
    (org / "aidlc.yaml").write_text(yaml.safe_dump({"pipeline": {"max_loops": 9, "agent_timeout_s": 5}}))
    (org / "conventions.md").write_text("Use type hints.")
    cfg = load_config(repo)
    assert cfg.get("pipeline.max_loops") == 2  # project wins
    assert cfg.get("pipeline.agent_timeout_s") == 5  # org over defaults
    assert cfg.conventions[0] == ("org", "Use type hints.")


def test_default_routing_reviewer_differs(repo: Path):
    (repo / "aidlc.yaml").write_text("{}\n")
    cfg = load_config(repo)
    assert models.validate(cfg) == []
    assert models.resolve(cfg, "review") != models.resolve(cfg, "implement")


def test_same_implementer_and_reviewer_rejected(repo: Path):
    cfg = load_config(repo)
    cfg.models["stages"]["review"] = "coding"
    assert any("different model" in e for e in models.validate(cfg))


def test_explicit_route_and_cost(repo: Path):
    cfg = load_config(repo)
    cfg.models["stages"]["review"] = {"provider": "codex", "model": "gpt-5"}
    choice = models.resolve(cfg, "review")
    assert (choice.provider, choice.model) == ("codex", "gpt-5")
    assert models.estimate_cost(cfg, "fake-reason", 1_000_000, 500_000) == 20.0
    assert models.estimate_cost(cfg, "unknown", 10, 10) == 0.0


def test_project_cannot_relax_ask_tier():
    base = {"commands": {"allow": ["ls*"], "deny": [], "unlisted": "deny", "ask": ["make *"]}, "paths": {}}
    override = {"commands": {"unlisted": "ask", "ask": ["make *", "terraform *"]}, "paths": {"ask": ["db/**"]}}
    merged, warnings = merge_guardrails(base, override)
    assert merged["commands"]["unlisted"] == "deny"
    assert merged["commands"]["ask"] == ["make *"]
    assert merged["paths"]["ask"] == ["db/**"]
    assert len(warnings) == 2
    tightened, _ = merge_guardrails({"commands": {"unlisted": "ask"}}, {"commands": {"unlisted": "deny"}})
    assert tightened["commands"]["unlisted"] == "deny"


def test_codex_writers_need_isolation(repo: Path, monkeypatch):
    monkeypatch.delenv("AIDLC_ISOLATED", raising=False)
    cfg = load_config(repo)
    cfg.models["tiers"]["coding"] = {"provider": "codex", "model": "gpt-x"}
    assert not any("pre-tool" in e for e in models.validate(cfg))  # read-only commands still work
    assert models.isolation_errors(cfg, ["review"]) == []
    assert "implement" in models.isolation_errors(cfg, ["implement"])[0]
    monkeypatch.setenv("AIDLC_ISOLATED", "1")
    assert models.isolation_errors(cfg, ["test", "implement"]) == []


def test_writers_need_isolation_by_default(repo: Path, monkeypatch):
    monkeypatch.delenv("AIDLC_ISOLATED", raising=False)
    cfg = load_config(repo)
    cfg.models["tiers"]["coding"] = {"provider": "claude", "model": "claude-x"}
    assert "container" in models.isolation_errors(cfg, ["implement"])[0]
    cfg.project.setdefault("harness", {})["require_isolation"] = False
    assert models.isolation_errors(cfg, ["implement"]) == []


def test_project_cannot_relax_org_isolation(repo: Path):
    import yaml

    org = repo / ".aidlc" / "org"
    org.mkdir(parents=True)
    (org / "aidlc.yaml").write_text(yaml.safe_dump({"harness": {"require_isolation": True}}))
    data = yaml.safe_load((repo / "aidlc.yaml").read_text())
    for relaxed in (False, None, 0):
        data["harness"] = {"require_isolation": relaxed}
        (repo / "aidlc.yaml").write_text(yaml.safe_dump(data))
        cfg = load_config(repo)
        assert cfg.get("harness.require_isolation") is True and any("isolation" in w for w in cfg.warnings)


def test_unknown_and_mistyped_keys_warn(repo: Path):
    import yaml

    data = yaml.safe_load((repo / "aidlc.yaml").read_text())
    data["pipline"] = {"max_loops": 1}
    data["pr"] = "yes"
    (repo / "aidlc.yaml").write_text(yaml.safe_dump(data))
    warnings = load_config(repo).warnings
    assert any("unknown key 'pipline'" in w for w in warnings)
    assert any("'pr' must be a mapping" in w for w in warnings)


def test_state_from_newer_schema_is_refused_and_unknown_fields_dropped(tmp_path: Path):
    import json

    import pytest

    from aidlc.state import RunState

    RunState("r", "R-1", "t", "s", "main", "abc", "b", str(tmp_path)).save(tmp_path)
    data = json.loads((tmp_path / "state.json").read_text())
    data["stages"] = {"verify": {"status": "passed", "future_field": 1}}
    (tmp_path / "state.json").write_text(json.dumps(data))
    assert RunState.load(tmp_path).stages["verify"].status == "passed"
    data["schema"] = 99
    (tmp_path / "state.json").write_text(json.dumps(data))
    with pytest.raises(SystemExit, match="newer aidlc"):
        RunState.load(tmp_path)


def test_project_can_narrow_but_not_widen_tools_allow():
    from aidlc.config import merge_guardrails

    merged, _ = merge_guardrails({"tools": {"allow": ["WebFetch"]}}, {"tools": {"allow": []}})
    assert merged["tools"]["allow"] == []
    merged, warnings = merge_guardrails({"tools": {"allow": []}}, {"tools": {"allow": ["WebFetch"]}})
    assert merged["tools"]["allow"] == [] and warnings
