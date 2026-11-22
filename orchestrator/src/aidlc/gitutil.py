"""Thin git wrappers."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(args: list[str], cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def is_repo(cwd: Path) -> bool:
    return subprocess.run(["git", "rev-parse", "--git-dir"], cwd=cwd, capture_output=True).returncode == 0


def toplevel(cwd: Path) -> Path:
    return Path(git(["rev-parse", "--show-toplevel"], cwd).strip())


def head_sha(cwd: Path) -> str:
    return git(["rev-parse", "HEAD"], cwd).strip()


def rev(cwd: Path, ref: str) -> str | None:
    out = subprocess.run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=cwd, capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else None


def current_branch(cwd: Path) -> str:
    return git(["rev-parse", "--abbrev-ref", "HEAD"], cwd).strip()


def dirty_paths(cwd: Path) -> list[str]:
    """Modified, added and untracked paths relative to the worktree root."""
    out = git(["status", "--porcelain", "--untracked-files=all"], cwd)
    paths = []
    for line in out.splitlines():
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        if not path.startswith(".aidlc/"):
            paths.append(path)
    return paths


def changed_since(cwd: Path, base: str) -> list[str]:
    committed = git(["diff", "--name-only", f"{base}...HEAD"], cwd).splitlines()
    return sorted(set(committed) | set(dirty_paths(cwd)))


def diff(cwd: Path, base: str) -> str:
    """Committed and uncommitted changes since ``base`` (a merge-base of HEAD)."""
    return git(["diff", base], cwd)


def commit_all(cwd: Path, message: str, name: str, email: str) -> str | None:
    if not dirty_paths(cwd):
        return None
    git(["add", "-A", "--", ".", ":(exclude).aidlc"], cwd)
    git(["-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "-m", message], cwd)
    return head_sha(cwd)


def restore(cwd: Path, ref: str, paths: list[str]) -> None:
    if paths:
        git(["checkout", ref, "--", *paths], cwd)
