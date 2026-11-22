"""Stage registry: order, model tier, declared inputs/outputs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Stage:
    index: int
    name: str
    title: str
    tier: str
    requires: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()


STAGES: tuple[Stage, ...] = (
    Stage(0, "intake", "Intake + stack detection", "deterministic", (), (), ("requirement", "stack_profile")),
    Stage(
        1,
        "analyze",
        "Requirements analysis & clarification",
        "reasoning",
        ("requirement",),
        ("stack_profile",),
        ("spec",),
    ),
    Stage(2, "plan", "Planning / design", "reasoning", ("spec",), ("stack_profile",), ("plan", "dependencies")),
    Stage(3, "test", "Red tests (write-locked)", "coding", ("spec", "stack_profile"), ("plan",), ("test_lock",)),
    Stage(
        4,
        "implement",
        "Implementation",
        "coding",
        ("stack_profile",),
        ("spec", "plan", "test_lock"),
        ("implementation",),
    ),
    Stage(
        5,
        "verify",
        "Requirement verification",
        "reasoning",
        ("spec",),
        ("test_lock", "plan"),
        ("verification",),
    ),
    Stage(6, "review", "Code review", "reasoning", (), ("spec", "plan", "verification"), ("review",)),
    Stage(7, "scan", "Static analysis gate", "deterministic", ("stack_profile",), ("plan",), ("scan",)),
    Stage(8, "pr", "PR + post-PR loop", "fast", (), ("spec", "verification", "review", "scan"), ("pr",)),
    Stage(9, "report", "Run report", "deterministic", (), (), ()),
)

BY_NAME = {s.name: s for s in STAGES}
ALIASES = {
    "static": "scan",
    "static-analysis": "scan",
    "tests": "test",
    "red": "test",
    "impl": "implement",
    "verification": "verify",
    "analysis": "analyze",
    "design": "plan",
    "planning": "plan",
}
# Stages whose failure routes back to implementation (bounded by pipeline.max_loops).
LOOP_BACK = {"verify", "review", "scan", "pr"}


def get(name: str) -> Stage:
    key = ALIASES.get(name, name)
    if key not in BY_NAME:
        raise KeyError(f"unknown stage '{name}'; choose from {', '.join(BY_NAME)}")
    return BY_NAME[key]


def select(
    stage: str | None = None, start: str | None = None, end: str | None = None, only: list[str] | None = None
) -> list[Stage]:
    if stage:
        return [get(stage)]
    if only:
        return sorted({get(s) for s in only}, key=lambda s: s.index)
    lo = get(start).index if start else 0
    hi = get(end).index if end else STAGES[-1].index
    return [s for s in STAGES if lo <= s.index <= hi]
