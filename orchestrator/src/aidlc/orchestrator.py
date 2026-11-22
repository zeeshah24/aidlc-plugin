"""Deterministic orchestrator: owns the state machine, loops, retries, human gates and metrics."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from aidlc import gitutil, harness, intake, models, permissions, report
from aidlc.adapters import AgentAdapter, default_adapters
from aidlc.config import Config
from aidlc.harness import AuditLog, RunPaths
from aidlc.runners import RUNNERS, Outcome, StageContext, StageError
from aidlc.stages import LOOP_BACK, Stage
from aidlc.state import RunState
from aidlc.util import sha256_file, slugify, stamp

# Artifacts that are cheap and deterministic to regenerate (graceful degradation).
DERIVABLE = {"stack_profile"}


@dataclass
class RunOptions:
    yes: bool = False
    headless: bool = False
    in_place: bool = False
    approve: list[str] = field(default_factory=list)


class PipelineError(RuntimeError):
    pass


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        adapters: dict[str, AgentAdapter] | None = None,
        options: RunOptions | None = None,
        ask: Callable[[str], str] | None = None,
        out: Callable[[str], None] = print,
    ):
        self.cfg = cfg
        self.adapters = adapters if adapters is not None else default_adapters()
        self.options = options or RunOptions()
        self.ask = ask
        self.out = out
        errors = models.validate(cfg)
        if errors:
            raise PipelineError("; ".join(errors))

    # --- run lifecycle -------------------------------------------------------------------------

    @property
    def runs_dir(self) -> Path:
        return self.cfg.aidlc_dir / "runs"

    def current_run_id(self) -> str | None:
        marker = self.cfg.aidlc_dir / "current"
        return marker.read_text().strip() if marker.exists() else None

    def _base(self) -> tuple[str, str]:
        root = self.cfg.root
        base_branch = self.cfg.get("project.base_branch") or gitutil.current_branch(root)
        sha = gitutil.rev(root, base_branch) or gitutil.rev(root, f"origin/{base_branch}") or gitutil.head_sha(root)
        return base_branch, sha

    def start(self, req: intake.Requirement) -> tuple[RunState, RunPaths]:
        root = self.cfg.root
        run_id = f"{slugify(req.req_id)}-{stamp()}"
        base_branch, base_sha = self._base()
        use_worktree = self.cfg.get("pipeline.worktree", True) and not self.options.in_place
        if use_worktree:
            branch = f"{self.cfg.get('pipeline.branch_prefix', 'aidlc/')}{slugify(req.req_id)}-{run_id[-6:]}"
            workdir = harness.create_worktree(root, run_id, branch, base_sha)
        else:
            branch, workdir = gitutil.current_branch(root), root
            base_sha = gitutil.git(["merge-base", base_sha, "HEAD"], root).strip()
        state = RunState(run_id, req.req_id, req.title, req.source, base_branch, base_sha, branch, str(workdir))
        paths = RunPaths(root, self.runs_dir / run_id, workdir)
        paths.run_dir.mkdir(parents=True, exist_ok=True)
        intake.write_requirement(req, self.cfg.aidlc_dir)
        paths.artifact("requirement").write_text(req.body)
        state.save(paths.run_dir)
        (self.cfg.aidlc_dir / "current").write_text(run_id)
        AuditLog(paths.audit_file).log(
            "run.start", run_id=run_id, source=req.source, branch=branch, worktree=use_worktree
        )
        return state, paths

    def start_adhoc(self) -> tuple[RunState, RunPaths]:
        """Ad-hoc run on the current checkout, for standalone stages without a requirement."""
        root = self.cfg.root
        base_branch, base_sha = self._base()
        base_sha = gitutil.git(["merge-base", base_sha, "HEAD"], root).strip()
        run_id = f"adhoc-{stamp()}"
        branch = gitutil.current_branch(root)
        state = RunState(run_id, "adhoc", f"Ad-hoc run on {branch}", "adhoc", base_branch, base_sha, branch, str(root))
        paths = RunPaths(root, self.runs_dir / run_id, root)
        state.save(paths.run_dir)
        (self.cfg.aidlc_dir / "current").write_text(run_id)
        return state, paths

    def open(self, run_id: str) -> tuple[RunState, RunPaths]:
        run_dir = self.runs_dir / run_id
        if not (run_dir / "state.json").exists():
            raise PipelineError(f"no run '{run_id}' under {self.runs_dir}")
        state = RunState.load(run_dir)
        return state, RunPaths(self.cfg.root, run_dir, Path(state.workdir))

    # --- gates ---------------------------------------------------------------------------------

    def _interactive(self) -> bool:
        return self.ask is not None and not self.options.headless

    def approve_gate(self, stage: str, state: RunState, paths: RunPaths) -> bool:
        explicit = stage in self.options.approve
        if not explicit and self._interactive():
            artifact = {"analyze": "spec", "plan": "plan"}.get(stage)
            where = f" Review {paths.artifact(artifact)}." if artifact else ""
            answer = self.ask(f"Human gate after '{stage}'.{where} Approve? [y/N] ").strip().lower()
            explicit = answer in {"y", "yes"}
            if not explicit:
                return False
        if not explicit and not self.options.yes:
            return False
        record_approval(state, paths, stage, explicit=explicit)
        return True

    def decide_permissions(self, waiting: list[dict], paths: RunPaths) -> bool:
        allowed = False
        for w in waiting:
            answer = self.ask(
                f"Guardrail approval {w['id']} ({w['stage']}): {w['reason']}\n  {w['subject']}\n"
                "  Allow [o]nce, for the [r]un, or [N]o? "
            )
            choice = {"o": "once", "once": "once", "r": "run", "run": "run"}.get(answer.strip().lower(), "deny")
            permissions.decide(paths.run_dir, w["id"], choice)
            allowed = allowed or choice != "deny"
        return allowed

    # --- execution -----------------------------------------------------------------------------

    def present(self, key: str, state: RunState, paths: RunPaths) -> bool:
        if key == "implementation":
            return bool(gitutil.changed_since(paths.workdir, state.base_sha))
        return paths.artifact(key).exists()

    def dirty_hashes(self) -> dict[str, str]:
        root = self.cfg.root
        return {p: sha256_file(root / p) if (root / p).is_file() else "-" for p in sorted(gitutil.dirty_paths(root))}

    def execute(self, state: RunState, paths: RunPaths, stages: list[Stage], skip_passed: bool = False) -> str:
        audit = AuditLog(paths.audit_file)
        ctx = StageContext(self.cfg, state, paths, self.adapters, audit, interactive=not self.options.headless)
        names = [s.name for s in stages]
        by_name = {s.name: s for s in stages}
        gates = set(self.cfg.get("pipeline.human_gates", []) or [])
        max_loops = int(self.cfg.get("pipeline.max_loops", 3))
        in_place = Path(state.workdir).resolve() == self.cfg.root.resolve()
        state.status = "running"
        i = 0
        try:
            while i < len(names):
                name = names[i]
                stage, rec = by_name[name], state.stage(name)
                if skip_passed and rec.status in ("passed", "skipped"):
                    i += 1
                    continue
                if rec.status == "awaiting_approval" and name in state.approvals:
                    rec.status = "passed"
                    i += 1
                    continue
                missing = [a for a in stage.requires if a not in DERIVABLE and not self.present(a, state, paths)]
                if missing:
                    raise PipelineError(
                        f"stage '{name}' is missing prerequisites: {', '.join(missing)} "
                        f"(run the producing stage first, e.g. --from {name})"
                    )
                writes = in_place and name in ("test", "implement")
                foreign = [p for p, h in self.dirty_hashes().items() if state.owned_paths.get(p) != h] if writes else []
                if foreign:
                    raise PipelineError(
                        f"stage '{name}' would discard uncommitted changes in this checkout "
                        f"({', '.join(foreign[:5])}); commit or stash them first"
                    )
                self.out(f"[{stage.index}] {stage.title} ...")
                rec.status = "running"
                state.save(paths.run_dir)
                audit.log("stage.start", stage=name)
                t0 = time.monotonic()
                try:
                    outcome = RUNNERS[name](ctx)
                except StageError as exc:
                    outcome = Outcome("failed", str(exc))
                finally:
                    if writes:  # also on Ctrl+C, so a resume recognises the agent's own edits
                        state.owned_paths = self.dirty_hashes()
                rec.duration_s = round(rec.duration_s + time.monotonic() - t0, 3)
                rec.attempts = max(rec.attempts, 1)
                rec.gate = outcome.gate or rec.gate
                state.event(name, outcome.status, outcome.detail)
                audit.log("stage.end", stage=name, outcome=outcome.status, detail=outcome.detail[:500])
                self.out(f"    {outcome.status}: {outcome.detail}")

                waiting = permissions.pending(paths.run_dir, name)
                if waiting and outcome.status == "passed":
                    permissions.expire(paths.run_dir, name)
                elif waiting:
                    if not self._interactive():
                        rec.status = state.status = "awaiting_permission"
                        ids = " ".join(w["id"] for w in waiting)
                        self.out(
                            f"    awaiting developer approval: `aidlc allow {ids}` (or `aidlc deny {ids}`), "
                            "then `aidlc run --resume`"
                        )
                        return state.status
                    if self.decide_permissions(waiting, paths):
                        rec.status = "pending"
                        self.out(f"    re-running {name} with the developer's allowances")
                        continue

                if outcome.status in ("passed", "skipped"):
                    rec.status = outcome.status
                    if name in gates and name in state.approvals and name not in self.options.approve:
                        state.approvals.remove(name)  # a regenerated artifact needs a fresh approval
                    gated = outcome.status == "passed" and name in gates and name not in state.approvals
                    if gated and not self.approve_gate(name, state, paths):
                        rec.status = "awaiting_approval"
                        state.status = "awaiting_approval"
                        self.out(f"    awaiting approval: run `aidlc approve {name}` then `aidlc run --resume`")
                        return state.status
                    i += 1
                    continue

                rec.status = "failed"
                if (
                    outcome.status == "route_back"
                    and name in LOOP_BACK
                    and "implement" in names
                    and names.index("implement") < i
                ):
                    if state.loops >= max_loops:
                        state.status = "failed"
                        state.feedback = outcome.feedback
                        self.out(f"    loop limit {max_loops} reached; stopping (fail-fast)")
                        return state.status
                    state.loops += 1
                    state.feedback = outcome.feedback
                    back = names.index("implement")
                    for later in names[back:]:
                        state.stage(later).status = "pending"
                    self.out(f"    routing back to implement (loop {state.loops}/{max_loops})")
                    i = back
                    continue
                state.feedback = outcome.feedback
                state.status = "changes_requested" if outcome.status == "route_back" else "failed"
                return state.status
            state.status = "completed"
            return state.status
        finally:
            state.save(paths.run_dir)
            report.write(self.cfg, state, paths)
            state.save(paths.run_dir)


def record_approval(state: RunState, paths: RunPaths, stage: str, explicit: bool) -> None:
    if stage not in state.approvals:
        state.approvals.append(stage)
    if stage == "plan" and explicit and paths.artifact("dependencies").exists():
        data = json.loads(paths.artifact("dependencies").read_text())
        for dep in data.get("dependencies", []):
            if dep["status"] == "needs_review":
                dep["status"] = "approved"
                dep["notes"].append("approved by human at plan gate")
        paths.artifact("dependencies").write_text(json.dumps(data, indent=2) + "\n")
    state.event(stage, "approved", "explicit" if explicit else "auto (--yes)")
    AuditLog(paths.audit_file).log("gate.approved", stage=stage, explicit=explicit)


def interactive_ask() -> Callable[[str], str] | None:
    return input if sys.stdin.isatty() else None
