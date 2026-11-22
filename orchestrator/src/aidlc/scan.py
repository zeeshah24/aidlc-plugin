"""Stage 7: static analysis gate with baseline-delta comparison.

Built-in, dependency-free analyzers (Python circular imports, complexity, public API drift,
language-agnostic duplication) run everywhere; external tools (pip-audit, bandit, vulture,
npm audit, madge, any SARIF-emitting tool) run when installed or configured.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shlex
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from aidlc import gitutil
from aidlc.util import glob_match

SEVERITY = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
SOURCE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".cs", ".rs", ".kt", ".rb", ".php"}
_COMMENT = re.compile(r"^(#|//|/\*|\*|\*/)")


@dataclass
class Finding:
    tool: str
    rule: str
    category: str  # vulnerability|dead_code|circular_dependency|duplication|complexity|api_drift
    severity: str
    file: str
    message: str
    line: int | None = None
    key: str = ""

    def __post_init__(self):
        if not self.key:
            self.key = f"{self.tool}|{self.rule}|{self.file}|{re.sub(r'[0-9]+', '#', self.message)}"


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def tracked_files(root: Path) -> list[str]:
    out = gitutil.git(["ls-files", "--cached", "--others", "--exclude-standard"], root)
    return [f for f in out.splitlines() if (root / f).is_file()]


def _is_test(path: str, globs: list[str]) -> bool:
    return glob_match(path, globs)


# --- Python built-ins -------------------------------------------------------------------------


def _module_name(rel: str) -> str:
    parts = list(Path(rel).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _parse(root: Path, rel: str) -> ast.Module | None:
    try:
        return ast.parse((root / rel).read_text(errors="ignore"))
    except SyntaxError:
        return None


def circular_imports(root: Path, files: list[str]) -> list[Finding]:
    py = [f for f in files if f.endswith(".py")]
    modules = {_module_name(f): f for f in py}
    graph: dict[str, set[str]] = {m: set() for m in modules}
    for mod, rel in modules.items():
        tree = _parse(root, rel)
        if tree is None:
            continue
        package = mod if rel.endswith("__init__.py") else mod.rpartition(".")[0]
        type_only = {
            id(n)
            for guard in ast.walk(tree)
            if isinstance(guard, ast.If) and "TYPE_CHECKING" in ast.unparse(guard.test)
            for stmt in guard.body
            for n in ast.walk(stmt)
        }
        for node in ast.walk(tree):
            if id(node) in type_only:
                continue
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    pkg_parts = package.split(".") if package else []
                    pkg_parts = pkg_parts[: len(pkg_parts) - (node.level - 1)] if node.level > 1 else pkg_parts
                    base = ".".join([*pkg_parts, base] if base else pkg_parts)
                for a in node.names:
                    sub = f"{base}.{a.name}" if base else a.name
                    targets.append(sub if sub in modules else base)
            for t in targets:
                while t and t not in modules:
                    t = t.rpartition(".")[0]
                if t and t != mod:
                    graph[mod].add(t)
                    parent = t.rpartition(".")[0]
                    while parent:  # importing pkg.sub runs pkg/__init__ first
                        if parent in modules and parent != mod and not mod.startswith(parent + "."):
                            graph[mod].add(parent)
                        parent = parent.rpartition(".")[0]
    findings = []
    for scc in _sccs(graph):
        if len(scc) > 1:
            members = sorted(scc)
            findings.append(
                Finding(
                    "aidlc",
                    "circular-import",
                    "circular_dependency",
                    "high",
                    modules[members[0]],
                    "import cycle: " + " -> ".join(members),
                    key="circular|" + "|".join(members),
                )
            )
    return findings


def _sccs(graph: dict[str, set[str]]) -> list[set[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[set[str]] = []
    counter = [0]

    def visit(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in graph.get(v, ()):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = set()
            while True:
                w = stack.pop()
                on.discard(w)
                comp.add(w)
                if w == v:
                    break
            out.append(comp)

    for v in list(graph):
        if v not in index:
            visit(v)
    return out


_BRANCHES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.IfExp,
    ast.ExceptHandler,
    ast.With,
    ast.AsyncWith,
    ast.Assert,
    ast.comprehension,
)


def _complexity(fn: ast.AST) -> int:
    score = 1
    for node in ast.walk(fn):
        if isinstance(node, _BRANCHES):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += len(node.values) - 1
        elif type(node).__name__ == "match_case":
            score += 1
    return score


def complexity(root: Path, files: list[str], threshold: int) -> list[Finding]:
    findings = []
    for rel in (f for f in files if f.endswith(".py")):
        tree = _parse(root, rel)
        if tree is None:
            continue
        for qual, fn in _functions(tree):
            score = _complexity(fn)
            if score > threshold:
                sev = "high" if score > threshold * 2 else "medium"
                findings.append(
                    Finding(
                        "aidlc",
                        "cyclomatic-complexity",
                        "complexity",
                        sev,
                        rel,
                        f"{qual} has cyclomatic complexity {score} (> {threshold})",
                        fn.lineno,
                        key=f"complexity|{rel}|{qual}",
                    )
                )
    return findings


def _functions(tree: ast.Module, prefix: str = ""):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield prefix + node.name, node
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef):
                    yield f"{prefix}{node.name}.{sub.name}", sub


def public_api(root: Path, files: list[str], test_globs: list[str]) -> dict[str, str]:
    api: dict[str, str] = {}
    for rel in files:
        if not rel.endswith(".py") or _is_test(rel, test_globs):
            continue
        if any(p.startswith("_") and p != "__init__.py" for p in Path(rel).parts):
            continue
        tree = _parse(root, rel)
        if tree is None:
            continue
        mod = _module_name(rel)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not node.name.startswith("_"):
                api[f"{mod}.{node.name}"] = _signature(node)
            elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                api[f"{mod}.{node.name}"] = "class"
                for sub in node.body:
                    if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef) and (
                        not sub.name.startswith("_") or sub.name == "__init__"
                    ):
                        api[f"{mod}.{node.name}.{sub.name}"] = _signature(sub)
    return api


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    a = fn.args
    required = len(a.args) - len(a.defaults)
    parts = [f"{x.arg}{'' if i < required else '='}" for i, x in enumerate(a.args)]
    parts += [f"*{a.vararg.arg}"] if a.vararg else []
    parts += [f"{k.arg}{'=' if d is not None else ''}" for k, d in zip(a.kwonlyargs, a.kw_defaults, strict=False)]
    parts += [f"**{a.kwarg.arg}"] if a.kwarg else []
    return "(" + ", ".join(parts) + ")"


def api_drift(base_api: dict[str, str], head_api: dict[str, str], declared_text: str) -> list[Finding]:
    findings = []
    for sym, sig in base_api.items():
        name = sym.rsplit(".", 1)[-1]
        declared = name in declared_text or sym in declared_text
        sev = "low" if declared else "high"
        note = " (declared in plan)" if declared else ""
        file = sym.rsplit(".", 1)[0]
        if sym not in head_api:
            findings.append(
                Finding(
                    "aidlc",
                    "api-removed",
                    "api_drift",
                    sev,
                    file,
                    f"public symbol {sym}{sig if sig != 'class' else ''} removed{note}",
                )
            )
        elif head_api[sym] != sig:
            findings.append(
                Finding(
                    "aidlc",
                    "api-changed",
                    "api_drift",
                    sev,
                    file,
                    f"public signature changed: {sym}{sig} -> {sym}{head_api[sym]}{note}",
                )
            )
    return findings


# --- Language-agnostic duplication -----------------------------------------------------------


def duplication(root: Path, files: list[str], min_lines: int, exclude: list[str]) -> list[Finding]:
    windows: dict[str, list[tuple[str, int]]] = {}
    for rel in files:
        if Path(rel).suffix not in SOURCE_EXT or _is_test(rel, exclude):
            continue
        lines = [(i + 1, ln.strip()) for i, ln in enumerate((root / rel).read_text(errors="ignore").splitlines())]
        lines = [(n, s) for n, s in lines if s and not _COMMENT.match(s) and len(s) > 2]
        for i in range(len(lines) - min_lines + 1):
            chunk = "\n".join(s for _, s in lines[i : i + min_lines])
            digest = hashlib.sha1(chunk.encode()).hexdigest()[:12]
            windows.setdefault(digest, []).append((rel, lines[i][0]))
    findings, seen = [], set()
    for digest, locs in windows.items():
        if len(locs) < 2:
            continue
        (fa, la), (fb, lb) = locs[0], locs[1]
        if fa == fb and abs(la - lb) < min_lines:
            continue
        pair = (fa, fb)
        if any(p == pair and abs(line - la) <= min_lines for p, line in seen):
            seen.add((pair, la))
            continue
        seen.add((pair, la))
        findings.append(
            Finding(
                "aidlc",
                "duplicate-block",
                "duplication",
                "medium",
                fa,
                f"{min_lines}+ duplicated lines at {fa}:{la} and {fb}:{lb}",
                la,
                key=f"dup|{digest}",
            )
        )
    return findings


# --- External tools ---------------------------------------------------------------------------


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)


def pip_audit(root: Path) -> list[Finding]:
    if not (root / "requirements.txt").exists():
        return []
    out = _run(["pip-audit", "-r", "requirements.txt", "-f", "json", "--progress-spinner", "off"], root).stdout
    data = json.loads(out or "{}")
    deps = data.get("dependencies", data if isinstance(data, list) else [])
    return [
        Finding(
            "pip-audit",
            v["id"],
            "vulnerability",
            "high",
            "requirements.txt",
            f"{d['name']}=={d.get('version')} has {v['id']}",
            key=f"pip-audit|{d['name']}|{v['id']}",
        )
        for d in deps
        for v in d.get("vulns", [])
    ]


def bandit(root: Path, test_globs: list[str]) -> list[Finding]:
    data = json.loads(_run(["bandit", "-r", ".", "-f", "json", "-q"], root).stdout or "{}")
    out = []
    for r in data.get("results", []):
        rel = str(Path(r["filename"]).as_posix()).removeprefix("./")
        if _is_test(rel, test_globs) or rel.startswith((".aidlc/", ".venv/")):
            continue
        out.append(
            Finding(
                "bandit",
                r["test_id"],
                "vulnerability",
                r["issue_severity"].lower(),
                rel,
                r["issue_text"],
                r.get("line_number"),
            )
        )
    return out


def vulture(root: Path) -> list[Finding]:
    out = []
    for line in _run(["vulture", ".", "--min-confidence", "80", "--exclude", ".aidlc,.venv"], root).stdout.splitlines():
        m = re.match(r"(.+?):(\d+): (.+)", line)
        if m:
            out.append(
                Finding(
                    "vulture",
                    "unused-code",
                    "dead_code",
                    "medium",
                    m.group(1).removeprefix("./"),
                    m.group(3),
                    int(m.group(2)),
                )
            )
    return out


def npm_audit(root: Path) -> list[Finding]:
    data = json.loads(_run(["npm", "audit", "--json"], root).stdout or "{}")
    return [
        Finding(
            "npm-audit",
            "advisory",
            "vulnerability",
            v.get("severity", "high"),
            "package.json",
            f"{name}: {v.get('severity')} vulnerability",
            key=f"npm-audit|{name}|{v.get('severity')}",
        )
        for name, v in data.get("vulnerabilities", {}).items()
    ]


def madge(root: Path) -> list[Finding]:
    cmd = ["madge", "--circular", "--json", "--extensions", "js,jsx,ts,tsx", "."]
    cycles = json.loads(_run(cmd, root).stdout or "[]")
    return [
        Finding(
            "madge",
            "circular-import",
            "circular_dependency",
            "high",
            c[0],
            "import cycle: " + " -> ".join(c),
            key="madge|" + "|".join(sorted(c)),
        )
        for c in cycles
    ]


def sarif(tool: dict, root: Path) -> list[Finding]:
    proc = _run(shlex.split(tool["command"]), root)
    data = json.loads(proc.stdout or "{}")
    levels = {"error": "high", "warning": "medium", "note": "low", "none": "info"}
    out = []
    for run in data.get("runs", []):
        for r in run.get("results", []):
            loc = (r.get("locations") or [{}])[0].get("physicalLocation", {})
            out.append(
                Finding(
                    tool["name"],
                    r.get("ruleId", "rule"),
                    tool.get("category", "vulnerability"),
                    levels.get(r.get("level", "warning"), "medium"),
                    loc.get("artifactLocation", {}).get("uri", "?"),
                    r.get("message", {}).get("text", ""),
                    loc.get("region", {}).get("startLine"),
                )
            )
    return out


def run_scanners(root: Path, profile: dict, gate_cfg: dict) -> ScanResult:
    files = [f for f in tracked_files(root) if not f.startswith(".aidlc/")]
    test_globs = profile.get("test_globs", [])
    result = ScanResult()
    langs = set(profile.get("languages", []))
    enabled = set(
        gate_cfg.get(
            "analyzers",
            ["vulnerability", "dead_code", "circular_dependency", "duplication", "complexity", "api_drift"],
        )
    )
    src = [f for f in files if not _is_test(f, test_globs)]
    if "python" in langs:
        if "circular_dependency" in enabled:
            result.findings += circular_imports(root, src)
        if "complexity" in enabled:
            result.findings += complexity(root, src, int(gate_cfg.get("complexity_threshold", 10)))
    if "duplication" in enabled:
        result.findings += duplication(root, files, int(gate_cfg.get("duplication_min_lines", 8)), test_globs)

    external = [
        ("pip-audit", "vulnerability", "python", lambda: pip_audit(root)),
        ("bandit", "vulnerability", "python", lambda: bandit(root, test_globs)),
        ("vulture", "dead_code", "python", lambda: vulture(root)),
        ("npm", "vulnerability", "js", lambda: npm_audit(root)),
        ("madge", "circular_dependency", "js", lambda: madge(root)),
    ]
    for binary, category, lang, fn in external:
        if category not in enabled:
            continue
        if lang == "js" and not langs & {"javascript", "typescript"}:
            continue
        if lang == "python" and "python" not in langs:
            continue
        if not shutil.which(binary):
            result.skipped.append(f"{binary} ({category}): not installed")
            continue
        try:
            result.findings += fn()
        except (json.JSONDecodeError, subprocess.SubprocessError, KeyError) as exc:
            result.skipped.append(f"{binary} ({category}): failed: {exc}")
    for tool in gate_cfg.get("tools", []):
        try:
            result.findings += sarif(tool, root)
        except (json.JSONDecodeError, subprocess.SubprocessError, OSError) as exc:
            result.skipped.append(f"{tool.get('name')}: failed: {exc}")
    return result


def gate(
    repo: Path, workdir: Path, base_sha: str, profile: dict, gate_cfg: dict, plan_text: str, cache_dir: Path
) -> dict:
    """Scan head and baseline; only findings new relative to the baseline can block."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256(json.dumps([gate_cfg, profile], sort_keys=True).encode()).hexdigest()[:12]
    cache = cache_dir / f"scan-baseline-{base_sha[:12]}-{fingerprint}.json"
    if cache.exists():
        base = json.loads(cache.read_text())
    else:
        tmp = cache_dir / f"base-{base_sha[:12]}"
        if tmp.exists():
            gitutil.git(["worktree", "remove", "--force", str(tmp)], repo, check=False)
        gitutil.git(["worktree", "add", "-q", "--detach", str(tmp), base_sha], repo)
        try:
            res = run_scanners(tmp, profile, gate_cfg)
            api = public_api(tmp, tracked_files(tmp), profile.get("test_globs", []))
        finally:
            gitutil.git(["worktree", "remove", "--force", str(tmp)], repo, check=False)
        base = {"keys": sorted({f.key for f in res.findings}), "api": api}
        cache.write_text(json.dumps(base))

    head = run_scanners(workdir, profile, gate_cfg)
    if "api_drift" in set(gate_cfg.get("analyzers", ["api_drift"])) and "python" in profile.get("languages", []):
        head_api = public_api(workdir, tracked_files(workdir), profile.get("test_globs", []))
        head.findings += api_drift(base["api"], head_api, plan_text)
    baseline = set(base["keys"])
    new = [f for f in head.findings if f.key not in baseline]
    threshold = SEVERITY[gate_cfg.get("block_severity", "medium")]
    blocking = [f for f in new if SEVERITY.get(f.severity, 2) >= threshold]
    return {
        "passed": not blocking,
        "baseline_count": len(baseline),
        "head_count": len(head.findings),
        "new": [asdict(f) for f in new],
        "blocking": [asdict(f) for f in blocking],
        "skipped": head.skipped,
        "block_severity": gate_cfg.get("block_severity", "medium"),
    }


def render(result: dict) -> str:
    lines = [
        f"**Gate:** {'PASS' if result['passed'] else 'BLOCK'} "
        f"(baseline {result['baseline_count']} findings, head {result['head_count']}, "
        f"new {len(result['new'])}, blocking {len(result['blocking'])} at >= {result['block_severity']})",
        "",
    ]
    if result["new"]:
        lines += ["| Severity | Category | Tool | Location | Message |", "|---|---|---|---|---|"]
        for f in result["new"]:
            loc = f"{f['file']}:{f['line']}" if f.get("line") else f["file"]
            lines.append(f"| {f['severity']} | {f['category']} | {f['tool']} | `{loc}` | {f['message']} |")
    if result["skipped"]:
        lines += ["", "Skipped: " + "; ".join(result["skipped"])]
    return "\n".join(lines) + "\n"
