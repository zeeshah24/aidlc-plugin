"""Small shared helpers."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def glob_match(path: str, globs: list[str]) -> bool:
    """fnmatch where `**/` also matches zero directories (`tests/**/*.py` matches `tests/x.py`)."""

    def one(g: str) -> bool:
        return (
            fnmatch.fnmatch(path, g)
            or (g.startswith("**/") and fnmatch.fnmatch(path, g[3:]))
            or ("/**/" in g and fnmatch.fnmatch(path, g.replace("/**/", "/")))
        )

    return any(one(g) for g in globs)


def run_killable(cmd, *, timeout: float | None, input: str | None = None, **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run`` that kills the whole process group on timeout or interrupt, so tools an agent or a
    test command spawned cannot outlive it and keep editing the worktree."""
    posix = os.name == "posix"
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=posix,
        **kwargs,
    )
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL) if posix else proc.kill()
        except ProcessLookupError:
            pass
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def slugify(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "req"


def tail(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else "...[truncated]...\n" + text[-max_chars:]


def head(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[:max_chars] + "\n...[truncated]..."


def strip_fence(text: str) -> str:
    """Remove a single wrapping ```markdown fence if the model added one."""
    s = text.strip()
    m = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*)\n```", s, flags=re.S)
    return (m.group(1) if m else s) + "\n"
