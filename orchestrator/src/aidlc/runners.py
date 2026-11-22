"""Stage runners. Each returns an Outcome; the orchestrator owns sequencing and loops."""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from aidlc import deps, gitutil, harness, models, prompts, report, scan, stack, traceability
from aidlc.adapters import AgentAdapter, AgentRequest, AgentResult
from aidlc.config import Config
from aidlc.harness import AuditLog, RunPaths
from aidlc.guardrails import _match as guardrails_match
from aidlc.state import RunState
from aidlc.util import glob_match, head, sha256_file, strip_fence, tail


class StageError(RuntimeError):
    pass


@dataclass
class Outcome:
    status: str  # passed|failed|route_back|skipped
    detail: str = ""
    feedback: list[str] = field(default_factory=list)
    gate: str | None = None


@dataclass
class StageContext:
    cfg: Config
    state: RunState
    paths: RunPaths
    adapters: dict[str, AgentAdapter]
    audit: AuditLog

    @property
    def workdir(self) -> Path:
        return self.paths.workdir

    @property
    def max_retries(self) -> int:
        return int(self.cfg.get("pipeline.max_retries", 2))

    def read(self, key: str) -> str:
        path = self.paths.artifact(key)
        return path.read_text() if path.exists() else ""

    def profile(self) -> dict:
        path = self.paths.artifact("stack_profile")
        if not path.exists():
            write_stack_profile(self)
        return json.loads(path.read_text())

    def id_pattern(self) -> str:
        return self.cfg.get("requirements.id_pattern", r"\b(?:REQ|AC|NFR)-\d+\b")

    def identity(self) -> tuple[str, str]:
        ident = self.cfg.get("identity", {}) or {}
        return ident.get("name", "aidlc-agent"), ident.get("email", "aidlc-agent@users.noreply.github.com")


# --- helpers ---------------------------------------------------------------------------------


def call_agent(ctx: StageContext, stage: str, prompt: str, writable: bool) -> AgentResult:
    choice = models.resolve(ctx.cfg, stage)
    adapter = ctx.adapters.get(choice.provider)
    if adapter is None or not adapter.available():
        raise StageError(
            f"provider '{choice.provider}' for stage '{stage}' is not available "
            f"(install its CLI or change models.yaml)"
        )
    req = AgentRequest(
        stage,
        prompt,
        choice.model,
        ctx.workdir,
        writable,
        env=harness.scoped_env(ctx.cfg, ctx.paths, stage),
        settings_file=harness.hook_settings(ctx.paths),
        timeout_s=int(ctx.cfg.get("pipeline.agent_timeout_s", 1800)),
    )
    (ctx.paths.run_dir / "prompts").mkdir(exist_ok=True)
    rec = ctx.state.stage(stage)
    (ctx.paths.run_dir / "prompts" / f"{stage}-{rec.attempts + 1}.md").write_text(prompt)
    ctx.audit.log("agent.start", stage=stage, provider=choice.provider, model=choice.model, writable=writable)
    result = adapter.run(req)
    cost = (
        result.cost_usd
        if result.cost_usd is not None
        else models.estimate_cost(ctx.cfg, choice.model, result.tokens_in, result.tokens_out)
    )
    rec.attempts += 1
    rec.tokens_in += result.tokens_in
    rec.tokens_out += result.tokens_out
    rec.cost_usd = round(rec.cost_usd + cost, 6)
    label = f"{choice.provider}:{choice.model}"
    if label not in rec.models:
        rec.models.append(label)
    ctx.audit.log(
        "agent.end",
        stage=stage,
        ok=result.ok,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost_usd=cost,
        error=result.error[:500],
    )
    if not result.ok:
        raise StageError(f"agent for '{stage}' failed: {result.error or 'no output'}")
    return result


def run_tests(ctx: StageContext) -> tuple[int, str]:
    cmd = ctx.profile().get("test_command")
    if not cmd:
        raise StageError("no test command detected; set stack.test_command in aidlc.yaml")
    ctx.audit.log("command.run", command=cmd)
    env = harness.scoped_env(ctx.cfg, ctx.paths, "tests")
    proc = subprocess.run(
        cmd,
        shell=True,
        cwd=ctx.workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=int(ctx.cfg.get("pipeline.test_timeout_s", 900)),
    )
    return proc.returncode, proc.stdout + proc.stderr


def is_test(path: str, globs: list[str]) -> bool:
    return glob_match(path, globs)


def discard(ctx: StageContext, paths: list[str]) -> None:
    tracked = set(gitutil.git(["ls-files"], ctx.workdir).splitlines())
    gitutil.restore(ctx.workdir, "HEAD", [p for p in paths if p in tracked])
    for p in paths:
        if p not in tracked and (ctx.workdir / p).is_file():
            (ctx.workdir / p).unlink()


def protected_changes(ctx: StageContext, changed: list[str]) -> list[str]:
    rules = ctx.cfg.guardrails.get("paths", {}).get("protected", [])
    return [p for p in changed if guardrails_match(p, rules)]


def section(md: str, title: str) -> str:
    m = re.search(rf"^##+\s*{re.escape(title)}\s*$(.*?)(?=^##\s|\Z)", md, flags=re.M | re.S | re.I)
    return m.group(1).strip() if m else ""


def bullets(text: str) -> list[str]:
    items = [ln.strip()[2:].strip() for ln in text.splitlines() if ln.strip().startswith(("- ", "* "))]
    return [i for i in items if i and i.lower().rstrip(".") not in {"none", "n/a"}]


def verdict(md: str, options: tuple[str, ...]) -> str | None:
    found = re.findall(rf"VERDICT:\s*\**\s*({'|'.join(options)})\b", md)
    return found[-1] if found else None


def repo_overview(ctx: StageContext, limit: int = 300) -> str:
    files = [f for f in gitutil.git(["ls-files"], ctx.workdir).splitlines() if not f.startswith(".aidlc/")]
    more = f"\n... and {len(files) - limit} more" if len(files) > limit else ""
    return "```text\n" + "\n".join(files[:limit]) + more + "\n```"


def file_block(ctx: StageContext, rels: list[str], max_chars: int = 20000) -> str:
    out = []
    for rel in rels:
        path = ctx.workdir / rel
        if path.is_file():
            out.append(f"### {rel}\n```\n{head(path.read_text(errors='ignore'), 6000)}\n```")
    return head("\n\n".join(out), max_chars)


def with_retries(ctx: StageContext, stage: str, attempt_fn: Callable[[list[str]], Outcome]) -> Outcome:
    feedback: list[str] = []
    outcome = Outcome("failed", "not attempted")
    for _ in range(ctx.max_retries + 1):
        outcome = attempt_fn(feedback)
        if outcome.status != "retry":
            return outcome
        feedback = outcome.feedback
        ctx.state.stage(stage).notes.append("retry: " + "; ".join(feedback)[:300])
    return Outcome("failed", f"{stage} failed after {ctx.max_retries + 1} attempts: {outcome.detail}", outcome.feedback)


# --- stage 0 ---------------------------------------------------------------------------------


def write_stack_profile(ctx: StageContext) -> dict:
    profile = stack.detect(ctx.workdir, ctx.cfg.get("stack", {}) or {})
    ctx.paths.artifact("stack_profile").write_text(json.dumps(profile, indent=2) + "\n")
    return profile


def run_intake(ctx: StageContext) -> Outcome:
    if not ctx.paths.artifact("requirement").exists():
        return Outcome("failed", "no requirement captured; start a run with --file or --ticket")
    profile = write_stack_profile(ctx)
    langs = ", ".join(profile["languages"]) or "unknown"
    return Outcome("passed", f"stack: {langs}; tests: {profile['test_command']}", gate="ok")


# --- stage 1 ---------------------------------------------------------------------------------


def run_analyze(ctx: StageContext) -> Outcome:
    def attempt(feedback: list[str]) -> Outcome:
        prompt = prompts.build(
            ctx.cfg,
            "analyze",
            [
                ("Requirement", ctx.read("requirement")),
                ("Stack profile", f"```json\n{ctx.read('stack_profile')}```"),
                ("Repository files", repo_overview(ctx)),
            ],
            feedback,
        )
        spec = strip_fence(call_agent(ctx, "analyze", prompt, writable=False).text)
        ids = traceability.requirement_ids(spec, ctx.id_pattern())
        if not ids:
            return Outcome(
                "retry",
                "spec has no requirement IDs",
                ["Every requirement and acceptance criterion needs an ID like REQ-1 / AC-1."],
            )
        ctx.paths.artifact("spec").write_text(spec)
        questions = bullets(section(spec, "Open Questions"))
        note = f"{len(ids)} IDs; {len(questions)} open question(s)"
        return Outcome("passed", note, gate="open-questions" if questions else "ok")

    return with_retries(ctx, "analyze", attempt)


# --- stage 2 ---------------------------------------------------------------------------------


def run_plan(ctx: StageContext) -> Outcome:
    profile = ctx.profile()

    def attempt(feedback: list[str]) -> Outcome:
        prompt = prompts.build(
            ctx.cfg,
            "plan",
            [
                ("Specification", ctx.read("spec")),
                ("Stack profile", f"```json\n{json.dumps(profile, indent=2)}\n```"),
                ("Repository files", repo_overview(ctx)),
            ],
            feedback,
        )
        plan = strip_fence(call_agent(ctx, "plan", prompt, writable=False).text)
        try:
            proposals = deps.parse_proposals(plan)
        except ValueError as exc:
            return Outcome("retry", str(exc), [f"Fix the aidlc-dependencies block: {exc}"])
        offline = bool(ctx.cfg.get("dependencies.offline", False))
        results = deps.evaluate(proposals, ctx.cfg.guardrails, profile.get("ecosystems", []), offline=offline)
        ctx.paths.artifact("plan").write_text(plan + "\n## Dependency gate (generated)\n\n" + deps.render(results))
        ctx.paths.artifact("dependencies").write_text(json.dumps({"dependencies": results}, indent=2) + "\n")
        rejected = [r for r in results if r["status"] == "rejected"]
        if rejected:
            return Outcome(
                "retry",
                "dependencies rejected",
                [
                    f"Dependency {r['name']} rejected ({'; '.join(r['notes'])}); "
                    "drop it or choose an allowed alternative."
                    for r in rejected
                ],
            )
        review = [r["name"] for r in results if r["status"] == "needs_review"]
        detail = f"{len(results)} dependency proposal(s)" + (f"; needs review: {', '.join(review)}" if review else "")
        return Outcome("passed", detail, gate="deps-need-review" if review else "ok")

    return with_retries(ctx, "plan", attempt)


# --- stage 3 ---------------------------------------------------------------------------------


def run_test(ctx: StageContext) -> Outcome:
    profile = ctx.profile()
    globs = profile.get("test_globs", [])
    ids = traceability.requirement_ids(ctx.read("spec"), ctx.id_pattern())
    baseline_red = False
    if not ctx.paths.artifact("test_lock").exists():
        base_code, _ = run_tests(ctx)
        baseline_red = base_code not in (0, 5)  # 5 = pytest "no tests collected"

    def attempt(feedback: list[str]) -> Outcome:
        prompt = prompts.build(
            ctx.cfg,
            "test",
            [
                ("Specification", ctx.read("spec")),
                ("Plan", ctx.read("plan")),
                (
                    "Test conventions",
                    f"- Test command: `{profile.get('test_command')}`\n" f"- Test files must match: {', '.join(globs)}",
                ),
            ],
            feedback,
        )
        call_agent(ctx, "test", prompt, writable=True)
        changed = gitutil.dirty_paths(ctx.workdir)
        stray = [p for p in changed if not is_test(p, globs)]
        if stray:
            discard(ctx, stray)
            changed = [p for p in changed if p not in stray]
        if not changed:
            return Outcome(
                "retry",
                "no test files written",
                [f"Write failing tests in files matching {globs}. Discarded non-test edits: {stray}"],
            )
        refs = traceability.references(ctx.workdir, changed, ctx.id_pattern())
        covered = {i for s in refs.values() for i in s}
        if ids and not covered:
            discard(ctx, changed)
            return Outcome(
                "retry",
                "tests do not reference requirement IDs",
                [f"Reference the acceptance criterion ID (e.g. {ids[0]}) in each test name or comment."],
            )
        code, output = run_tests(ctx)
        if code == 0:
            discard(ctx, changed)
            return Outcome(
                "retry",
                "tests passed before implementation",
                [
                    "The new tests pass already; they must fail until the feature is implemented.",
                    f"Test output:\n{tail(output, 1500)}",
                ],
            )
        name, email = ctx.identity()
        sha = gitutil.commit_all(ctx.workdir, f"test({ctx.state.req_id}): add failing tests [aidlc]", name, email)
        lock = {
            "commit": sha,
            "red_exit_code": code,
            "test_command": profile.get("test_command"),
            "files": {p: sha256_file(ctx.workdir / p) for p in sorted(changed) if (ctx.workdir / p).is_file()},
        }
        ctx.paths.artifact("test_lock").write_text(json.dumps(lock, indent=2) + "\n")
        missing = [i for i in ids if i not in covered]
        detail = f"{len(lock['files'])} test file(s) locked; red exit {code}"
        if missing:
            ctx.state.stage("test").notes.append(f"IDs without tests: {', '.join(missing)}")
        if baseline_red:
            note = "suite already failed before new tests (deps not installed, empty suite, or broken tests)"
            return Outcome("passed", f"{detail}; red gate unverified: {note}", gate="red-unverified")
        return Outcome("passed", detail, gate="red")

    return with_retries(ctx, "test", attempt)


# --- stage 4 ---------------------------------------------------------------------------------


def lock_violations(ctx: StageContext) -> list[str]:
    path = ctx.paths.artifact("test_lock")
    if not path.exists():
        return []
    files = json.loads(path.read_text())["files"]
    return [
        p for p, digest in files.items() if not (ctx.workdir / p).is_file() or sha256_file(ctx.workdir / p) != digest
    ]


def run_implement(ctx: StageContext) -> Outcome:
    lock = json.loads(ctx.read("test_lock") or "{}")
    approved = [
        d["name"]
        for d in json.loads(ctx.read("dependencies") or "{}").get("dependencies", [])
        if d.get("status") == "approved"
    ]
    carried = list(ctx.state.feedback)

    def attempt(feedback: list[str]) -> Outcome:
        prompt = prompts.build(
            ctx.cfg,
            "implement",
            [
                ("Specification", ctx.read("spec")),
                ("Plan", ctx.read("plan")),
                ("Locked tests (read-only; make them pass)", file_block(ctx, list(lock.get("files", {})))),
                (
                    "Environment",
                    f"- Test command: `{ctx.profile().get('test_command')}`\n"
                    f"- Approved new dependencies: {', '.join(approved) or 'none'}",
                ),
            ],
            carried + feedback,
        )
        call_agent(ctx, "implement", prompt, writable=True)
        notes = []
        violated = lock_violations(ctx)
        if violated:
            gitutil.restore(ctx.workdir, lock["commit"], violated)
            notes.append(f"Locked tests were modified and have been restored: {violated}. Do not edit them.")
        prot = protected_changes(ctx, gitutil.dirty_paths(ctx.workdir))
        if prot:
            discard(ctx, prot)
            notes.append(f"Edits to protected paths were discarded: {prot}.")
        code, output = run_tests(ctx)
        if code != 0:
            return Outcome(
                "retry",
                f"tests failing (exit {code})",
                [*notes, f"Tests still fail. Output:\n{tail(output, 3000)}"],
            )
        name, email = ctx.identity()
        loop = ctx.state.loops
        msg = f"feat({ctx.state.req_id}): implement{f' (loop {loop})' if loop else ''} [aidlc]"
        sha = gitutil.commit_all(ctx.workdir, msg, name, email) or gitutil.head_sha(ctx.workdir)
        ctx.paths.artifact("implementation").write_text(json.dumps({"commit": sha, "loop": loop}, indent=2) + "\n")
        ctx.state.feedback = []
        return Outcome(
            "passed",
            f"tests green at {sha[:10]}" + (f"; {len(notes)} guardrail correction(s)" if notes else ""),
            gate="green",
        )

    return with_retries(ctx, "implement", attempt)


# --- stage 5 ---------------------------------------------------------------------------------


def run_verify(ctx: StageContext) -> Outcome:
    profile = ctx.profile()
    globs = profile.get("test_globs", [])
    spec = ctx.read("spec")
    ids = traceability.requirement_ids(spec, ctx.id_pattern())
    changed = gitutil.changed_since(ctx.workdir, ctx.state.base_sha)
    tests = sorted(
        set(json.loads(ctx.read("test_lock") or "{}").get("files", {})) | {p for p in changed if is_test(p, globs)}
    )
    code = [p for p in changed if not is_test(p, globs)]
    matrix = traceability.build_matrix(
        ids,
        traceability.references(ctx.workdir, tests, ctx.id_pattern()),
        traceability.references(ctx.workdir, code, ctx.id_pattern()),
    )
    table = traceability.render(matrix)
    (ctx.paths.run_dir / "traceability.json").write_text(json.dumps(matrix, indent=2) + "\n")

    def attempt(feedback: list[str]) -> Outcome:
        prompt = prompts.build(
            ctx.cfg,
            "verify",
            [
                ("Specification", spec),
                ("Traceability matrix (generated)", table),
                ("Changed files", "\n".join(f"- {p}" for p in changed)),
                ("Diff against base", f"```diff\n{gitutil.diff(ctx.workdir, ctx.state.base_sha)}\n```"),
                ("Plan", ctx.read("plan")),
            ],
            feedback,
        )
        text = strip_fence(call_agent(ctx, "verify", prompt, writable=False).text)
        result = verdict(text, ("PASS", "GAPS"))
        if result is None:
            return Outcome("retry", "no verdict", ["End with a line `VERDICT: PASS` or `VERDICT: GAPS`."])
        ctx.paths.artifact("verification").write_text(text + "\n## Traceability matrix (generated)\n\n" + table)
        if matrix["untested"]:
            ctx.state.stage("verify").notes.append(f"untested IDs: {', '.join(matrix['untested'])}")
        if result == "GAPS":
            gaps = bullets(section(text, "Gaps")) or ["Verification reported gaps; see verification.md"]
            return Outcome("route_back", f"{len(gaps)} gap(s)", gaps, gate="gaps")
        return Outcome("passed", f"{len(ids)} IDs traced; {len(matrix['untested'])} untested", gate="pass")

    return with_retries(ctx, "verify", attempt)


# --- stage 6 ---------------------------------------------------------------------------------


def run_review(ctx: StageContext) -> Outcome:
    def attempt(feedback: list[str]) -> Outcome:
        prompt = prompts.build(
            ctx.cfg,
            "review",
            [
                ("Specification", ctx.read("spec")),
                ("Verification", ctx.read("verification")),
                ("Plan", ctx.read("plan")),
                ("Diff against base", f"```diff\n{gitutil.diff(ctx.workdir, ctx.state.base_sha)}\n```"),
            ],
            feedback,
        )
        text = strip_fence(call_agent(ctx, "review", prompt, writable=False).text)
        result = verdict(text, ("APPROVE", "REQUEST_CHANGES"))
        if result is None:
            return Outcome("retry", "no verdict", ["End with `VERDICT: APPROVE` or `VERDICT: REQUEST_CHANGES`."])
        ctx.paths.artifact("review").write_text(text)
        if result == "REQUEST_CHANGES":
            blockers = bullets(section(text, "Blockers")) or ["Reviewer requested changes; see review.md"]
            return Outcome("route_back", f"{len(blockers)} blocker(s)", blockers, gate="changes-requested")
        return Outcome("passed", "approved", gate="approved")

    return with_retries(ctx, "review", attempt)


# --- stage 7 ---------------------------------------------------------------------------------


def run_scan(ctx: StageContext) -> Outcome:
    gate_cfg = ctx.cfg.get("gates.static", {}) or {}
    result = scan.gate(
        ctx.paths.root,
        ctx.workdir,
        ctx.state.base_sha,
        ctx.profile(),
        gate_cfg,
        ctx.read("plan"),
        ctx.paths.root / ".aidlc" / "cache",
    )
    ctx.paths.artifact("scan").write_text(json.dumps(result, indent=2) + "\n")
    (ctx.paths.run_dir / "scan.md").write_text(scan.render(result))
    detail = f"{len(result['new'])} new, {len(result['blocking'])} blocking"
    if not result["passed"]:
        fb = [f"[{f['severity']}] {f['category']} {f['file']}: {f['message']}" for f in result["blocking"]]
        return Outcome("route_back", detail, fb, gate="blocked")
    return Outcome("passed", detail, gate="pass")


# --- stage 8 ---------------------------------------------------------------------------------


def pr_body(ctx: StageContext) -> str:
    spec, ver, rev = ctx.read("spec"), ctx.read("verification"), ctx.read("review")
    scan_md = (ctx.paths.run_dir / "scan.md").read_text() if (ctx.paths.run_dir / "scan.md").exists() else ""
    parts = [f"## {ctx.state.req_id}: {ctx.state.title}", "", f"Source: `{ctx.state.source}`", ""]
    summary = section(spec, "Summary")
    if summary:
        parts += ["### Summary", summary, ""]
    matrix = section(ver, "Traceability matrix (generated)")
    if matrix:
        parts += ["### Traceability", matrix, ""]
    parts += [
        "### Gates",
        f"- Verification: {verdict(ver, ('PASS', 'GAPS')) or 'not run'}",
        f"- Review: {verdict(rev, ('APPROVE', 'REQUEST_CHANGES')) or 'not run'}",
        f"- Static analysis: {scan_md.splitlines()[0] if scan_md else 'not run'}",
        "",
        f"_Generated by AI-DLC run `{ctx.state.run_id}`. Full report: `.aidlc/reports/{ctx.state.run_id}.md`._",
    ]
    return "\n".join(parts) + "\n"


def _gh(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=300)


def run_pr(ctx: StageContext) -> Outcome:
    body_file = ctx.paths.run_dir / "pr-body.md"
    body_file.write_text(pr_body(ctx))
    pr_cfg = ctx.cfg.get("pr", {}) or {}
    if not pr_cfg.get("enabled", True):
        return Outcome("skipped", f"PR disabled; body written to {body_file.name}")
    remotes = gitutil.git(["remote"], ctx.workdir, check=False).split()
    if "origin" not in remotes or not shutil.which("gh"):
        return Outcome("skipped", f"no origin remote or gh CLI; body written to {body_file.name}")
    branch = ctx.state.branch
    push = subprocess.run(["git", "push", "-u", "origin", branch], cwd=ctx.workdir, capture_output=True, text=True)
    ctx.audit.log("command.run", command=f"git push -u origin {branch}", exit=push.returncode)
    if push.returncode != 0:
        return Outcome("failed", f"push failed: {push.stderr.strip()[-500:]}")
    existing = json.loads(ctx.read("pr") or "{}")
    if not existing.get("url"):
        args = [
            "pr",
            "create",
            "--base",
            ctx.state.base_branch,
            "--head",
            branch,
            "--title",
            f"{ctx.state.req_id}: {ctx.state.title}",
            "--body-file",
            str(body_file),
        ]
        if pr_cfg.get("draft", False):
            args.append("--draft")
        proc = _gh(args, ctx.workdir)
        if proc.returncode != 0:
            return Outcome("failed", f"gh pr create failed: {proc.stderr.strip()[-500:]}")
        existing = {"url": proc.stdout.strip().splitlines()[-1]}
    else:
        _gh(["pr", "edit", existing["url"], "--body-file", str(body_file)], ctx.workdir)
    ctx.paths.artifact("pr").write_text(json.dumps(existing, indent=2) + "\n")
    wait = int(pr_cfg.get("wait_for_checks_s", 0))
    if wait <= 0:
        return Outcome("passed", existing["url"], gate="opened")
    failed = wait_for_checks(ctx, existing["url"], wait, int(pr_cfg.get("poll_interval_s", 30)))
    if failed is None:
        return Outcome("passed", f"{existing['url']} (checks still pending)", gate="pending")
    if not failed:
        return Outcome("passed", f"{existing['url']} (checks green)", gate="green")
    logs = "\n\n".join(failed_logs(ctx, c) for c in failed)
    prompt = prompts.build(
        ctx.cfg,
        "pr",
        [
            ("Failing checks", "\n".join(f"- {c['name']}" for c in failed)),
            ("Failure logs", f"```\n{tail(logs, 20000)}\n```"),
        ],
    )
    triage = call_agent(ctx, "pr", prompt, writable=False).text
    return Outcome(
        "route_back",
        f"{len(failed)} failing check(s)",
        bullets(triage) or [triage.strip()[:2000]],
        gate="checks-failed",
    )


def wait_for_checks(ctx: StageContext, url: str, timeout_s: int, interval_s: int) -> list[dict] | None:
    deadline = time.monotonic() + timeout_s
    while True:
        proc = _gh(["pr", "checks", url, "--json", "name,bucket,link"], ctx.workdir)
        checks = json.loads(proc.stdout or "[]") if proc.stdout.strip().startswith("[") else []
        if checks and all(c["bucket"] != "pending" for c in checks):
            return [c for c in checks if c["bucket"] == "fail"]
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval_s)


def failed_logs(ctx: StageContext, check: dict) -> str:
    m = re.search(r"/actions/runs/(\d+)", check.get("link", ""))
    if not m:
        return f"{check['name']}: no logs available"
    proc = _gh(["run", "view", m.group(1), "--log-failed"], ctx.workdir)
    return f"### {check['name']}\n{tail(proc.stdout, 8000)}"


# --- stage 9 ---------------------------------------------------------------------------------


def run_report(ctx: StageContext) -> Outcome:
    path = report.write(ctx.cfg, ctx.state, ctx.paths)
    return Outcome("passed", str(path.relative_to(ctx.paths.root)), gate="written")


RUNNERS: dict[str, Callable[[StageContext], Outcome]] = {
    "intake": run_intake,
    "analyze": run_analyze,
    "plan": run_plan,
    "test": run_test,
    "implement": run_implement,
    "verify": run_verify,
    "review": run_review,
    "scan": run_scan,
    "pr": run_pr,
    "report": run_report,
}
