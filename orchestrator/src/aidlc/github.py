"""GitHub access for the PR stage, via the `gh` CLI (the adopter's own credentials)."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from aidlc.util import tail


def gh(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=300)


def wait_for_checks(cwd: Path, url: str, timeout_s: int, interval_s: int) -> list[dict] | None:
    """Failed checks once all have finished, or None if still pending at the deadline."""
    deadline = time.monotonic() + timeout_s
    while True:
        proc = gh(["pr", "checks", url, "--json", "name,bucket,link"], cwd)
        checks = json.loads(proc.stdout or "[]") if proc.stdout.strip().startswith("[") else []
        if checks and all(c["bucket"] != "pending" for c in checks):
            return [c for c in checks if c["bucket"] == "fail"]
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval_s)


def failed_logs(cwd: Path, check: dict) -> str:
    m = re.search(r"/actions/runs/(\d+)", check.get("link", ""))
    if not m:
        return f"{check['name']}: no logs available"
    proc = gh(["run", "view", m.group(1), "--log-failed"], cwd)
    return f"### {check['name']}\n{tail(proc.stdout, 8000)}"
