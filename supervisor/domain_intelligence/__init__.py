"""Domain Intelligence Engine (Phase 6).

A registry of Domain Intelligence Modules. Each module is flag-gated (default off),
so with no module enabled ``run_domain_modules`` is a no-op and engine behavior is
byte-identical. New domains are added by registering a module here — no engine
rewrite, no analyzer-per-failure-mode sprawl.
"""
from __future__ import annotations

from supervisor.domain_intelligence.base import DomainModule, EvidenceView
from supervisor.domain_intelligence.database import DatabaseIntelligence

# Registered modules (one per operational domain). Order is stable → deterministic.
_MODULES: list[DomainModule] = [
    DatabaseIntelligence(),
]


def enabled_modules() -> list[DomainModule]:
    return [m for m in _MODULES if m.enabled()]


def run_domain_modules(view: EvidenceView) -> list[dict]:
    """Collect hypotheses from every enabled domain module (deterministic order)."""
    out: list[dict] = []
    for m in enabled_modules():
        try:
            out.extend(m.analyze(view))
        except Exception:  # a module must never break the investigation
            continue
    return out


__all__ = ["DomainModule", "EvidenceView", "enabled_modules", "run_domain_modules"]
