"""Model tier routing (models.yaml) and cost estimation."""

from __future__ import annotations

import os
from dataclasses import dataclass

from aidlc.config import Config


class ModelConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ModelChoice:
    tier: str
    provider: str
    model: str


def resolve(cfg: Config, stage: str) -> ModelChoice:
    tiers: dict = cfg.models.get("tiers", {})
    spec = cfg.models.get("stages", {}).get(stage)
    if spec is None:
        raise ModelConfigError(f"models.yaml has no route for stage '{stage}'")
    if isinstance(spec, str):
        if spec not in tiers:
            raise ModelConfigError(f"stage '{stage}' routes to unknown tier '{spec}'")
        return ModelChoice(spec, tiers[spec]["provider"], tiers[spec]["model"])
    tier = spec.get("tier", "custom")
    base = tiers.get(tier, {})
    return ModelChoice(tier, spec.get("provider", base.get("provider")), spec.get("model", base.get("model")))


WRITABLE_STAGES = ("test", "implement")


def isolation_errors(cfg: Config, stages: list[str]) -> list[str]:
    """Writing stages run agent-written code (tests, build scripts), so the command guard cannot contain
    them; they need an isolated runtime. Codex always does (no pre-tool hooks); other providers unless
    `harness.require_isolation` is false."""
    if cfg.get("harness.isolated") or os.environ.get("AIDLC_ISOLATED") == "1":
        return []
    required = cfg.get("harness.require_isolation", True)
    errors = []
    for stage in stages:
        if stage not in WRITABLE_STAGES:
            continue
        provider = resolve(cfg, stage).provider
        if provider == "codex":
            errors.append(
                f"stage '{stage}' routes to codex, which has no pre-tool guard hooks; route it to claude "
                "or run agents in the aidlc container (harness.isolated)"
            )
        elif required and provider != "fake":
            errors.append(
                f"stage '{stage}' runs agent-written code; run it in the aidlc container "
                "(scripts/aidlc-container.sh) or set harness.require_isolation: false for a trusted local setup"
            )
    return errors


def validate(cfg: Config) -> list[str]:
    """Return config errors; enforces implementer != reviewer."""
    errors = []
    for stage in cfg.models.get("stages", {}):
        try:
            resolve(cfg, stage)
        except ModelConfigError as exc:
            errors.append(str(exc))
    if errors:
        return errors
    rules = cfg.models.get("rules", {})
    if rules.get("reviewer_must_differ_from_implementer", True):
        impl, rev = resolve(cfg, "implement"), resolve(cfg, "review")
        if (impl.provider, impl.model) == (rev.provider, rev.model):
            hint = " (Copilot Free only offers 'auto': set rules.reviewer_must_differ_from_implementer: false)"
            errors.append(
                f"review must use a different model than implement (both {impl.model})"
                + (hint if impl.model == "auto" else "")
            )
    return errors


def estimate_cost(cfg: Config, model: str, tokens_in: int, tokens_out: int) -> float:
    price = cfg.models.get("pricing_usd_per_mtok", {}).get(model)
    if not price:
        return 0.0
    return round((tokens_in * price.get("input", 0) + tokens_out * price.get("output", 0)) / 1_000_000, 6)
