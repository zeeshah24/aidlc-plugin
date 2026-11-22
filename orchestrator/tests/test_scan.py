from pathlib import Path

from conftest import commit, sh

from aidlc import scan

PROFILE = {"languages": ["python"], "test_globs": ["tests/*"], "ecosystems": ["PyPI"]}
CFG = {
    "block_severity": "medium",
    "complexity_threshold": 3,
    "duplication_min_lines": 4,
    "analyzers": ["circular_dependency", "duplication", "complexity", "api_drift"],
}

BRANCHY = "def {}(x):\n" + "".join(f"    if x == {i}:\n        return {i}\n" for i in range(5)) + "    return 0\n"


def test_builtin_analyzers(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "a.py").write_text("from pkg import b\n")
    (tmp_path / "pkg" / "b.py").write_text("import pkg.a\n")
    (tmp_path / "c.py").write_text(BRANCHY.format("f"))
    files = ["pkg/__init__.py", "pkg/a.py", "pkg/b.py", "c.py"]
    (tmp_path / "sib").mkdir()
    (tmp_path / "sib" / "__init__.py").write_text("from sib.x import X\n")
    (tmp_path / "sib" / "x.py").write_text("from . import y\nX = 1\n")
    (tmp_path / "sib" / "y.py").write_text("")
    assert scan.circular_imports(tmp_path, ["sib/__init__.py", "sib/x.py", "sib/y.py"]) == []
    (tmp_path / "ini").mkdir()
    (tmp_path / "ini" / "__init__.py").write_text("import outer\n")
    (tmp_path / "ini" / "sub.py").write_text("")
    (tmp_path / "outer.py").write_text("import ini.sub\n")
    assert scan.circular_imports(tmp_path, ["ini/__init__.py", "ini/sub.py", "outer.py"])
    (tmp_path / "ini" / "__init__.py").write_text(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import outer\n"
    )
    assert scan.circular_imports(tmp_path, ["ini/__init__.py", "ini/sub.py", "outer.py"]) == []
    assert [f.category for f in scan.circular_imports(tmp_path, files)] == ["circular_dependency"]
    assert [f.file for f in scan.complexity(tmp_path, files, 3)] == ["c.py"]
    api = scan.public_api(tmp_path, files, [])
    assert "c.f" in api
    drift = scan.api_drift(api, {}, "")
    assert drift and drift[0].severity == "high"
    assert scan.api_drift(api, {}, "We remove `f`.")[0].severity == "low"


def test_baseline_delta_gate(repo: Path, tmp_path: Path):
    block = "".join(f"    total += {i} * value\n" for i in range(6))
    fn = "def {}(value):\n    total = 0\n" + block + "    return total\n"
    (repo / "old.py").write_text(fn.format("one") + "\n\n" + fn.format("two"))
    commit(repo, "legacy duplication")
    base = sh(["git", "rev-parse", "HEAD"], repo).strip()
    result = scan.gate(repo, repo, base, PROFILE, CFG, "", tmp_path / "cache")
    assert result["passed"] and result["baseline_count"] >= 1 and not result["new"]

    (repo / "new.py").write_text(BRANCHY.format("g"))
    commit(repo, "complex")
    result = scan.gate(repo, repo, base, PROFILE, CFG, "", tmp_path / "cache")
    assert not result["passed"]
    assert {f["file"] for f in result["blocking"]} == {"new.py"}
    assert "new.py" in scan.render(result)

    lenient = {**CFG, "complexity_threshold": 50}
    assert scan.gate(repo, repo, base, PROFILE, lenient, "", tmp_path / "cache")["passed"]
    strict = {**CFG, "complexity_threshold": 1}
    result = scan.gate(repo, repo, base, PROFILE, strict, "", tmp_path / "cache")
    assert {f["file"] for f in result["blocking"] if f["category"] == "complexity"} == {"new.py"}
