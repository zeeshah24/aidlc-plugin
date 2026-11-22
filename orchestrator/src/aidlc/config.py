"""Layered configuration: plugin defaults -> org (.aidlc/org) -> project (aidlc.yaml).

The org layer may set policy freely. The project layer may only tighten guardrails
(least privilege, no escalation): it can add denies and drop allows, never widen them.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from aidlc.resources import plugin_root, template
from aidlc.util import load_yaml

PROJECT_FILE = "aidlc.yaml"
AIDLC_DIR = ".aidlc"


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _union(a: list, b: list) -> list:
    return list(dict.fromkeys([*a, *b]))


def _subset(base: list, override: list, label: str, warnings: list[str]) -> list:
    dropped = [x for x in override if x not in base]
    if dropped:
        warnings.append(f"guardrails: project cannot widen {label}; ignored {dropped}")
    return [x for x in override if x in base]


def merge_guardrails(base: dict, override: dict) -> tuple[dict, list[str]]:
    """Merge a lower-trust guardrails layer onto ``base`` without allowing escalation."""
    warnings: list[str] = []
    out = copy.deepcopy(base)
    o_cmd, b_cmd = override.get("commands", {}), out.setdefault("commands", {})
    if "allow" in o_cmd:
        b_cmd["allow"] = _subset(b_cmd.get("allow", []), o_cmd["allow"], "commands.allow", warnings)
    b_cmd["deny"] = _union(b_cmd.get("deny", []), o_cmd.get("deny", []))
    if "ask" in o_cmd:
        b_cmd["ask"] = _subset(b_cmd.get("ask", []), o_cmd["ask"], "commands.ask", warnings)
    if o_cmd.get("unlisted") == "deny":
        b_cmd["unlisted"] = "deny"
    elif o_cmd.get("unlisted") == "ask" and b_cmd.get("unlisted", "deny") != "ask":
        warnings.append("guardrails: project cannot relax commands.unlisted to 'ask'")

    o_paths, b_paths = override.get("paths", {}), out.setdefault("paths", {})
    b_paths["protected"] = _union(b_paths.get("protected", []), o_paths.get("protected", []))
    b_paths["ask"] = _union(b_paths.get("ask", []), o_paths.get("ask", []))
    b_paths["no_read"] = _union(b_paths.get("no_read", []), o_paths.get("no_read", []))

    o_dep, b_dep = override.get("dependencies", {}), out.setdefault("dependencies", {})
    for key in ("allowed_licenses", "allowed_registries"):
        if key in o_dep:
            b_dep[key] = _subset(b_dep.get(key, []), o_dep[key], f"dependencies.{key}", warnings)
    if o_dep.get("block_on_cve") is False and b_dep.get("block_on_cve", True):
        warnings.append("guardrails: project cannot disable dependencies.block_on_cve")
    b_dep["install_commands"] = _union(b_dep.get("install_commands", []), o_dep.get("install_commands", []))

    o_env, b_env = override.get("env", {}), out.setdefault("env", {})
    if "passthrough" in o_env:
        b_env["passthrough"] = _subset(b_env.get("passthrough", []), o_env["passthrough"], "env.passthrough", warnings)
    b_env["test_exclude"] = _union(b_env.get("test_exclude", []), o_env.get("test_exclude", []))

    out["prompt_rules"] = _union(out.get("prompt_rules", []), override.get("prompt_rules", []))
    return out, warnings


@dataclass
class Config:
    root: Path
    project: dict
    models: dict
    guardrails: dict
    conventions: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def aidlc_dir(self) -> Path:
        return self.root / AIDLC_DIR

    def get(self, dotted: str, default=None):
        node = self.project
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def load_config(root: Path) -> Config:
    plugin = plugin_root()
    models = load_yaml(plugin / "models.yaml")
    guardrails = load_yaml(plugin / "guardrails.yaml")
    project = yaml.safe_load(template(PROJECT_FILE)) or {}

    org = root / AIDLC_DIR / "org"
    models = deep_merge(models, load_yaml(org / "models.yaml"))
    guardrails = deep_merge(guardrails, load_yaml(org / "guardrails.yaml"))
    project = deep_merge(project, load_yaml(org / PROJECT_FILE))

    local = load_yaml(root / PROJECT_FILE)
    warnings: list[str] = []
    org_telemetry = load_yaml(org / PROJECT_FILE).get("telemetry")
    if org_telemetry and "telemetry" in local:
        # the org owns telemetry policy; a project may only switch it off
        requested = local.pop("telemetry") or {}
        if requested.get("enabled") is False:
            project["telemetry"]["enabled"] = False
        ignored = sorted(k for k in requested if k != "enabled" or requested[k] is not False)
        if ignored:
            warnings.append(f"telemetry: set by org policy; project keys ignored {ignored}")
    project = deep_merge(project, {k: v for k, v in local.items() if k not in ("models", "guardrails")})
    models = deep_merge(models, local.get("models", {}))
    guardrails, rail_warnings = merge_guardrails(guardrails, local.get("guardrails", {}))
    warnings += rail_warnings

    conventions = []
    for layer, path in (
        ("org", org / "conventions.md"),
        ("repo", root / "CONVENTIONS.md"),
        ("project", root / AIDLC_DIR / "conventions.md"),
    ):
        if path.exists() and path.read_text().strip():
            conventions.append((layer, path.read_text().strip()))
    return Config(root, project, models, guardrails, conventions, warnings)
