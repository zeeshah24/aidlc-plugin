from pathlib import Path

import pytest

from aidlc import intake, stack


def test_markdown_frontmatter_and_heading(tmp_path: Path):
    a = tmp_path / "a.md"
    a.write_text("---\nid: PAY-7\ntitle: Refunds\n---\n# Something\nBody\n")
    req = intake.from_file(a)
    assert (req.req_id, req.title) == ("PAY-7", "Refunds")
    assert req.body.startswith("# Something")
    b = tmp_path / "story.md"
    b.write_text("# SHOP-12: Cart totals\n")
    assert intake.from_file(b).req_id == "SHOP-12"
    evil = tmp_path / "evil.md"
    evil.write_text("---\nid: ../../README\n---\n# x\n")
    assert intake.from_file(evil).req_id == "readme"
    with pytest.raises(ValueError):
        intake.write_requirement(intake.Requirement("../x", "t", "b", "s"), tmp_path / ".aidlc")
    out = intake.write_requirement(intake.from_file(a), tmp_path / ".aidlc")
    assert out.name == "PAY-7.md" and "source: file:" in out.read_text()


def test_jira(monkeypatch):
    seen = []

    def fetch(url):
        seen.append(url)
        return {
            "fields": {
                "summary": "Export CSV",
                "description": "As a user I want CSV export.",
                "issuetype": {"name": "Story"},
                "customfield_1": "Given data, when exporting, then a CSV downloads.",
            }
        }

    jira_cfg = {"base_url": "https://jira.example.com/", "acceptance_criteria_field": "customfield_1"}
    req = intake.from_jira("PROJ-123", jira_cfg, fetch)
    assert seen == ["https://jira.example.com/rest/api/2/issue/PROJ-123"]
    assert req.req_id == "PROJ-123" and "## Acceptance Criteria" in req.body and "Story" in req.body
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    with pytest.raises(ValueError):
        intake.from_jira("PROJ-1", {}, fetch)


def test_stack_detection(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\ndependencies=["fastapi"]\n')
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}, "devDependencies": {"jest": "1"}}')
    (tmp_path / "go.mod").write_text("module x\n")
    profile = stack.detect(tmp_path, {"lint_command": "make lint"})
    assert profile["languages"] == ["python", "javascript", "go"]
    assert {"PyPI", "npm", "Go"} <= set(profile["ecosystems"])
    assert "fastapi" in profile["frameworks"] and "jest" in profile["frameworks"]
    assert profile["test_command"] and profile["lint_command"] == "make lint"
    assert profile["test_globs"]
