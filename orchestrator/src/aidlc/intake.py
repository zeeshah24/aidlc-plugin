"""Stage 0a: requirement intake from a markdown file or a Jira story."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml

from aidlc.util import slugify

TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


@dataclass
class Requirement:
    req_id: str
    title: str
    body: str
    source: str


def from_file(path: Path) -> Requirement:
    text = path.read_text()
    meta: dict = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            meta = yaml.safe_load(text[3:end]) or {}
            text = text[end + 4 :].lstrip()
    heading = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), path.stem)
    found = re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", heading)
    req_id = str(meta.get("id") or (found.group(0) if found else slugify(path.stem)))
    if not SAFE_ID.match(req_id) or ".." in req_id:
        digest = hashlib.sha256(req_id.encode()).hexdigest()[:8]
        req_id = f"{slugify(req_id)[:90].rstrip('-') or 'req'}-{digest}"
    title = str(meta.get("title") or heading)
    return Requirement(req_id, title, text, f"file:{path}")


def _jira_request(url: str, payload: dict | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if os.environ.get("JIRA_PAT"):
        headers["Authorization"] = f"Bearer {os.environ['JIRA_PAT']}"
    elif os.environ.get("JIRA_EMAIL") and os.environ.get("JIRA_API_TOKEN"):
        raw = f"{os.environ['JIRA_EMAIL']}:{os.environ['JIRA_API_TOKEN']}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - URL comes from project config
        return json.loads(resp.read())


def jira_base(jira_cfg: dict) -> str:
    return (jira_cfg.get("base_url") or os.environ.get("JIRA_BASE_URL") or "").rstrip("/")


def jira_key(source: str) -> str | None:
    return source.removeprefix("jira:") if source.startswith("jira:") else None


def jira_comment(key: str, text: str, jira_cfg: dict, send=_jira_request) -> None:
    """Write back to the ticket (e.g. the PR link) so traceability runs both ways."""
    send(f"{jira_base(jira_cfg)}/rest/api/2/issue/{key}/comment", {"body": text})


def from_jira(key: str, jira_cfg: dict, fetch=_jira_request) -> Requirement:
    base = jira_base(jira_cfg)
    if not base:
        raise ValueError("Jira base URL not configured (aidlc.yaml jira.base_url or JIRA_BASE_URL)")
    ac_field = jira_cfg.get("acceptance_criteria_field")
    data = fetch(f"{base}/rest/api/2/issue/{key}")
    fields = data.get("fields", {})
    summary = fields.get("summary", key)
    parts = [f"# {key}: {summary}", ""]
    meta = []
    for label, value in (
        ("Type", (fields.get("issuetype") or {}).get("name")),
        ("Priority", (fields.get("priority") or {}).get("name")),
        ("Labels", ", ".join(fields.get("labels") or [])),
    ):
        if value:
            meta.append(f"- **{label}:** {value}")
    parts += [*meta, "", "## Description", "", fields.get("description") or "_No description._"]
    if ac_field and fields.get(ac_field):
        parts += ["", "## Acceptance Criteria", "", str(fields[ac_field])]
    parts += ["", f"_Source: {base}/browse/{key}_"]
    return Requirement(key, summary, "\n".join(parts) + "\n", f"jira:{key}")


def write_requirement(req: Requirement, aidlc_dir: Path) -> Path:
    if not SAFE_ID.match(req.req_id) or ".." in req.req_id:
        raise ValueError(f"unsafe requirement id: {req.req_id!r}")
    path = aidlc_dir / "requirements" / f"{req.req_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    front = yaml.safe_dump({"id": req.req_id, "title": req.title, "source": req.source}, sort_keys=False)
    body = req.body
    if body.startswith("---"):
        end = body.find("\n---", 3)
        body = body[end + 4 :].lstrip() if end != -1 else body
    path.write_text(f"---\n{front}---\n\n{body}")
    return path
