"""Small shared helpers."""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator
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
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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
    """``subprocess.run`` that kills the whole process tree on timeout or interrupt, so tools an agent or a
    test command spawned cannot outlive it and keep editing the worktree."""
    posix = os.name == "posix"
    if isinstance(cmd, list) and not kwargs.get("shell"):
        cmd = [resolve_exe(cmd[0]), *cmd[1:]]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=posix,
        **kwargs,
    )
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except BaseException:
        _kill_tree(proc)
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        return
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    with contextlib.suppress(OSError):
        proc.kill()


def resolve_exe(name: str) -> str:
    """Full path of a program on PATH. Windows only starts `claude.cmd`/`codex.cmd` (npm shims) by full path."""
    return shutil.which(name) or name


@contextlib.contextmanager
def file_lock(path: Path, *, blocking: bool = True) -> Iterator[bool]:
    """Exclusive inter-process lock on `path`; yields False when `blocking` is off and another process holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as fh:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            while True:
                try:
                    msvcrt.locking(fh.fileno(), mode, 1)
                    break
                except OSError:
                    if not blocking:
                        yield False
                        return
            try:
                yield True
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


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
