"""Ask-tier guardrail exceptions: raised by the guard hook, decided by the developer.

Deny rules are never routed here; only actions the policy marks as `ask` can be approved, and only
in interactive runs. Every request and decision is written to the run's audit log.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aidlc.harness import AuditLog
from aidlc.util import now_iso

FILE = "permissions.json"
CHOICES = {"once": "allowed_once", "run": "allowed_run", "deny": "denied"}


def load(run_dir: Path) -> list[dict]:
    path = run_dir / FILE
    return json.loads(path.read_text()) if path.exists() else []


def _save(run_dir: Path, items: list[dict]) -> None:
    (run_dir / FILE).write_text(json.dumps(items, indent=2) + "\n")


def request_id(subject: str) -> str:
    return hashlib.sha256(subject.encode()).hexdigest()[:8]


def check(
    run_dir: Path, subject: str, stage: str, tool: str, reason: str, interactive: bool, audit: AuditLog
) -> tuple[bool, str]:
    """Return (allowed, message) for an ask-tier action, recording a request when needed."""
    if not interactive:
        return False, f"{reason} (needs developer approval, which headless runs never grant)"
    items = load(run_dir)
    rid = request_id(subject)
    item = next((i for i in items if i["id"] == rid), None)
    status = item["status"] if item else None
    if status in ("allowed_run", "allowed_once"):
        if status == "allowed_once":
            item["status"] = "used"
            _save(run_dir, items)
        audit.log("permission.used", id=rid, scope=status)
        return True, ""
    if status == "denied":
        return False, f"{reason} (denied by the developer)"
    if status != "pending":
        items = [i for i in items if i["id"] != rid]
        items.append(
            {
                "id": rid,
                "subject": subject,
                "stage": stage,
                "tool": tool,
                "reason": reason,
                "status": "pending",
                "requested": now_iso(),
            }
        )
        _save(run_dir, items)
        audit.log("permission.requested", id=rid, subject=subject[:300], reason=reason)
    return False, (
        f"{reason}. Developer approval requested (id {rid}). Do not work around this; "
        "finish your turn and state what you were blocked on."
    )


def pending(run_dir: Path, stage: str | None = None) -> list[dict]:
    return [i for i in load(run_dir) if i["status"] == "pending" and stage in (None, i["stage"])]


def decide(run_dir: Path, rid: str, choice: str, actor: str = "human") -> dict:
    items = load(run_dir)
    item = next((i for i in items if i["id"] == rid and i["status"] == "pending"), None)
    if item is None:
        raise KeyError(f"no pending approval request '{rid}'")
    item["status"] = CHOICES[choice]
    item["decided"] = now_iso()
    _save(run_dir, items)
    event = "permission.denied" if choice == "deny" else "permission.granted"
    AuditLog(run_dir / "audit.jsonl", actor=actor).log(event, id=rid, scope=choice, subject=item["subject"][:300])
    return item


def expire(run_dir: Path, stage: str) -> None:
    items = load(run_dir)
    for i in items:
        if i["status"] == "pending" and i["stage"] == stage:
            i["status"] = "expired"
    _save(run_dir, items)
