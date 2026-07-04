"""InvestigationStore runner for the Intelligence Runtime.

Wires the existing ``intelligence.investigation_store`` module (and its
EvidenceGraph companion) into the Phase 19 Intelligence Runtime at
``POST_PERSIST``. No changes to InvestigationStore or EvidenceGraph
themselves — the runner is a thin adapter that:

1. Reads the completed investigation from ``RuntimeContext`` (fetch_out,
   cres, aout, result).
2. Deduplicates by file existence — the store keys each investigation to
   ``{INVESTIGATIONS_DIR}/{investigation_id}.json`` so an existing file
   is a sufficient dedup key.
3. Builds a minimal EvidenceGraph envelope containing a single OUTCOME
   node whose content carries the result summary + cross-references to
   the sibling artifacts (DecisionTrace + ResolutionMemory).
4. Delegates persistence to ``InvestigationStore.save_graph()`` verbatim.

Feature-flag-gated: registered under ``ENABLE_INVESTIGATION_STORE_WRITE``.
Declared dependency on ``resolution_memory`` (Phase 20) so ordering
guarantees ResolutionMemory has already written its record — the runner
can then reference it by ID.

Never raises. Any failure inside the runner is caught by the runtime's
failure isolation and reported on the ModuleResult.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from sentinel_core.runtime import (
    IntelligenceStage,
    ModuleSpec,
    RuntimeContext,
)

logger = logging.getLogger("sentinalai.intelligence_modules.investigation_store")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INVESTIGATION_STORE_FEATURE_FLAG = "ENABLE_INVESTIGATION_STORE_WRITE"
WRITE_VERSION = 1

# Same skip semantics as Phase 20 — non-actionable outcomes aren't worth
# an envelope.
_SKIP_PREFIXES = ("INSUFFICIENT", "META_QUERY", "BLOCKED", "LOW CONFIDENCE")


# ---------------------------------------------------------------------------
# ModuleSpec — declarative registration
# ---------------------------------------------------------------------------

INVESTIGATION_STORE_SPEC = ModuleSpec(
    name="investigation_store",
    stage=IntelligenceStage.POST_PERSIST,
    feature_flag=INVESTIGATION_STORE_FEATURE_FLAG,
    priority=200,                       # runs AFTER resolution_memory (priority=100)
    dependencies=("resolution_memory",),
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def investigation_store_runner(ctx: RuntimeContext) -> dict[str, Any]:
    """Persist a candidate InvestigationStore envelope for one completed
    investigation.

    Runs at POST_PERSIST after ResolutionMemory (declared dependency), so
    the resolution_memory_id cross-reference is derivable via a store query.

    Returns receipt metadata:
        {status, record_id, deduplicated, graph_path,
         decision_trace_id, resolution_memory_id, version}

    Statuses:
        success       — new graph envelope written
        deduplicated  — existing envelope found for this investigation_id
        skipped       — non-actionable root cause; nothing written
        failed        — internal error (runtime captures error_type)
    """
    result = ctx.result or {}
    root_cause = str(result.get("root_cause", "") or "").strip()
    if not root_cause or root_cause.startswith(_SKIP_PREFIXES):
        return {
            "status":  "skipped",
            "reason":  "no_actionable_root_cause",
            "version": WRITE_VERSION,
        }

    fetch_out = ctx.fetch_out or {}
    incident = fetch_out.get("incident") if isinstance(fetch_out, dict) else None
    incident_id = ""
    if isinstance(incident, dict):
        incident_id = str(incident.get("incident_id") or "")

    service = ""
    if isinstance(fetch_out, dict):
        service = str(fetch_out.get("service", "") or "")

    incident_type = ""
    if ctx.cres is not None:
        incident_type = str(getattr(ctx.cres, "incident_type", "") or "")

    dir_path = os.environ.get("INVESTIGATIONS_DIR", "eval/investigations")

    # Dedup by graph-file existence. The store keys each investigation to
    # ``{dir}/{investigation_id}.json``; an existing file is a stable
    # deterministic identity signal.
    graph_path = os.path.join(dir_path, f"{ctx.investigation_id}.json")
    if os.path.exists(graph_path):
        return {
            "status":       "deduplicated",
            "record_id":    ctx.investigation_id,
            "deduplicated": True,
            "graph_path":   graph_path,
            "version":      WRITE_VERSION,
        }

    # Cross-refs — read at runtime; missing values become empty strings.
    decision_trace_id = _extract_decision_trace_id(ctx)
    resolution_memory_id = _find_resolution_memory_id(
        investigation_id=ctx.investigation_id,
        service=service,
        incident_type=incident_type,
    )

    from intelligence.evidence_graph import EvidenceGraph, EvidenceNode
    from intelligence.investigation_store import InvestigationStore
    from intelligence.schema import (
        EntityType,
        InvestigationPhase,
        NodeType,
    )

    graph = EvidenceGraph(
        investigation_id=ctx.investigation_id,
        incident_id=incident_id,
        service=service,
        incident_type=incident_type,
        phase=InvestigationPhase.RESOLVED,
    )

    # One envelope node carrying the result summary + cross-references.
    outcome_node = EvidenceNode.make(
        source_type="investigate",
        node_type=NodeType.OUTCOME,
        entity_id=service or ctx.investigation_id,
        entity_type=EntityType.SERVICE,
        investigation_id=ctx.investigation_id,
        content={
            "root_cause":          str(result.get("root_cause", ""))[:2000],
            "confidence":          int(result.get("confidence", 0) or 0),
            "reasoning":           str(result.get("reasoning", ""))[:2000],
            "evidence_snapshot_keys": sorted(
                (result.get("_evidence_snapshot") or {}).keys()
            ),
            "remediation":         result.get("remediation", {}) or {},
            "decision_trace_id":   decision_trace_id,
            "resolution_memory_id": resolution_memory_id,
            "citation_coverage":   result.get("citation_coverage", 0.0),
            "fix_proposed":        bool(result.get("proposed_fix")),
        },
    )
    graph.add_node(outcome_node)

    store = InvestigationStore(investigations_dir=dir_path)
    store.save_graph(graph)

    return {
        "status":               "success",
        "record_id":            ctx.investigation_id,
        "deduplicated":         False,
        "graph_path":           graph_path,
        "decision_trace_id":    decision_trace_id,
        "resolution_memory_id": resolution_memory_id,
        "version":              WRITE_VERSION,
    }


# ---------------------------------------------------------------------------
# Cross-reference helpers
# ---------------------------------------------------------------------------

def _extract_decision_trace_id(ctx: RuntimeContext) -> str:
    """Return the decision_trace_id from AnalyzeResult, or ""."""
    if ctx.aout is None:
        return ""
    meta = getattr(ctx.aout, "decision_trace_meta", None)
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("trace_id", "") or "")


def _find_resolution_memory_id(
    *,
    investigation_id: str,
    service: str = "",
    incident_type: str = "",
) -> str:
    """Return the ResolutionMemory record_id for this investigation, or "".

    Reuses the Phase 20 store query pattern. Returns "" on any failure so
    the InvestigationStore write path doesn't depend on ResolutionMemory
    being available.
    """
    try:
        from intelligence.resolution_memory import ResolutionMemoryStore
        db_path = os.environ.get("OPS_DB_PATH", "eval/ops_intelligence.db")
        candidates = ResolutionMemoryStore(db_path).query(
            service=service or None,
            incident_type=incident_type or None,
            limit=200,
        )
        for m in candidates:
            if m.investigation_id == investigation_id:
                return m.memory_id
    except Exception as exc:
        logger.debug("investigation_store: RM lookup failed: %s", exc)
    return ""


__all__ = [
    "INVESTIGATION_STORE_SPEC",
    "INVESTIGATION_STORE_FEATURE_FLAG",
    "WRITE_VERSION",
    "investigation_store_runner",
]
