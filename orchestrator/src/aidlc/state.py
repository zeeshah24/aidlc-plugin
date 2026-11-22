"""Persistent run state (.aidlc/runs/<run-id>/state.json)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from aidlc.util import now_iso

STATE_FILE = "state.json"


@dataclass
class StageRecord:
    status: str = "pending"  # pending|running|passed|failed|skipped|awaiting_approval
    attempts: int = 0
    duration_s: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    models: list[str] = field(default_factory=list)
    gate: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class RunState:
    run_id: str
    req_id: str
    title: str
    source: str
    base_branch: str
    base_sha: str
    branch: str
    workdir: str
    created: str = field(default_factory=now_iso)
    status: str = "created"
    stages: dict[str, StageRecord] = field(default_factory=dict)
    approvals: list[str] = field(default_factory=list)
    loops: int = 0
    feedback: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    owned_paths: dict[str, str] = field(default_factory=dict)  # path -> content hash left by this run's agents

    def stage(self, name: str) -> StageRecord:
        return self.stages.setdefault(name, StageRecord())

    def event(self, stage: str, outcome: str, detail: str = "") -> None:
        self.history.append({"ts": now_iso(), "stage": stage, "outcome": outcome, "detail": detail})

    def save(self, run_dir: Path) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / STATE_FILE).write_text(json.dumps(asdict(self), indent=2) + "\n")

    @classmethod
    def load(cls, run_dir: Path) -> RunState:
        data = json.loads((run_dir / STATE_FILE).read_text())
        data["stages"] = {k: StageRecord(**v) for k, v in data.get("stages", {}).items()}
        return cls(**data)
