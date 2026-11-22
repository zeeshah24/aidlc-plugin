"""`aidlc` command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from aidlc import __version__, gitutil, intake, models, stack, stages
from aidlc.config import AIDLC_DIR, PROJECT_FILE, load_config
from aidlc.orchestrator import Pipeline, PipelineError, RunOptions, interactive_ask, record_approval
from aidlc.resources import template

EXIT = {"completed": 0, "awaiting_approval": 3, "awaiting_permission": 3, "changes_requested": 1, "failed": 1}
ORG_FILES = ("guardrails.yaml", "models.yaml", PROJECT_FILE, "conventions.md")


def project_root() -> Path:
    cwd = Path.cwd()
    return gitutil.toplevel(cwd) if gitutil.is_repo(cwd) else cwd


def cmd_init(args: argparse.Namespace) -> int:
    root = project_root()
    aidlc_dir = root / AIDLC_DIR
    for sub in ("requirements", "runs", "reports"):
        (aidlc_dir / sub).mkdir(parents=True, exist_ok=True)
    (aidlc_dir / ".gitignore").write_text("worktrees/\ncache/\ncurrent\nruns/*/prompts/\n")
    conv = aidlc_dir / "conventions.md"
    if not conv.exists():
        conv.write_text(template("conventions.md"))
    cfg_file = root / PROJECT_FILE
    if not cfg_file.exists() or args.force:
        cfg_file.write_text(template(PROJECT_FILE))
        print(f"wrote {cfg_file.relative_to(root)}")
    if args.org:
        copied = pull_org(args.org, aidlc_dir / "org")
        print(f"pulled org standards from {args.org}: {', '.join(copied) or 'nothing found'}")
    profile = stack.detect(root)
    print(f"detected stack: {', '.join(profile['languages']) or 'unknown'}; tests: {profile['test_command']}")
    print(f"initialized {aidlc_dir.relative_to(root)}/")
    return 0


def pull_org(source: str, dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    src = Path(source).expanduser()
    with tempfile.TemporaryDirectory() as tmp:
        if not src.is_dir():
            subprocess.run(["git", "clone", "-q", "--depth", "1", source, tmp], check=True)
            src = Path(tmp)
        copied = []
        for name in ORG_FILES:
            if (src / name).exists():
                shutil.copy2(src / name, dest / name)
                copied.append(name)
    (dest / "SOURCE").write_text(source + "\n")
    return copied


def _requirement(args: argparse.Namespace, cfg) -> intake.Requirement | None:
    target = args.target
    if args.file or (target and Path(target).is_file()):
        return intake.from_file(Path(args.file or target))
    key = args.ticket or target
    if key:
        if not intake.TICKET_RE.match(key):
            raise PipelineError(f"'{key}' is neither a file nor a Jira key")
        return intake.from_jira(key, cfg.get("jira", {}) or {})
    return None


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(project_root())
    for w in cfg.warnings:
        print(f"warning: {w}", file=sys.stderr)
    only = args.only.split(",") if args.only else None
    explicit = bool(args.stage or args.start or args.end or only)
    selection = stages.select(args.stage, args.start, args.end, only)
    options = RunOptions(
        yes=args.yes,
        headless=args.headless,
        in_place=args.in_place,
        approve=args.approve.split(",") if args.approve else [],
    )
    pipe = Pipeline(cfg, options=options, ask=None if args.headless else interactive_ask())
    if args.dry_run:
        for st in selection:
            route = (
                f"{models.resolve(cfg, st.name).provider}:{models.resolve(cfg, st.name).model}"
                if st.name in cfg.models.get("stages", {})
                else st.tier
            )
            print(f"[{st.index}] {st.name:<9} {route:<32} requires: {', '.join(st.requires) or '-'}")
        return 0
    req = _requirement(args, cfg)
    if req:
        state, paths = pipe.start(req)
        print(f"run {state.run_id} on branch {state.branch} ({paths.workdir})")
    elif args.run or args.resume or explicit:
        run_id = args.run or pipe.current_run_id()
        if run_id and not args.adhoc:
            state, paths = pipe.open(run_id)
        elif explicit:
            state, paths = pipe.start_adhoc()
        else:
            raise PipelineError("no run to resume")
    else:
        raise PipelineError("nothing to run: pass a requirement file / Jira key, --resume, or --stage")
    skip = bool(args.resume) and not explicit
    status = pipe.execute(state, paths, selection, skip_passed=skip)
    print(f"run {state.run_id}: {status}; report: {paths.report_file.relative_to(cfg.root)}")
    return EXIT.get(status, 1)


def _open(args: argparse.Namespace):
    cfg = load_config(project_root())
    pipe = Pipeline(cfg)
    run_id = args.run or pipe.current_run_id()
    if not run_id:
        raise PipelineError("no current run; pass --run")
    return cfg, pipe.open(run_id)


def cmd_approve(args: argparse.Namespace) -> int:
    _, (state, paths) = _open(args)
    targets = args.stages or [n for n, r in state.stages.items() if r.status == "awaiting_approval"]
    for name in targets:
        record_approval(state, paths, stages.get(name).name, explicit=True)
        print(f"approved {name}")
    state.save(paths.run_dir)
    return 0


def cmd_permission(args: argparse.Namespace) -> int:
    from aidlc import permissions

    _, (_state, paths) = _open(args)
    if not args.ids:
        waiting = permissions.pending(paths.run_dir)
        for w in waiting:
            print(f"{w['id']}  [{w['stage']}] {w['reason']}\n          {w['subject']}")
        print("" if waiting else "no pending approval requests", end="\n" if not waiting else "")
        return 0
    choice = "deny" if args.choice == "deny" else ("run" if args.for_run else "once")
    for rid in args.ids:
        permissions.decide(paths.run_dir, rid, choice)
        print(f"{'denied' if choice == 'deny' else 'allowed (' + choice + ')'} {rid}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _, (state, _paths) = _open(args)
    if args.json:
        print(
            json.dumps(
                {
                    "run_id": state.run_id,
                    "status": state.status,
                    "stages": {k: v.status for k, v in state.stages.items()},
                },
                indent=2,
            )
        )
        return 0
    print(f"{state.run_id}  {state.status}  branch={state.branch}  loops={state.loops}")
    for st in stages.STAGES:
        rec = state.stages.get(st.name)
        if rec:
            print(f"  [{st.index}] {st.name:<9} {rec.status:<18} attempts={rec.attempts} gate={rec.gate or '-'}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from aidlc import report

    cfg, (state, paths) = _open(args)
    print(report.write(cfg, state, paths).read_text())
    return 0


def cmd_stages(_args: argparse.Namespace) -> int:
    for st in stages.STAGES:
        print(
            f"[{st.index}] {st.name:<9} {st.tier:<13} {st.title}  "
            f"(requires: {', '.join(st.requires) or '-'}; produces: {', '.join(st.produces) or 'report'})"
        )
    return 0


def cmd_stack(args: argparse.Namespace) -> int:
    print(json.dumps(stack.detect(Path(args.path).resolve()), indent=2))
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    cfg = load_config(project_root())
    ok = True
    for tool in ("git", "claude", "codex", "gh", "pip-audit", "bandit", "vulture", "npm", "madge"):
        print(f"{'ok ' if shutil.which(tool) else '-- '} {tool}")
    isolated = bool(cfg.get("harness.isolated")) or os.environ.get("AIDLC_ISOLATED") == "1"
    print(f"{'ok ' if isolated else '-- '} isolated agent runtime (container/VM)")
    for err in models.validate(cfg):
        ok = False
        print(f"ERR {err}")
    for w in cfg.warnings:
        print(f"WARN {w}")
    for name in cfg.models.get("stages", {}):
        choice = models.resolve(cfg, name)
        print(f"     {name:<9} -> {choice.tier}: {choice.provider}:{choice.model}")
    return 0 if ok else 1


def cmd_guard(args: argparse.Namespace) -> int:
    from aidlc.guardrails import hook_main

    return hook_main(args.event)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aidlc", description="AI-DLC: requirements -> reviewed PR")
    p.add_argument("--version", action="version", version=f"aidlc {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="initialize .aidlc/ and aidlc.yaml in this project")
    s.add_argument("--org", help="path or git URL with enterprise standards")
    s.add_argument("--force", action="store_true", help="overwrite aidlc.yaml")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("run", help="run the pipeline or selected stages")
    s.add_argument("target", nargs="?", help="requirement markdown file or Jira key")
    s.add_argument("--ticket", help="Jira issue key")
    s.add_argument("--file", help="requirement markdown file")
    s.add_argument("--run", help="existing run id (default: current)")
    s.add_argument("--resume", action="store_true", help="continue the current run, skipping passed stages")
    s.add_argument("--adhoc", action="store_true", help="run selected stages on the current checkout")
    s.add_argument("--stage", help="run a single stage")
    s.add_argument("--from", dest="start", help="first stage")
    s.add_argument("--to", dest="end", help="last stage")
    s.add_argument("--only", help="comma-separated stages")
    s.add_argument("--yes", action="store_true", help="auto-approve human gates")
    s.add_argument("--approve", help="comma-separated gates to approve explicitly")
    s.add_argument("--headless", action="store_true", help="CI mode: never prompt")
    s.add_argument("--in-place", action="store_true", help="use the current checkout instead of a worktree")
    s.add_argument("--dry-run", action="store_true", help="show the stage plan and model routing")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("approve", help="approve pending human gates")
    s.add_argument("stages", nargs="*")
    s.add_argument("--run")
    s.set_defaults(fn=cmd_approve)

    for verb, text in (("allow", "approve"), ("deny", "deny")):
        s = sub.add_parser(verb, help=f"{text} pending ask-tier guardrail requests (no ids: list them)")
        s.add_argument("ids", nargs="*")
        s.add_argument("--run")
        if verb == "allow":
            s.add_argument("--for-run", action="store_true", help="allow for the rest of the run, not just once")
        s.set_defaults(fn=cmd_permission, choice=verb, for_run=False)

    s = sub.add_parser("status", help="show run status")
    s.add_argument("--run")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("report", help="regenerate and print the run report")
    s.add_argument("--run")
    s.set_defaults(fn=cmd_report)

    sub.add_parser("stages", help="list pipeline stages").set_defaults(fn=cmd_stages)

    s = sub.add_parser("stack", help="detect the project stack")
    s.add_argument("path", nargs="?", default=".")
    s.set_defaults(fn=cmd_stack)

    sub.add_parser("doctor", help="check tools and configuration").set_defaults(fn=cmd_doctor)

    s = sub.add_parser("guard", help="guardrail hook entry point (Claude Code hooks)")
    s.add_argument("event", choices=["pre-tool", "post-tool"])
    s.set_defaults(fn=cmd_guard)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (PipelineError, gitutil.GitError, KeyError, ValueError) as exc:
        print(f"aidlc: {exc}", file=sys.stderr)
        return 2
