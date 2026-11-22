"""Bidirectional traceability: requirement IDs <-> tests <-> changed code."""

from __future__ import annotations

import re
from pathlib import Path


def requirement_ids(spec_text: str, pattern: str) -> list[str]:
    return list(dict.fromkeys(re.findall(pattern, spec_text)))


def references(workdir: Path, files: list[str], pattern: str) -> dict[str, set[str]]:
    rx = re.compile(pattern)
    refs: dict[str, set[str]] = {}
    for rel in files:
        path = workdir / rel
        if not path.is_file():
            continue
        try:
            found = set(rx.findall(path.read_text(errors="ignore", encoding="utf-8")))
        except OSError:
            continue
        if found:
            refs[rel] = found
    return refs


def build_matrix(ids: list[str], test_refs: dict[str, set[str]], code_refs: dict[str, set[str]]) -> dict:
    rows = []
    for rid in ids:
        rows.append(
            {
                "id": rid,
                "tests": sorted(f for f, s in test_refs.items() if rid in s),
                "code": sorted(f for f, s in code_refs.items() if rid in s),
            }
        )
    known = set(ids)
    orphans = sorted({r for s in test_refs.values() for r in s} - known)
    return {"rows": rows, "untested": [r["id"] for r in rows if not r["tests"]], "orphan_refs": orphans}


def render(matrix: dict) -> str:
    lines = ["| Requirement | Tests | Code |", "|---|---|---|"]
    for row in matrix["rows"]:
        tests = "<br>".join(f"`{t}`" for t in row["tests"]) or "**none**"
        code = "<br>".join(f"`{c}`" for c in row["code"]) or "-"
        lines.append(f"| {row['id']} | {tests} | {code} |")
    if matrix["orphan_refs"]:
        lines.append(f"\nTests reference unknown requirement IDs: {', '.join(matrix['orphan_refs'])}")
    return "\n".join(lines) + "\n"
