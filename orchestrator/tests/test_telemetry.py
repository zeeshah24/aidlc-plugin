import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml
from conftest import sh
from test_pipeline import IMPL, SPEC, TEST, Script, pipeline

from aidlc import intake, stages, telemetry
from aidlc.adapters import FakeAdapter
from aidlc.config import load_config
from aidlc.orchestrator import Pipeline, RunOptions


def configure(repo: Path, **tel) -> None:
    cfg = yaml.safe_load((repo / "aidlc.yaml").read_text())
    cfg["telemetry"] = tel
    (repo / "aidlc.yaml").write_text(yaml.safe_dump(cfg))


@pytest.fixture
def collector():
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": json.loads(body)})
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", received
    server.shutdown()


def run(repo: Path, requirement: Path, **opts):
    pipe, _ = pipeline(repo, Script(), **opts)
    state, paths = pipe.start(intake.from_file(requirement))
    assert pipe.execute(state, paths, stages.select()) == "completed", state.history
    return state, paths


def audit_events(paths) -> list[str]:
    return [json.loads(line)["event"] for line in paths.audit_file.read_text().splitlines()]


def test_disabled_by_default_sends_nothing(repo, requirement, collector, monkeypatch):
    url, received = collector
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", url)  # an ambient endpoint alone does not opt in
    state, paths = run(repo, requirement)
    assert received == []
    assert not any(e.startswith("telemetry.") for e in audit_events(paths))
    assert telemetry.summary(load_config(repo), state, paths)["status"] == "completed"


def test_enabled_exports_content_free_trace(repo, requirement, collector, monkeypatch):
    url, received = collector
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer t0k")
    configure(repo, enabled=True, endpoint=url, file=".aidlc/telemetry.jsonl", team="payments")
    state, paths = run(repo, requirement)

    [post] = received
    assert post["path"] == "/v1/traces" and post["auth"] == "Bearer t0k"
    spans = post["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    names = {s["name"] for s in spans}
    assert {"aidlc.run", "aidlc.stage.implement", "aidlc.stage.review"} <= names
    assert len({s["traceId"] for s in spans}) == 1
    review = next(s for s in spans if s["name"] == "aidlc.stage.review")
    attrs = {a["key"]: a["value"] for a in review["attributes"]}
    assert attrs["gen_ai.request.model"] == {"stringValue": "fake:fake-reason"}
    assert "gen_ai.usage.input_tokens" in attrs and attrs["aidlc.stage.status"] == {"stringValue": "passed"}

    [record] = [json.loads(x) for x in (repo / ".aidlc/telemetry.jsonl").read_text().splitlines()]
    assert record["run_id"].startswith("h:") and record["team"] == "payments"
    assert record["repo"].startswith("h:") and "developer" not in record
    exported = json.dumps(post["body"]) + json.dumps(record)
    leaks = (state.run_id, state.req_id, SPEC.splitlines()[2], IMPL, TEST, "calc.py", "Add numbers", str(repo))
    for content in (*leaks, "t@example.com"):
        assert content not in exported
    assert "telemetry.exported" in audit_events(paths)


def test_raw_identifiers_and_developer_are_opt_in(repo, requirement):
    configure(repo, enabled=True, raw_identifiers=True, include_developer=True)
    sh(["git", "config", "user.email", "dev@example.com"], repo)
    state, paths = run(repo, requirement)
    record = telemetry.summary(load_config(repo), state, paths)
    assert record["repo"] == repo.name and record["developer"] == "dev@example.com"
    assert record["run_id"] == state.run_id


def test_export_failure_does_not_fail_run(repo, requirement):
    configure(repo, enabled=True, endpoint="http://127.0.0.1:9")
    _, paths = run(repo, requirement)
    assert "telemetry.failed" in audit_events(paths)


def test_run_id_in_commits_and_pr_body(repo, requirement):
    state, paths = run(repo, requirement)
    log = sh(["git", "log", "--format=%B", f"{state.base_sha}..{state.branch}"], repo)
    assert log.count(f"AIDLC-Run: {state.run_id}") == 2
    assert f"AIDLC-Run: {state.run_id}" in (paths.run_dir / "pr-body.md").read_text()


def test_rating_asked_at_end_of_interactive_run(repo, requirement):
    configure(repo, enabled=True)
    questions: list[str] = []
    ask = lambda q: questions.append(q) or "4"  # noqa: E731
    pipe = Pipeline(load_config(repo), {"fake": FakeAdapter(Script())}, RunOptions(), ask=ask, out=lambda _m: None)
    state, paths = pipe.start(intake.from_file(requirement))
    assert pipe.execute(state, paths, stages.select()) == "completed"
    assert len(questions) == 1 and state.rating == 4
    assert "Developer rating: 4/5" in paths.report_file.read_text()


def test_rating_never_asked_headless(repo, requirement):
    configure(repo, enabled=True)
    asked = []
    opts = RunOptions(headless=True)
    pipe = Pipeline(load_config(repo), {"fake": FakeAdapter(Script())}, opts, ask=asked.append, out=lambda _m: None)
    state, paths = pipe.start(intake.from_file(requirement))
    assert pipe.execute(state, paths, stages.select()) == "completed"
    assert asked == [] and state.rating is None


def test_org_telemetry_policy_cannot_be_widened(repo):
    org = repo / ".aidlc" / "org"
    org.mkdir(parents=True)
    (org / "aidlc.yaml").write_text(yaml.safe_dump({"telemetry": {"enabled": True, "endpoint": "https://otel.corp"}}))
    configure(repo, enabled=True, endpoint="https://elsewhere", raw_identifiers=True)
    cfg = load_config(repo)
    assert cfg.get("telemetry")["endpoint"] == "https://otel.corp"
    assert not cfg.get("telemetry")["raw_identifiers"]
    assert any("telemetry" in w for w in cfg.warnings)
    configure(repo, enabled=False)
    assert not telemetry.enabled(load_config(repo))


def test_outcomes_join_prs_by_run_id():
    prs = [
        {
            "number": 7,
            "state": "MERGED",
            "createdAt": "2026-11-20T10:00:00Z",
            "mergedAt": "2026-11-20T16:30:00Z",
            "body": "## X\n\nAIDLC-Run: 20261120-abc\n",
            "reviews": [{"state": "CHANGES_REQUESTED"}, {"state": "APPROVED"}],
            "commits": [
                {"messageBody": "AIDLC-Run: 20261120-abc"},
                {"messageBody": "AIDLC-Run: 20261120-abc"},
                {"messageBody": "fix typo"},
            ],
        },
        {"number": 8, "state": "OPEN", "body": "manual PR", "reviews": [], "commits": []},
    ]
    [out] = telemetry.outcomes(prs, [{"number": 9, "body": "Reverts acme/app#7"}])
    assert out["run_id"] == "20261120-abc" and out["merged"] and out["hours_to_merge"] == 6.5
    assert (out["reviews"], out["changes_requested"]) == (2, 1)
    assert (out["agent_commits"], out["human_commits"]) == (2, 1)
    assert out["reverted"] and out["state"] == "merged"
    span = telemetry.outcome_span(out, "2026-11-22T00:00:00+00:00")
    assert span["traceId"] == telemetry.trace_id("20261120-abc")


def test_outcome_run_ids_hash_like_run_records(repo, requirement):
    cfg = load_config(repo)
    state, paths = run(repo, requirement)
    run_record = telemetry.summary(cfg, state, paths)
    [outcome] = telemetry.pseudonymize(cfg, [{"run_id": state.run_id, "pr": 1}])
    assert outcome["run_id"] == run_record["run_id"] != state.run_id


def test_empty_org_telemetry_policy_still_owns_settings(repo):
    org = repo / ".aidlc" / "org"
    org.mkdir(parents=True)
    (org / "aidlc.yaml").write_text(yaml.safe_dump({"telemetry": {}}))
    configure(repo, enabled=True, endpoint="https://elsewhere")
    assert not telemetry.enabled(load_config(repo))
