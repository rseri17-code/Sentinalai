"""Phase: CLASSIFY — incident type, severity, and deferred enrichment.

Second behavioral decomposition of supervisor.agent. This module owns the
sequence executed today inside investigate() between FetchPhase return and
the COLLECT phase invocation (lines ~364-409 in the post-Phase-10 file).

Responsibilities:
- ``classify_incident()`` on the fetched summary
- ITSM context enrichment (delegates to ``supervisor._fetch_itsm_context``)
- Confluence context enrichment (delegates to ``supervisor._fetch_confluence_context``)
- Severity detection (``detect_severity``) and budget rescaling
  (``get_budget_for_severity``) — replaces the budget handle the FetchPhase
  constructed, carrying forward calls already recorded by receipts
- Deferred-future creation: experience lookup, knowledge-graph lookup,
  historical-context lookup — all submitted to ``self._sup._parallel_executor``
  to start work in parallel with the upcoming COLLECT phase

NOT owned (caller responsibility):
- the ``trace_span("investigate")`` itself (held by investigate())
- span.set_attribute observability calls — the phase RETURNS the values
  and investigate() sets them; that keeps the phase free of OTEL surface
- ``_stream.emit_phase("classify", ...)`` — investigate() owns the stream
  so it can emit "collect" and onward immediately after
- COLLECT, ANALYZE, PERSIST — unchanged

Adapter pattern: ClassificationPhase holds a supervisor reference and
calls existing helpers (``_fetch_itsm_context``,
``_fetch_confluence_context``, ``_fetch_historical_context``,
``_parallel_executor``). No worker or LLM logic is reimplemented.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from sentinel_core.context import InvestigationContext
from sentinel_core.evidence import EvidenceLedger
from supervisor.phases.contracts import PhaseOutput, PhaseResult, PhaseStatus

logger = logging.getLogger("sentinalai.classify")


# ---------------------------------------------------------------------------
# Typed result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClassificationResult:
    """All values produced by the CLASSIFY phase.

    Downstream code reads only this object's fields — it must NOT reach into
    ClassificationPhase internals. The phase guarantees:

    - ``incident_type`` is the string emitted by ``classify_incident()``
    - ``severity`` is a ``supervisor.severity.Severity`` instance
    - ``budget`` is the SEVERITY-SCALED ExecutionBudget — replaces whatever
      budget the caller had before; ``calls_made`` is pre-loaded with the
      ``receipts.summary()["total_calls"]`` count to carry forward FETCH +
      enrichment usage
    - ``itsm_context`` / ``confluence_context`` may be ``None`` if the
      respective worker is unavailable or returns nothing
    - the three futures are always present (never None); ``concurrent.
      futures.Future`` instances submitted to the supervisor's parallel
      executor. Caller awaits them downstream.
    """
    incident_type: str
    severity: Any                                   # supervisor.severity.Severity
    budget: Any                                     # supervisor.guardrails.ExecutionBudget
    itsm_context: Optional[dict[str, Any]]
    confluence_context: Optional[dict[str, Any]]
    experience_future: Any                          # concurrent.futures.Future
    kg_future: Any                                  # concurrent.futures.Future
    historical_future: Any                          # concurrent.futures.Future

    def metadata(self) -> dict[str, Any]:
        """Lightweight serializable view (drops futures and large dicts)."""
        return {
            "incident_type":           self.incident_type,
            "severity_level":          getattr(self.severity, "level",  None),
            "severity_label":          getattr(self.severity, "label",  None),
            "severity_source":         getattr(self.severity, "source", None),
            "severity_budget":         getattr(self.severity, "budget", None),
            "itsm_has_context":        self.itsm_context is not None,
            "confluence_has_context":  self.confluence_context is not None,
        }


# ---------------------------------------------------------------------------
# Phase
# ---------------------------------------------------------------------------

class ClassificationPhase:
    """Run the CLASSIFY stage of an investigation.

    Adapter pattern: holds a reference to the supervisor and delegates to
    its existing enrichment helpers. The output is a ``ClassificationResult``
    bundled inside a ``PhaseResult`` so the supervisor pipeline stays
    contract-clean.

    Usage from investigate() (after FetchPhase):

        from supervisor.phases.classify import ClassificationPhase
        classify_result = ClassificationPhase(self).execute(ctx, fetch_out)
        cres = classify_result.output.result["classification"]
        incident_type     = cres.incident_type
        severity          = cres.severity
        budget            = cres.budget
        itsm_context      = cres.itsm_context
        confluence_context = cres.confluence_context
        experience_future = cres.experience_future
        kg_future         = cres.kg_future
        historical_future = cres.historical_future
    """

    PHASE_NAME = "classify"

    def __init__(self, supervisor: Any) -> None:
        self._sup = supervisor

    def execute(
        self,
        ctx: InvestigationContext,
        fetch_out: dict[str, Any],
        ledger: EvidenceLedger | None = None,
        span: Any = None,
    ) -> PhaseResult:
        """Execute the classify stage.

        ``fetch_out`` is ``FetchPhase.execute(ctx).output.result`` — the dict
        returned by Phase 10. Specifically requires:
          - ``incident``  (dict)
          - ``summary``   (str)
          - ``service``   (str)
          - ``receipts``  (ReceiptCollector)
          - ``budget``    (ExecutionBudget — will be REPLACED)
          - ``circuits``  (CircuitBreakerRegistry)

        ``ledger`` is accepted for symmetry; CLASSIFY does not emit ledger
        items in the legacy code path.

        ``span`` is the supervisor's investigation trace span; when provided
        the phase sets the incident_type / service / severity attributes on
        it at the SAME moments the legacy code did (so observability output
        is byte-equivalent). When None (tests), attribute setting is skipped.

        Returns a PhaseResult with status=COMPLETED. The actual values are
        in ``output.result["classification"]`` as a ``ClassificationResult``.
        """
        # Imports here keep this module's top-level surface tight and avoid
        # any import-cycle surprise with supervisor.agent.
        from supervisor.experience_store import retrieve_similar as _retrieve_experiences
        from supervisor.knowledge_graph import query_similar as _kg_query_similar
        from supervisor.observability import EVAL_INCIDENT_TYPE, EVAL_SERVICE
        from supervisor.progress_stream import get_stream
        from supervisor.severity import detect_severity, get_budget_for_severity
        from supervisor.tool_selector import classify_incident

        sup = self._sup
        incident_id = ctx.incident_id
        incident   = fetch_out["incident"]
        summary    = fetch_out["summary"]
        service    = fetch_out["service"]
        receipts   = fetch_out["receipts"]
        circuits   = fetch_out["circuits"]
        # NOTE: do NOT use fetch_out["budget"] for downstream — it gets
        # replaced after severity detection. We do use it for the two
        # enrichment calls below so their accounting flows into receipts
        # before we rescale.
        pre_scale_budget = fetch_out["budget"]

        # --- Classify + immediate observability (legacy ordering) ---
        # The original investigate() emitted the "classify" phase event and
        # set incident_type/service span attrs RIGHT AFTER classify_incident,
        # BEFORE ITSM/Confluence/severity. Preserve that order exactly so
        # any synchronous stream subscriber sees the same sequence.
        incident_type = classify_incident(summary)
        if span is not None:
            span.set_attribute(EVAL_INCIDENT_TYPE, incident_type)
            span.set_attribute(EVAL_SERVICE, service)
        logger.info(
            "Classified %s as %s (service=%s)",
            incident_id, incident_type, service,
        )
        get_stream().emit_phase(
            incident_id, "classify",
            incident_type=incident_type, service=service,
        )

        # --- ITSM context (Phase 1 hydration) ---
        itsm_context = sup._fetch_itsm_context(
            service, summary, receipts, pre_scale_budget, circuits,
        )

        # --- Confluence context (Phase 1 hydration) ---
        confluence_context = sup._fetch_confluence_context(
            service, summary, incident_type, receipts, pre_scale_budget, circuits,
        )

        # --- Severity detection + budget scaling ---
        # Severity is detected from incident + itsm_context. The budget is
        # then replaced with the severity-appropriate one; we preserve
        # case_id and carry forward calls already recorded so the new budget
        # accounts for FETCH + enrichment usage.
        severity = detect_severity(incident, itsm_context)
        budget = get_budget_for_severity(severity)
        budget.case_id = incident_id
        budget.calls_made = receipts.summary()["total_calls"]
        if span is not None:
            span.set_attribute("sentinalai.severity_level",  severity.level)
            span.set_attribute("sentinalai.severity_label",  severity.label)
            span.set_attribute("sentinalai.severity_source", severity.source)
        logger.info(
            "Severity: level=%d label=%s source=%s budget=%d",
            severity.level, severity.label, severity.source, severity.budget,
        )

        # --- Deferred enrichment futures ---
        # Submitted now so they run in parallel with the upcoming COLLECT
        # phase. Awaited by investigate() after COLLECT returns.
        experience_future = sup._parallel_executor.submit(
            _retrieve_experiences, incident_type, service,
        )
        kg_future = sup._parallel_executor.submit(
            _kg_query_similar, service, incident_type,
        )
        historical_future = sup._parallel_executor.submit(
            sup._fetch_historical_context,
            service, summary, incident_type, receipts, budget, circuits,
        )

        cres = ClassificationResult(
            incident_type=incident_type,
            severity=severity,
            budget=budget,
            itsm_context=itsm_context,
            confluence_context=confluence_context,
            experience_future=experience_future,
            kg_future=kg_future,
            historical_future=historical_future,
        )

        return PhaseResult(
            phase=self.PHASE_NAME,
            status=PhaseStatus.COMPLETED,
            output=PhaseOutput(result={"classification": cres}),
        )


__all__ = ["ClassificationPhase", "ClassificationResult"]
