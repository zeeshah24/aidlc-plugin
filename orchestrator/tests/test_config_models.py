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
    assert any("codex" in e and "implement" in e for e in models.validate(cfg))
    monkeypatch.setenv("AIDLC_ISOLATED", "1")
    assert not any("pre-tool" in e for e in models.validate(cfg))
