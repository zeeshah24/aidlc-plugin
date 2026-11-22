"""Opt-in, content-free run telemetry owned by the adopting organisation.

Nothing is sent unless `telemetry.enabled` is true, and only to the endpoint or file the adopter
configures; the plugin has no endpoint of its own. Records hold counts, durations, outcomes and
model names, never prompts, code, diffs, paths or command text. Repo and developer identifiers are
salted hashes unless `telemetry.raw_identifiers` is set. `aidlc telemetry show` prints exactly what
would be exported.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

from aidlc import __version__, permissions
from aidlc.config import Config
from aidlc.harness import RunPaths
from aidlc.state import RunState

SCHEMA = "aidlc.run/1"
RUN_TRAILER = "AIDLC-Run"


def settings(cfg: Config) -> dict:
    return cfg.get("telemetry", {}) or {}


def enabled(cfg: Config) -> bool:
    return bool(settings(cfg).get("enabled"))


def _ident(cfg: Config, value: str) -> str:
    if not value or settings(cfg).get("raw_identifiers"):
        return value
    salt = os.environ.get("AIDLC_TELEMETRY_SALT", "")
    return "h:" + hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()[:16]


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _audit_counts(paths: RunPaths) -> dict[str, int]:
    counts: dict[str, int] = {}
    if paths.audit_file.exists():
        for line in paths.audit_file.read_text(encoding="utf-8").splitlines():
            event = json.loads(line).get("event", "")
            counts[event] = counts.get(event, 0) + 1
    return counts


def _seconds(start: str | None, end: str | None) -> float:
    if not start or not end:
        return 0.0
    return max((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 0.0)


def summary(cfg: Config, state: RunState, paths: RunPaths) -> dict:
    """The run record exported to the adopter's telemetry sink."""
    tcfg = settings(cfg)
    requests = permissions.load(paths.run_dir)
    audit = _audit_counts(paths)
    repo = _git(["remote", "get-url", "origin"], cfg.root) or cfg.root.name
    record = {
        "schema": SCHEMA,
        "aidlc_version": __version__,
        "run_id": _ident(cfg, state.run_id),  # run ids embed the requirement id
        "team": tcfg.get("team", ""),
        "repo": _ident(cfg, repo),
        "status": state.status,
        "created": state.created,
        "loops": state.loops,
        "rating": state.rating,
        "totals": {
            "duration_s": round(sum(r.duration_s for r in state.stages.values()), 3),
            "tokens_in": sum(r.tokens_in for r in state.stages.values()),
            "tokens_out": sum(r.tokens_out for r in state.stages.values()),
            "cost_usd": round(sum(r.cost_usd for r in state.stages.values()), 6),
        },
        "stages": {
            name: {
                "status": r.status,
                "attempts": r.attempts,
                "duration_s": r.duration_s,
                "tokens_in": r.tokens_in,
                "tokens_out": r.tokens_out,
                "cost_usd": r.cost_usd,
                "models": r.models,
                "gate": r.gate,
                "started": r.started,
                "ended": r.ended,
            }
            for name, r in state.stages.items()
        },
        "friction": {
            "human_gates_approved": len(state.approvals),
            "permission_requests": len(requests),
            "permissions_granted": sum(r["status"] in ("allowed_once", "allowed_run", "used") for r in requests),
            "permissions_denied": sum(r["status"] == "denied" for r in requests),
            "permission_wait_s": round(sum(_seconds(r.get("requested"), r.get("decided")) for r in requests), 1),
            "guardrail_blocks": audit.get("tool.blocked", 0),
        },
    }
    if tcfg.get("include_developer"):
        record["developer"] = _ident(cfg, _git(["config", "user.email"], cfg.root))
    return record


# --- OTLP/HTTP JSON (no SDK dependency) -------------------------------------------------------


def trace_id(run_id: str) -> str:
    return hashlib.sha256(f"trace:{run_id}".encode()).hexdigest()[:32]


def _span_id(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:16]


def _nanos(iso: str | None) -> str:
    return str(int(datetime.fromisoformat(iso).timestamp() * 1e9)) if iso else "0"


def _attrs(values: dict) -> list[dict]:
    out = []
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            v = {"boolValue": value}
        elif isinstance(value, int):
            v = {"intValue": str(value)}
        elif isinstance(value, float):
            v = {"doubleValue": value}
        else:
            v = {"stringValue": ", ".join(value) if isinstance(value, list) else str(value)}
        out.append({"key": key, "value": v})
    return out


def _envelope(spans: list[dict]) -> dict:
    resource = _attrs({"service.name": "aidlc", "service.version": __version__})
    return {
        "resourceSpans": [
            {"resource": {"attributes": resource}, "scopeSpans": [{"scope": {"name": "aidlc"}, "spans": spans}]}
        ]
    }


def run_spans(record: dict) -> list[dict]:
    """One trace per run: a root span plus one child span per stage (GenAI usage attributes)."""
    tid, root = trace_id(record["run_id"]), _span_id(record["run_id"], "run")
    stages = record["stages"].values()
    starts = [s["started"] for s in stages if s["started"]]
    ends = [s["ended"] for s in stages if s["ended"]]
    root_attrs = {
        "aidlc.run_id": record["run_id"],
        "aidlc.team": record["team"],
        "aidlc.repo": record["repo"],
        "aidlc.status": record["status"],
        "aidlc.loops": record["loops"],
        "aidlc.rating": record["rating"],
        "aidlc.cost_usd": record["totals"]["cost_usd"],
        "gen_ai.usage.input_tokens": record["totals"]["tokens_in"],
        "gen_ai.usage.output_tokens": record["totals"]["tokens_out"],
        **{f"aidlc.friction.{k}": v for k, v in record["friction"].items()},
    }
    if "developer" in record:
        root_attrs["aidlc.developer"] = record["developer"]
    spans = [
        {
            "traceId": tid,
            "spanId": root,
            "name": "aidlc.run",
            "kind": 1,
            "startTimeUnixNano": _nanos(min(starts) if starts else record["created"]),
            "endTimeUnixNano": _nanos(max(ends) if ends else record["created"]),
            "attributes": _attrs(root_attrs),
            "status": {"code": 1 if record["status"] == "completed" else 2},
        }
    ]
    for name, s in record["stages"].items():
        if not s["started"]:
            continue
        spans.append(
            {
                "traceId": tid,
                "spanId": _span_id(record["run_id"], "stage", name),
                "parentSpanId": root,
                "name": f"aidlc.stage.{name}",
                "kind": 1,
                "startTimeUnixNano": _nanos(s["started"]),
                "endTimeUnixNano": _nanos(s["ended"] or s["started"]),
                "attributes": _attrs(
                    {
                        "aidlc.stage": name,
                        "aidlc.stage.status": s["status"],
                        "aidlc.stage.attempts": s["attempts"],
                        "aidlc.stage.gate": s["gate"],
                        "aidlc.cost_usd": s["cost_usd"],
                        "gen_ai.request.model": s["models"],
                        "gen_ai.usage.input_tokens": s["tokens_in"],
                        "gen_ai.usage.output_tokens": s["tokens_out"],
                    }
                ),
                "status": {"code": 2 if s["status"] == "failed" else 1},
            }
        )
    return spans


def outcome_span(record: dict, now: str) -> dict:
    """Joins a PR outcome to the run's trace (same trace id)."""
    return {
        "traceId": trace_id(record["run_id"]),
        "spanId": _span_id(record["run_id"], "outcome", now),
        "parentSpanId": _span_id(record["run_id"], "run"),
        "name": "aidlc.outcome",
        "kind": 1,
        "startTimeUnixNano": _nanos(now),
        "endTimeUnixNano": _nanos(now),
        "attributes": _attrs({f"aidlc.outcome.{k}": v for k, v in record.items() if k != "schema"}),
    }


def _headers(tcfg: dict) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    raw = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
    for pair in filter(None, raw.split(",")):
        key, _, value = pair.partition("=")
        headers[key.strip()] = value.strip()
    headers.update(tcfg.get("headers", {}) or {})
    return headers


def send(cfg: Config, records: list[dict], spans: list[dict]) -> list[str]:
    """Deliver to the adopter's sinks. Returns the sinks written; raises on delivery errors."""
    tcfg = settings(cfg)
    if not tcfg.get("enabled"):
        return []
    sinks = []
    if tcfg.get("file"):
        path = Path(tcfg["file"]).expanduser()
        path = path if path.is_absolute() else cfg.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.writelines(json.dumps(r, sort_keys=True) + "\n" for r in records)
        sinks.append("file")
    endpoint = tcfg.get("endpoint") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if endpoint and spans:
        req = urllib.request.Request(
            endpoint.rstrip("/") + "/v1/traces",
            data=json.dumps(_envelope(spans)).encode(),
            headers=_headers(tcfg),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):  # noqa: S310 - adopter-configured endpoint
            pass
        sinks.append("otlp")
    return sinks


def export_run(cfg: Config, state: RunState, paths: RunPaths) -> list[str]:
    record = summary(cfg, state, paths)
    return send(cfg, [record], run_spans(record))


# --- PR outcomes (GitHub) ---------------------------------------------------------------------

_RUN_RE = re.compile(rf"^{RUN_TRAILER}:\s*(\S+)", re.MULTILINE)
_REVERT_RE = re.compile(r"Reverts\s+[\w.-]+/[\w.-]+#(\d+)")


def _hours(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    s, e = (datetime.fromisoformat(x.replace("Z", "+00:00")) for x in (start, end))
    return round((e - s).total_seconds() / 3600, 2)


def outcomes(prs: list[dict], reverts: list[dict]) -> list[dict]:
    """Outcome records for PRs opened by aidlc runs (identified by the AIDLC-Run trailer)."""
    reverted = {int(m) for r in reverts for m in _REVERT_RE.findall(r.get("body") or "")}
    out = []
    for pr in prs:
        match = _RUN_RE.search(pr.get("body") or "")
        if not match:
            continue
        commits = pr.get("commits") or []
        reviews = pr.get("reviews") or []
        human = [c for c in commits if f"{RUN_TRAILER}:" not in (c.get("messageBody") or "")]
        out.append(
            {
                "schema": "aidlc.outcome/1",
                "run_id": match.group(1),
                "pr": pr["number"],
                "state": pr.get("state", "").lower(),
                "merged": bool(pr.get("mergedAt")),
                "hours_to_merge": _hours(pr.get("createdAt"), pr.get("mergedAt")),
                "reviews": len(reviews),
                "changes_requested": sum(r.get("state") == "CHANGES_REQUESTED" for r in reviews),
                "agent_commits": len(commits) - len(human),
                "human_commits": len(human),
                "reverted": pr["number"] in reverted,
            }
        )
    return out


def pseudonymize(cfg: Config, records: list[dict]) -> list[dict]:
    """Hash outcome run ids the same way as run records, so both still join."""
    return [{**r, "run_id": _ident(cfg, r["run_id"])} for r in records]


def fetch_outcomes(root: Path, limit: int) -> list[dict]:
    fields = "number,state,createdAt,mergedAt,body,reviews,commits"

    def gh(search: str, json_fields: str) -> list[dict]:
        args = ["gh", "pr", "list", "--state", "all", "--limit", str(limit), "--search", search]
        proc = subprocess.run(
            [*args, "--json", json_fields],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=300,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            raise RuntimeError(f"gh pr list failed: {proc.stderr.strip()[-300:]}")
        return json.loads(proc.stdout or "[]")

    return outcomes(gh(f'"{RUN_TRAILER}:" in:body', fields), gh('"Reverts" in:body', "number,body"))
