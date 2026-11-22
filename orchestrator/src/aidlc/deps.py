"""Gated dependency installs: license, CVE (OSV) and registry checks for proposed dependencies."""

from __future__ import annotations

import json
import re
import urllib.request

import yaml

DEP_BLOCK = re.compile(r"```aidlc-dependencies[^\n]*\n(.*?)```", re.S)

_LICENSE_ALIASES = {
    "mit": "MIT",
    "mit license": "MIT",
    "apache 2.0": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    "bsd-2-clause": "BSD-2-Clause",
    "isc": "ISC",
    "isc license": "ISC",
    "isc license (iscl)": "ISC",
    "psf": "PSF-2.0",
    "python software foundation license": "PSF-2.0",
    "psf-2.0": "PSF-2.0",
    "mpl-2.0": "MPL-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "gpl-3.0": "GPL-3.0",
    "gnu general public license v3 (gplv3)": "GPL-3.0",
    "agpl-3.0": "AGPL-3.0",
    "lgpl-3.0": "LGPL-3.0",
    "unlicense": "Unlicense",
}


def parse_proposals(plan_text: str) -> list[dict]:
    match = DEP_BLOCK.search(plan_text)
    if not match:
        return []
    data = yaml.safe_load(match.group(1)) or []
    if not isinstance(data, list):
        raise ValueError("aidlc-dependencies block must be a YAML list")
    return [d for d in data if isinstance(d, dict) and d.get("name")]


def normalize_license(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.strip().lower()
    return _LICENSE_ALIASES.get(key, raw.strip() if len(raw) < 40 else None)


def http_json(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed public registries
        return json.loads(resp.read())


def lookup_license(name: str, version: str | None, ecosystem: str, fetch=http_json) -> str | None:
    if ecosystem == "PyPI":
        info = fetch(f"https://pypi.org/pypi/{name}/{version + '/' if version else ''}json")["info"]
        raw = info.get("license_expression") or info.get("license")
        if not raw or len(raw) > 40:
            classifiers = [c for c in info.get("classifiers", []) if c.startswith("License ::")]
            raw = classifiers[-1].split("::")[-1].strip() if classifiers else raw
        return normalize_license(raw)
    if ecosystem == "npm":
        meta = fetch(f"https://registry.npmjs.org/{name}/{version or 'latest'}")
        lic = meta.get("license")
        return normalize_license(lic.get("type") if isinstance(lic, dict) else lic)
    return None


def lookup_vulns(name: str, version: str, ecosystem: str, fetch=http_json) -> list[str]:
    body = {"package": {"name": name, "ecosystem": ecosystem}, "version": version}
    return [v["id"] for v in fetch("https://api.osv.dev/v1/query", body).get("vulns", [])]


def evaluate(
    proposals: list[dict], guardrails: dict, ecosystems: list[str], fetch=http_json, offline: bool = False
) -> list[dict]:
    policy = guardrails.get("dependencies", {})
    licenses = set(policy.get("allowed_licenses", []))
    registries = set(policy.get("allowed_registries", []))
    results = []
    for prop in proposals:
        eco = prop.get("ecosystem") or (ecosystems[0] if ecosystems else "unknown")
        version = str(prop["version"]) if prop.get("version") else None
        rec = {
            "name": prop["name"],
            "version": version,
            "ecosystem": eco,
            "reason": prop.get("reason", ""),
            "license": None,
            "vulns": [],
            "status": "approved",
            "notes": [],
        }
        if eco not in registries:
            rec["status"] = "rejected"
            rec["notes"].append(f"registry '{eco}' not allowed")
        elif offline:
            rec["status"] = "needs_review"
            rec["notes"].append("offline: license/CVE not checked")
        else:
            try:
                rec["license"] = lookup_license(prop["name"], version, eco, fetch)
            except Exception as exc:  # network/registry errors degrade to human review
                rec["notes"].append(f"license lookup failed: {exc}")
            if rec["license"] is None:
                rec["status"] = "needs_review"
                rec["notes"].append("license unknown")
            elif rec["license"] not in licenses:
                rec["status"] = "rejected"
                rec["notes"].append(f"license {rec['license']} not allowed")
            if not version:
                rec["status"] = "rejected" if rec["status"] == "rejected" else "needs_review"
                rec["notes"].append("no pinned version; CVE check needs an exact version")
            else:
                try:
                    rec["vulns"] = lookup_vulns(prop["name"], version, eco, fetch)
                except Exception as exc:
                    rec["status"] = "rejected" if rec["status"] == "rejected" else "needs_review"
                    rec["notes"].append(f"CVE lookup failed: {exc}")
                if rec["vulns"] and policy.get("block_on_cve", True):
                    rec["status"] = "rejected"
                    rec["notes"].append(f"known vulnerabilities: {', '.join(rec['vulns'])}")
        results.append(rec)
    return results


def render(results: list[dict]) -> str:
    if not results:
        return "No new dependencies proposed.\n"
    lines = [
        "| Package | Version | Ecosystem | License | CVEs | Status | Notes |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['name']} | {r['version'] or '-'} | {r['ecosystem']} | {r['license'] or '?'} | "
            f"{', '.join(r['vulns']) or '-'} | {r['status']} | {'; '.join(r['notes']) or '-'} |"
        )
    return "\n".join(lines) + "\n"
