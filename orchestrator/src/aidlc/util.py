"""Small shared helpers."""

from __future__ import annotations

import fnmatch
import hashlib
import re
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
    """fnmatch where a leading `**/` also matches files at the repository root."""
    return any(fnmatch.fnmatch(path, g) or (g.startswith("**/") and fnmatch.fnmatch(path, g[3:])) for g in globs)


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
