"""Planner runtime module — plugs the deterministic planner into the
IntelligenceRuntime at POST_PERSIST.

The runner:
1. Reads ``ctx.phase_receipts`` (populated by supervisor.agent).
2. Builds IntelligenceContext, DecisionContext, and KnowledgeGraph
   from the receipts + result — reusing the canonical libraries
   already in ``sentinel_core.models``.
3. Constructs a PlanContext.
4. Invokes :class:`PlannerBuilder` to produce an :class:`InvestigationPlan`.
5. Returns the plan (+ a compact summary) on receipt metadata.

The runner NEVER modifies ``ctx``, NEVER invokes the LLM, NEVER touches
any store, and returns ``skipped`` when the feature flag is off or when
no phase receipts are available.

Feature flag: ``ENABLE_PLANNER``. Default off.
Priority 950 at POST_PERSIST — after resolution_memory (100),
investigation_store (200), intelligence_context_persister (800), and
enterprise_knowledge_graph (900).
"""
from __future__ import annotations

import logging
from typing import Any

from sentinel_core.runtime import (
    IntelligenceStage,
    ModuleSpec,
    RuntimeContext,
)

logger = logging.getLogger("sentinalai.deterministic_planner.runtime")


PLANNER_FEATURE_FLAG = "ENABLE_PLANNER"
RUNTIME_VERSION = 1


PLANNER_SPEC = ModuleSpec(
    name="planner",
    stage=IntelligenceStage.POST_PERSIST,
    feature_flag=PLANNER_FEATURE_FLAG,
    priority=950,
)


def planner_runner(ctx: RuntimeContext) -> dict[str, Any]:
    """Build a deterministic InvestigationPlan from ``ctx.phase_receipts``.

    Returns:
        {status, plan, plan_summary, version}

    Statuses:
        success — plan built (possibly empty if no signals)
        skipped — no phase_receipts
        failed  — runtime-captured error
    """
    receipts = ctx.phase_receipts or ()
    if not receipts:
        return {
            "status":  "skipped",
            "reason":  "no_phase_receipts",
            "version": RUNTIME_VERSION,
        }

    # Lazy imports keep cycle risk contained + honour the mission rule
    # that the planner never invokes anything at module import time.
    from sentinel_core.models.decision_context import DecisionContext
    from sentinel_core.models.intel_context import IntelligenceContext
    from sentinel_core.models.knowledge_graph import KnowledgeGraphBuilder
    from sentinel_core.models.plan_context import PlanContext
    from supervisor.deterministic_planner.planner_builder import PlannerBuilder

    intel_ctx = IntelligenceContext.from_receipts(receipts)
    decision = DecisionContext.from_intelligence_context(intel_ctx)

    incident_id = _extract_incident_id(ctx)
    service = decision.service or _extract_service(ctx)
    incident_type = decision.incident_type or _extract_incident_type(ctx)
    root_cause, remediation = _extract_result_fields(ctx)

    kg = KnowledgeGraphBuilder.from_intelligence_context(
        intel_ctx,
        incident_id=incident_id,
        service=service,
        incident_type=incident_type,
        root_cause=root_cause,
        remediation_action=remediation,
    )

    pc = PlanContext(
        service=service,
        incident_type=incident_type,
        decision_context=decision,
        knowledge_graph=kg,
        receipts=tuple(receipts),
        current_confidence=int(decision.confidence),
        target_confidence=80,
    )

    plan = PlannerBuilder().build(pc)

    return {
        "status":  "success",
        "service": service,
        "incident_id": incident_id,
        "plan":    plan.to_dict(),
        "plan_summary": {
            "plan_id":                    plan.plan_id,
            "goal_count":                 plan.goal_count(),
            "step_count":                 plan.step_count(),
            "estimated_total_cost":       plan.estimated_total_cost,
            "estimated_total_latency_ms": plan.estimated_total_latency_ms,
            "initial_confidence":         plan.initial_confidence,
            "target_confidence":          plan.target_confidence,
            "final_confidence":           plan.final_confidence(),
        },
        "version": RUNTIME_VERSION,
    }


# ---------------------------------------------------------------------------
# Context extractors — copies of the persister's helpers so we don't
# introduce a dependency cycle between the two runtime modules.
# ---------------------------------------------------------------------------

def _extract_incident_id(ctx: RuntimeContext) -> str:
    if not (ctx.fetch_out and isinstance(ctx.fetch_out, dict)):
        return ""
    incident = ctx.fetch_out.get("incident")
    if isinstance(incident, dict):
        v = incident.get("incident_id") or ""
        if v:
            return str(v)
    return ""


def _extract_service(ctx: RuntimeContext) -> str:
    if ctx.fetch_out and isinstance(ctx.fetch_out, dict):
        v = ctx.fetch_out.get("service", "")
        if v:
            return str(v)
    return ""


def _extract_incident_type(ctx: RuntimeContext) -> str:
    if ctx.cres is not None:
        v = getattr(ctx.cres, "incident_type", "")
        if v:
            return str(v)
    return ""


def _extract_result_fields(ctx: RuntimeContext) -> tuple[str, str]:
    result = ctx.result if isinstance(ctx.result, dict) else {}
    root_cause = str(result.get("root_cause", "") or "")
    remediation = result.get("remediation") or {}
    if isinstance(remediation, dict):
        action = str(remediation.get("immediate_action", "") or "")
    else:
        action = ""
    return root_cause, action


__all__ = [
    "PLANNER_SPEC",
    "PLANNER_FEATURE_FLAG",
    "RUNTIME_VERSION",
    "planner_runner",
]
