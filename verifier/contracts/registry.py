"""Resolves stub vs. real per module from VERIFIER_REAL (design doc §8.1).

``get(name)`` reads VERIFIER_REAL on every call (tests can flip it) and imports real
modules lazily, so a missing real module never breaks a stub run.
"""
from __future__ import annotations

import importlib
from types import ModuleType, SimpleNamespace

from verifier.contracts.config import env

# Henry: llm, extract, slack, sandbox, agent, judge. Jake: resume, fetch, rubric, render,
# annotate, ats, harness. Always real (not in the registry): store, snapshot, generate,
# pipeline, service.
NAMES: tuple[str, ...] = (
    "llm", "extract", "slack", "sandbox", "agent", "judge",
    "resume", "fetch", "rubric", "render", "annotate", "ats", "harness",
)

_REAL_PATHS: dict[str, str] = {
    **{n: f"verifier.stack.{n}" for n in ("llm", "extract", "slack", "agent", "judge")},
    **{n: f"verifier.core.{n}" for n in ("resume", "fetch", "rubric", "render", "annotate", "harness")},
    "ats": "verifier.ats.adapter",
}


def _real_path(name: str) -> str:
    if name == "sandbox":
        return ("verifier.stack.sandbox_openshell" if env("SANDBOX_BACKEND") == "openshell"
                else "verifier.core.harness")
    return _REAL_PATHS[name]


def real_modules() -> list[str]:
    """Registry names currently resolving to real modules (for /internal/health)."""
    raw = [p.strip() for p in env("VERIFIER_REAL").split(",") if p.strip()]
    if "all" in raw:
        return list(NAMES)
    return [n for n in NAMES if n in raw]


def get(name: str) -> ModuleType | SimpleNamespace:
    """The real module if `name` is in VERIFIER_REAL (or it is `all`), else the stub."""
    if name not in NAMES:
        raise ValueError(f"unknown registry name {name!r}; expected one of {', '.join(NAMES)}")
    if name in real_modules():
        return importlib.import_module(_real_path(name))
    from verifier.contracts import stubs
    return getattr(stubs, name)
