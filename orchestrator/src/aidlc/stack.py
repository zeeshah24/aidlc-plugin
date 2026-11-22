"""Stage 0b: deterministic stack detection -> stack-profile.json."""

from __future__ import annotations

import json
from pathlib import Path

_SKIP = {".git", ".aidlc", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", "target"}


def _exists(root: Path, *names: str) -> bool:
    return any((root / n).exists() for n in names)


def _glob_any(root: Path, pattern: str) -> bool:
    return any(p for p in root.glob(pattern) if not (set(p.relative_to(root).parts) & _SKIP))


def detect(root: Path, overrides: dict | None = None) -> dict:
    profile: dict = {
        "languages": [],
        "package_managers": [],
        "frameworks": [],
        "test_command": None,
        "lint_command": None,
        "test_globs": [],
        "ecosystems": [],
    }
    if _exists(root, "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt") or _glob_any(root, "*.py"):
        profile["languages"].append("python")
        profile["ecosystems"].append("PyPI")
        text = (root / "pyproject.toml").read_text() if (root / "pyproject.toml").exists() else ""
        reqs = (root / "requirements.txt").read_text() if (root / "requirements.txt").exists() else ""
        if "poetry" in text:
            profile["package_managers"].append("poetry")
        elif (root / "uv.lock").exists():
            profile["package_managers"].append("uv")
        else:
            profile["package_managers"].append("pip")
        profile["frameworks"] += [
            fw for fw in ("django", "fastapi", "flask", "pydantic") if fw in (text + reqs).lower()
        ]
        profile["test_command"] = "python -m pytest -q"
        profile["lint_command"] = "ruff check ." if "ruff" in text else None
        profile["test_globs"] += [
            "tests/**/*.py",
            "test/**/*.py",
            "**/test_*.py",
            "**/*_test.py",
            "**/conftest.py",
        ]
    if (root / "package.json").exists():
        pkg = json.loads((root / "package.json").read_text())
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        profile["languages"].append(
            "typescript" if "typescript" in deps or (root / "tsconfig.json").exists() else "javascript"
        )
        profile["ecosystems"].append("npm")
        pm = "pnpm" if (root / "pnpm-lock.yaml").exists() else "yarn" if (root / "yarn.lock").exists() else "npm"
        profile["package_managers"].append(pm)
        profile["frameworks"] += [
            fw for fw in ("react", "next", "vue", "express", "nestjs", "jest", "vitest") if fw in deps
        ]
        if "test" in pkg.get("scripts", {}):
            profile["test_command"] = profile["test_command"] or f"{pm} test"
        if "lint" in pkg.get("scripts", {}):
            profile["lint_command"] = profile["lint_command"] or f"{pm} run lint"
        profile["test_globs"] += ["**/*.test.*", "**/*.spec.*", "**/__tests__/**", "test/**", "tests/**"]
    if (root / "go.mod").exists():
        profile["languages"].append("go")
        profile["ecosystems"].append("Go")
        profile["test_command"] = profile["test_command"] or "go test ./..."
        profile["test_globs"].append("**/*_test.go")
    if _exists(root, "pom.xml", "build.gradle", "build.gradle.kts"):
        profile["languages"].append("java")
        profile["ecosystems"].append("Maven")
        cmd = "mvn -q test" if (root / "pom.xml").exists() else "./gradlew test"
        profile["test_command"] = profile["test_command"] or cmd
        profile["test_globs"].append("src/test/**")
    if _glob_any(root, "*.sln") or _glob_any(root, "**/*.csproj"):
        profile["languages"].append("csharp")
        profile["ecosystems"].append("NuGet")
        profile["test_command"] = profile["test_command"] or "dotnet test"
        profile["test_globs"] += ["**/*Tests/**", "**/*.Tests/**"]
    if (root / "Cargo.toml").exists():
        profile["languages"].append("rust")
        profile["ecosystems"].append("crates.io")
        profile["test_command"] = profile["test_command"] or "cargo test"
        profile["test_globs"].append("tests/**")
    profile["test_globs"] = list(dict.fromkeys(profile["test_globs"]))
    for key, value in (overrides or {}).items():
        if value is not None:
            profile[key] = value
    return profile
