"""Stage 9: deterministic run report (time, tokens, cost, retries, gate outcomes)."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from aidlc import permissions
from aidlc.config import Config
from aidlc.harness import RunPaths
from aidlc.stages import STAGES
from aidlc.state import RunState


def _fmt_time(seconds: float) -> str:
    return f"{seconds:.1f}s" if seconds < 60 else f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def build(cfg: Config, state: RunState, paths: RunPaths) -> str:
    rows, tot = [], {"time": 0.0, "in": 0, "out": 0, "cost": 0.0, "retries": 0}
    for st in STAGES:
        rec = state.stages.get(st.name)
        if rec is None:
            continue
        retries = max(rec.attempts - 1, 0)
        tot["time"] += rec.duration_s
        tot["in"] += rec.tokens_in
        tot["out"] += rec.tokens_out
        tot["cost"] += rec.cost_usd
        tot["retries"] += retries
        rows.append(
            f"| {st.index} | {st.name} | {rec.status} | {rec.attempts} | {retries} | "
            f"{_fmt_time(rec.duration_s)} | {rec.tokens_in:,} | {rec.tokens_out:,} | "
            f"${rec.cost_usd:.4f} | {', '.join(rec.models) or st.tier} | {rec.gate or '-'} |"
        )
    lines = [
        f"# AI-DLC run report: {state.run_id}",
        "",
        f"- **Requirement:** {state.req_id} — {state.title}",
        f"- **Source:** `{state.source}`",
        f"- **Status:** {state.status}",
        f"- **Branch:** `{state.branch}` (base `{state.base_branch}` @ `{state.base_sha[:10]}`)",
        f"- **Implementation loops:** {state.loops} / {cfg.get('pipeline.max_loops', 3)}",
        f"- **Created:** {state.created}",
        "",
        "## Stages",
        "",
        "| # | Stage | Status | Attempts | Retries | Time | Tokens in | Tokens out | Est. cost | Model | Gate |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
        *rows,
        f"| | **total** | | | {tot['retries']} | {_fmt_time(tot['time'])} | {tot['in']:,} | {tot['out']:,} | "
        f"${tot['cost']:.4f} | | |",
        "",
    ]
    notes = [(n, r.notes) for n, r in state.stages.items() if r.notes]
    if notes:
        lines += ["## Notes", ""] + [f"- **{n}:** {msg}" for n, ns in notes for msg in ns] + [""]
    requests = permissions.load(paths.run_dir)
    if requests:
        lines += ["## Guardrail approvals", "", "| Id | Stage | Decision | Action | Reason |", "|---|---|---|---|---|"]
        lines += [
            f"| {r['id']} | {r['stage']} | {r['status']} | `{r['subject'][:120]}` | {_cell(r['reason'])} |"
            for r in requests
        ]
        lines.append("")
    if state.history:
        lines += ["## Timeline", "", "| Time | Stage | Outcome | Detail |", "|---|---|---|---|"]
        lines += [
            f"| {h['ts']} | {h['stage']} | {h['outcome']} | {h['detail'].replace('|', '/')[:200]} |"
            for h in state.history
        ]
        lines.append("")
    if cfg.warnings:
        lines += ["## Config warnings", ""] + [f"- {w}" for w in cfg.warnings] + [""]
    lines.append("_Costs use provider-reported values when available, otherwise `models.yaml` pricing._")
    return "\n".join(lines) + "\n"


def write(cfg: Config, state: RunState, paths: RunPaths) -> Path:
    out = paths.report_file
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(cfg, state, paths))
    data = {
        "run_id": state.run_id,
        "status": state.status,
        "loops": state.loops,
        "stages": {k: asdict(v) for k, v in state.stages.items()},
    }
    (paths.run_dir / "report.json").write_text(json.dumps(data, indent=2) + "\n")
    return out


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")[:160]
