"""Persistent run state (.aidlc/runs/<run-id>/state.json)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from aidlc.util import file_lock, now_iso

STATE_FILE = "state.json"
STATE_SCHEMA = 2  # bump with a migration in RunState.load when the layout changes


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
    started: str | None = None
    ended: str | None = None


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
    owned_paths: dict[str, str] = field(default_factory=dict)
    rating: int | None = None  # developer's 1-5 rating of the run
    schema: int = STATE_SCHEMA

    def stage(self, name: str) -> StageRecord:
        return self.stages.setdefault(name, StageRecord())

    def event(self, stage: str, outcome: str, detail: str = "") -> None:
        self.history.append({"ts": now_iso(), "stage": stage, "outcome": outcome, "detail": detail})

    def save(self, run_dir: Path) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(run_dir / STATE_FILE, json.dumps(asdict(self), indent=2) + "\n")

    @classmethod
    def load(cls, run_dir: Path) -> RunState:
        data = json.loads((run_dir / STATE_FILE).read_text(encoding="utf-8"))
        if data.get("schema", 1) > STATE_SCHEMA:
            raise SystemExit(f"aidlc: {run_dir.name} was written by a newer aidlc (state schema {data['schema']})")
        data["schema"] = STATE_SCHEMA
        known = {f.name for f in fields(StageRecord)}
        for rec in data.get("stages", {}).values():
            for key in set(rec) - known:
                rec.pop(key)
        data["stages"] = {k: StageRecord(**v) for k, v in data.get("stages", {}).items()}
        return cls(**data)


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def run_lock(run_dir: Path) -> Iterator[None]:
    """Only one process may execute a given run at a time."""
    with file_lock(run_dir / "run.lock", blocking=False) as acquired:
        if not acquired:
            raise SystemExit(f"aidlc: run {run_dir.name} is already executing in another process")
        yield
