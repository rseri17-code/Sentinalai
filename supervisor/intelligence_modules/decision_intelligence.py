"""DecisionIntelligence runner for the Intelligence Runtime.

**First deterministic Decision Intelligence layer.** Runs at POST_COLLECT
— after all six POST_CLASSIFY read modules have populated the classify
receipt with intelligence entries, and before the analyzer executes.

The runner is a pure transform:
    ctx.phase_receipts
        ↓ (IntelligenceContext.from_receipts)
    IntelligenceContext
        ↓ (DecisionContext.from_intelligence_context)
    DecisionContext

Absolutely no LLM is invoked. Absolutely no natural language is
generated. The runner only produces structured operational
recommendations that future guided-investigation / predictive
consumers can key off deterministically.

The result is written as compact receipt metadata under
``receipt.metadata["intelligence"]["decision_intelligence"]``. The
analyzer prompt is not modified. The evidence dict is not modified.
investigate() is not modified.

Feature-flag-gated: ``ENABLE_DECISION_INTELLIGENCE``. Default off.

Never raises. Runtime failure isolation catches internal errors.
"""
from __future__ import annotations

import logging
from typing import Any

from sentinel_core.runtime import (
    IntelligenceStage,
    ModuleSpec,
    RuntimeContext,
)

logger = logging.getLogger("sentinalai.intelligence_modules.decision_intelligence")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DECISION_INTELLIGENCE_FEATURE_FLAG = "ENABLE_DECISION_INTELLIGENCE"
DECISION_VERSION = 1


# ---------------------------------------------------------------------------
# ModuleSpec
# ---------------------------------------------------------------------------

DECISION_INTELLIGENCE_SPEC = ModuleSpec(
    name="decision_intelligence",
    stage=IntelligenceStage.POST_COLLECT,
    feature_flag=DECISION_INTELLIGENCE_FEATURE_FLAG,
    priority=100,
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def decision_intelligence_runner(ctx: RuntimeContext) -> dict[str, Any]:
    """Build a DecisionContext from ctx.phase_receipts.

    Returns receipt metadata containing the full DecisionContext dict
    (bounded), plus a compact ``decision_summary`` for downstream
    consumers that only need the top-level signals.

    Statuses:
        success — DecisionContext built (may be empty if no intelligence)
        skipped — no phase_receipts on ctx (empty runtime path)
        failed  — runtime-captured error
    """
    receipts = ctx.phase_receipts or ()
    if not receipts:
        return {
            "status":  "skipped",
            "reason":  "no_phase_receipts",
            "version": DECISION_VERSION,
        }

    # Lazy import so cycle risk stays contained
    from sentinel_core.models.decision_context import DecisionContext
    from sentinel_core.models.intel_context import IntelligenceContext

    intel_ctx = IntelligenceContext.from_receipts(receipts)
    decision = DecisionContext.from_intelligence_context(intel_ctx)

    # Compact summary — five keys downstream consumers can pattern-match on
    summary = {
        "confidence":             decision.confidence,
        "investigation_priority": decision.investigation_priority,
        "likely_failure_type":    decision.likely_failure_type,
        "recurring_incident":     decision.recurring_incident,
        "blast_radius_severity":  decision.likely_blast_radius.severity,
    }

    return {
        "status":            "success",
        "decision_context":  decision.to_dict(),
        "decision_summary":  summary,
        "evidence_gaps":     list(decision.evidence_gaps),
        "recommended_next_service": decision.recommended_next_service,
        "version":           DECISION_VERSION,
    }


__all__ = [
    "DECISION_INTELLIGENCE_SPEC",
    "DECISION_INTELLIGENCE_FEATURE_FLAG",
    "DECISION_VERSION",
    "decision_intelligence_runner",
]
