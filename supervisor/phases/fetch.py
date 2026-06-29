"""Phase: FETCH — incident loading, validation, initial context preparation.

First behavioral decomposition of supervisor.agent. This module owns ONLY
the sequence executed today inside investigate() between the trace_span
opening (line ~331) and the classification call (line ~395):

- per-investigation handle construction (receipts / budget / circuits / call_graph)
- thread-local investigation state reset
- INVESTIGATION_STARTED event emission
- the actual incident fetch (delegated to ``supervisor._fetch_incident``)
- empty-incident validation (delegated to ``supervisor._empty_result``)
- summary / service extraction from the loaded incident
- TLS caching of the current incident
- meta-query short-circuit detection

NOT in scope (kept inside investigate()):
- the replay short-circuit (lines 310-329) — runs BEFORE trace_span and calls
  _analyze_evidence; that's an analysis re-entry, not a fetch
- the trace_span("investigate") wrapper itself
- classification, enrichment, severity detection, playbook, analysis,
  persistence — all unchanged

Adapter pattern: FetchPhase holds a reference to the supervisor and calls
its existing methods (``_fetch_incident``, ``_empty_result``). No worker
or LLM logic is reimplemented here. The supervisor remains the single
source of truth for those routines until a later phase extracts them too.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from sentinel_core.context import InvestigationContext
from sentinel_core.evidence import EvidenceLedger
from supervisor.phases.contracts import PhaseOutput, PhaseResult, PhaseStatus

logger = logging.getLogger("sentinalai.fetch")


class FetchPhase:
    """Run the FETCH stage of an investigation.

    Construct with a reference to the supervisor — the phase delegates to
    its handles and helper methods rather than reimplementing them, so the
    extraction is byte-equivalent.

    Usage from investigate():

        with trace_span("investigate", case_id=incident_id) as span:
            span.set_attribute(GENAI_SYSTEM, "sentinalai")
            span.set_attribute(GENAI_OPERATION_NAME, "investigate")

            fetch_result = FetchPhase(self).execute(ctx)
            out = fetch_result.output.result
            if out["early_return"] is not None:
                return out["early_return"]

            incident = out["incident"]
            summary  = out["summary"]
            service  = out["service"]
            receipts = out["receipts"]
            budget   = out["budget"]
            circuits = out["circuits"]
            # ... continue with classify and onward ...
    """

    PHASE_NAME = "fetch"

    def __init__(self, supervisor: Any) -> None:
        """Hold the supervisor reference for adapter-style delegation.

        ``supervisor`` is a ``SentinalAISupervisor`` instance. Typed as Any
        here so this module does not need to import agent.py at module load
        (avoids any import-cycle surprise).
        """
        self._sup = supervisor

    def execute(
        self,
        ctx: InvestigationContext,
        ledger: EvidenceLedger | None = None,
    ) -> PhaseResult:
        """Execute the fetch stage.

        ``ctx`` must carry ``ctx.incident_id``. All other fields on ctx are
        treated as informational; fetch derives its own handles from the
        supervisor.

        ``ledger`` is accepted for symmetry with the phase contract; FETCH
        does not currently emit ledger items because the legacy code path
        does not produce evidence entries at this stage (the incident
        payload becomes ``self._tls.current_incident``, not an evidence key).

        Returns a PhaseResult whose ``output.result`` dict is:

            early_return : Optional[dict]    -- if set, investigate() returns this
            incident     : Optional[dict]    -- normalized incident or None
            summary      : str               -- incident summary text
            service      : str               -- affected service (default "unknown")
            receipts     : ReceiptCollector
            budget       : ExecutionBudget
            circuits     : CircuitBreakerRegistry
            call_graph   : CallGraph

        Status is always COMPLETED on a normal return; FAILED is reserved
        for unexpected exceptions surfaced as PhaseResult.error.
        """
        # Imports here keep this module's top-level import graph tight and
        # avoid any chance of a cycle with supervisor.agent.
        from supervisor.guardrails import CircuitBreakerRegistry, ExecutionBudget
        from supervisor.llm_call_graph import CallGraph, set_current_graph
        from supervisor.progress_stream import EventType, get_stream
        from supervisor.receipt import ReceiptCollector
        from supervisor.tool_selector import is_meta_query

        sup = self._sup
        incident_id = ctx.incident_id

        # --- TLS reset (mirrors investigate() lines 339-344) ---
        sup._tls.investigation_deadline = (
            time.monotonic() + sup.INVESTIGATION_DEADLINE_SECONDS
        )
        sup._tls.current_incident = None
        sup._tls.itsm_evidence = None
        sup._tls.devops_evidence = None
        sup._tls.current_investigation_id = incident_id
        sup._tls.current_phase = "collect"

        # --- LLM call-graph init (line 347-348) ---
        call_graph = CallGraph(investigation_id=incident_id)
        set_current_graph(call_graph)

        # --- Per-investigation handles (lines 351-358) ---
        receipts = ReceiptCollector(case_id=incident_id)
        budget = ExecutionBudget(case_id=incident_id)
        circuits = CircuitBreakerRegistry()

        # --- Start log + event (lines 360-365) ---
        logger.info("Starting investigation for %s", incident_id)
        _stream = get_stream()
        _stream.emit(
            incident_id,
            EventType.INVESTIGATION_STARTED,
            {"incident_id": incident_id},
            phase="start",
        )

        # --- Step 1: Fetch incident (line 368) ---
        incident = sup._fetch_incident(incident_id, receipts, budget, circuits)

        # --- Empty-incident guard (lines 369-371) ---
        if not incident:
            logger.warning("No incident data for %s", incident_id)
            empty = sup._empty_result(incident_id, "No incident data available")
            return self._result(
                early_return=empty,
                incident=None, summary="", service="",
                receipts=receipts, budget=budget, circuits=circuits,
                call_graph=call_graph,
            )

        # --- Extract summary / service (lines 373-374) ---
        summary = incident.get("summary", "")
        service = incident.get("affected_service", "unknown")

        # --- TLS cache for time-anchoring downstream (line 377) ---
        sup._tls.current_incident = incident

        # --- Meta-query short-circuit (lines 380-393) ---
        if is_meta_query(summary):
            logger.info("Meta-query detected for %s, skipping investigation", incident_id)
            meta_result = {
                "incident_id": incident_id,
                "root_cause": "META_QUERY_NOT_INCIDENT",
                "confidence": 0,
                "evidence_timeline": [],
                "reasoning": (
                    f"Input appears to be a question rather than an active incident: "
                    f"'{summary[:200]}'. "
                    "Please provide an incident summary describing a failure condition "
                    "(e.g., 'payments-api returning 5xx errors', 'checkout OOMKilled in prod')."
                ),
            }
            return self._result(
                early_return=meta_result,
                incident=incident, summary=summary, service=service,
                receipts=receipts, budget=budget, circuits=circuits,
                call_graph=call_graph,
            )

        # --- Normal path: caller should continue with classify and onward ---
        return self._result(
            early_return=None,
            incident=incident, summary=summary, service=service,
            receipts=receipts, budget=budget, circuits=circuits,
            call_graph=call_graph,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _result(
        self,
        *,
        early_return: Any,
        incident: Any,
        summary: str,
        service: str,
        receipts: Any,
        budget: Any,
        circuits: Any,
        call_graph: Any,
    ) -> PhaseResult:
        """Wrap the fetch outputs in a PhaseResult."""
        return PhaseResult(
            phase=self.PHASE_NAME,
            status=PhaseStatus.COMPLETED,
            output=PhaseOutput(
                result={
                    "early_return": early_return,
                    "incident":     incident,
                    "summary":      summary,
                    "service":      service,
                    "receipts":     receipts,
                    "budget":       budget,
                    "circuits":     circuits,
                    "call_graph":   call_graph,
                },
            ),
        )


__all__ = ["FetchPhase"]
