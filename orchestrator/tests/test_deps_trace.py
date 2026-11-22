from aidlc import deps, traceability

PLAN = """# Plan
```aidlc-dependencies
- {name: requests, version: "2.32.3", ecosystem: PyPI, reason: http}
- {name: gplthing, version: "1.0", ecosystem: PyPI}
- {name: oldlib, version: "0.1", ecosystem: PyPI}
- {name: nover, ecosystem: PyPI}
- {name: internal, version: "1", ecosystem: Artifactory}
```
"""
RAILS = {
    "dependencies": {"allowed_licenses": ["MIT", "Apache-2.0"], "allowed_registries": ["PyPI"], "block_on_cve": True}
}


def fake_fetch(url, payload=None):
    if payload is not None:
        return {"vulns": [{"id": "GHSA-1"}]} if payload["package"]["name"] == "oldlib" else {}
    lic = {"requests": "Apache 2.0", "gplthing": "GPL-3.0", "oldlib": "MIT", "nover": "MIT"}
    name = url.split("/pypi/")[1].split("/")[0]
    return {"info": {"license": lic[name], "classifiers": []}}


def test_dependency_gate():
    proposals = deps.parse_proposals(PLAN)
    assert [p["name"] for p in proposals] == ["requests", "gplthing", "oldlib", "nover", "internal"]
    status = {r["name"]: r["status"] for r in deps.evaluate(proposals, RAILS, ["PyPI"], fake_fetch)}
    assert status == {
        "requests": "approved",
        "gplthing": "rejected",
        "oldlib": "rejected",
        "nover": "needs_review",
        "internal": "rejected",
    }
    offline = deps.evaluate(proposals[:1], RAILS, ["PyPI"], fake_fetch, offline=True)
    assert offline[0]["status"] == "needs_review"
    assert "requests" in deps.render(offline)


def test_lookup_failure_degrades_to_review():
    def broken(url, payload=None):
        raise OSError("offline")

    res = deps.evaluate([{"name": "x", "version": "1", "ecosystem": "PyPI"}], RAILS, [], broken)
    assert res[0]["status"] == "needs_review"


def test_no_dependency_block():
    assert deps.parse_proposals("# Plan\nnothing") == []


def test_traceability_matrix(tmp_path):
    (tmp_path / "test_a.py").write_text("def test_ac1():  # AC-1\n    pass\n# AC-9\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("# implements REQ-1\n", encoding="utf-8")
    spec = "REQ-1 ... AC-1 ... AC-2 ... AC-1"
    ids = traceability.requirement_ids(spec, r"\b(?:REQ|AC|NFR)-\d+\b")
    assert ids == ["REQ-1", "AC-1", "AC-2"]
    tests = traceability.references(tmp_path, ["test_a.py", "missing.py"], r"\b(?:REQ|AC)-\d+\b")
    code = traceability.references(tmp_path, ["a.py"], r"\b(?:REQ|AC)-\d+\b")
    matrix = traceability.build_matrix(ids, tests, code)
    assert matrix["untested"] == ["REQ-1", "AC-2"]
    assert matrix["orphan_refs"] == ["AC-9"]
    assert "| AC-1 | `test_a.py` |" in traceability.render(matrix)
