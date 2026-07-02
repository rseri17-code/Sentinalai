"""Phase: ANALYZE — hypothesis selection, calibration, critique, citation, gates.

Fourth behavioral decomposition of supervisor.agent. Owns the sequence
executed today inside investigate() between CollectPhase return and the
Observability / Persist phases (current investigate() lines ~397-530).

Responsibilities (PRESENT in legacy code):
- deadline check + early return when investigation_deadline exceeded
- "analyze" stream phase emit
- TLS cache of evidence + incident_type (so harness reanalyze() can reuse)
- _analyze_evidence (delegated) inside the analyze_evidence trace span
- attach _gate_post_collection to result (the gate object came from COLLECT)
- confidence calibration (calibrator singleton) + emit_confidence
- pop _hypothesis_count / _winner_hypothesis / _llm_metrics from result
- recurrence_check (graceful when supervisor.recurrence_tracker is absent)
- grounding_score + emit_confidence("grounding_*") (graceful when absent)
- _apply_self_critique (delegated; may mutate result + evidence + confidence)
- annotate_citations
- check_post_analysis gates G2+G3+G5 (may BLOCK by rewriting root_cause)
- _online_evaluate + _annotate_online
- _evidence_snapshot construction
- git_blame_pinpoint extraction (when evidence["git_blame"] present)
- causal_change extraction (top ITSM correlation with score >= 0.45)

NOT owned (stays in investigate()):
- the outer trace_span("investigate") wrapper itself
- _record_observability (Observe phase per legacy comment)
- _run_judge_scoring + judge phase
- generate_remediation
- proposed_fix + git-incident link + verification_loop
- record_investigation_outcome (metrics dashboard)
- _persist_results
- emit_complete

Adapter pattern: AnalyzePhase holds a supervisor reference and delegates
to its existing helpers (_analyze_evidence, _apply_self_critique,
_empty_result). All other dependencies are pure module-level functions
(get_calibrator, annotate_citations, check_post_analysis, _online_evaluate,
_annotate_online, _grounding_score, _recurrence_check). No worker / LLM
logic is reimplemented.

Evidence dict authority: the legacy dict remains the source of truth.
EvidenceLedger ownership unchanged — analyze does not write to a ledger.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from sentinel_core.context import InvestigationContext
from sentinel_core.evidence import EvidenceLedger
from supervisor.phases.contracts import PhaseOutput, PhaseResult, PhaseStatus

logger = logging.getLogger("sentinalai.analyze")


# ---------------------------------------------------------------------------
# Typed result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AnalyzeResult:
    """All values produced by the ANALYZE phase.

    - ``result``: the mutated investigation-result dict, ready for the
      Observability / Persist phases.
    - ``evidence``: the evidence dict — may have been mutated in-place by
      self_critique's gap-evidence merge.
    - ``confidence``: the final post-calibration, post-critique confidence
      (integer 0-100).
    - ``hypothesis_count`` / ``winner_hypothesis`` / ``llm_metrics``: values
      popped from ``result`` for downstream observability + persist.
    - ``early_return``: populated when the investigation deadline was
      exceeded before analysis started. Caller returns this dict immediately.
    """
    result: dict[str, Any]
    evidence: dict[str, Any]
    confidence: int = 0
    hypothesis_count: int = 0
    winner_hypothesis: str = "none"
    llm_metrics: dict[str, Any] = field(default_factory=dict)
    early_return: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Phase
# ---------------------------------------------------------------------------

class AnalyzePhase:
    """Run the ANALYZE stage of an investigation.

    Adapter pattern: holds a reference to the supervisor. Delegates the two
    self-coupled helpers (``_analyze_evidence``, ``_apply_self_critique``,
    ``_empty_result``) to it; all other behavior uses pure module functions.

    Usage from investigate() (after CollectPhase):

        from supervisor.phases.analyze import AnalyzePhase
        analyze_result = AnalyzePhase(self).execute(_ctx, _fout, _cres, _cout)
        aout = analyze_result.output.result["analyze"]
        if aout.early_return is not None:
            return aout.early_return
        result            = aout.result
        evidence          = aout.evidence
        confidence        = aout.confidence
        hypothesis_count  = aout.hypothesis_count
        winner_hypothesis = aout.winner_hypothesis
        llm_metrics       = aout.llm_metrics
        # ... Observe / Judge / Remediation / Persist continue unchanged ...
    """

    PHASE_NAME = "analyze"

    def __init__(self, supervisor: Any) -> None:
        self._sup = supervisor

    def execute(
        self,
        ctx: InvestigationContext,
        fetch_out: dict[str, Any],
        classification: Any,                          # ClassificationResult
        collect_out: Any,                             # CollectResult
        ledger: EvidenceLedger | None = None,
    ) -> PhaseResult:
        """Execute the analyze stage.

        ``ctx``: InvestigationContext (incident_id only).
        ``fetch_out``: FetchPhase.output.result — requires incident, summary,
            service, receipts, circuits.
        ``classification``: ClassificationResult — requires incident_type, budget.
        ``collect_out``: CollectResult — requires evidence, gate_post_collection.
        ``ledger``: accepted for symmetry; ANALYZE does not emit ledger items
            (the legacy code path does not write to evidence here either; only
            self_critique merges gap-evidence in-place into the dict).

        Returns PhaseResult with output.result["analyze"] = AnalyzeResult.
        """
        # Lazy imports keep this module decoupled from the supervisor agent
        # module at load time (no import cycle).
        from supervisor.confidence_calibrator import get_calibrator
        from supervisor.evidence_citation import annotate_citations
        from supervisor.evidence_gates import check_post_analysis
        from supervisor.observability import trace_span
        from supervisor.online_evaluator import (
            annotate_result as _annotate_online,
            evaluate as _online_evaluate,
        )
        from supervisor.progress_stream import get_stream

        # Graceful-degradation imports (match the agent.py pattern at lines 127-143).
        try:
            from supervisor.grounding_confidence import score as _grounding_score
            _GROUNDING_AVAILABLE = True
        except ImportError:
            _GROUNDING_AVAILABLE = False

        try:
            from supervisor.recurrence_tracker import check as _recurrence_check
            _RECURRENCE_AVAILABLE = True
        except ImportError:
            _RECURRENCE_AVAILABLE = False

        sup = self._sup
        incident_id = ctx.incident_id

        # --- Unpack inputs ---
        incident  = fetch_out["incident"]
        service   = fetch_out["service"]
        receipts  = fetch_out["receipts"]
        circuits  = fetch_out["circuits"]
        incident_type = classification.incident_type
        budget        = classification.budget
        evidence      = collect_out.evidence
        gate_post_collection = collect_out.gate_post_collection

        _stream = get_stream()

        # --- Step 4: deadline guard before expensive LLM calls ---
        _deadline = getattr(sup._tls, "investigation_deadline", None)
        if _deadline is not None and time.monotonic() > _deadline:
            logger.warning(
                "Investigation deadline exceeded before analysis for %s; "
                "returning timeout result",
                incident_id,
            )
            early = sup._empty_result(
                incident_id,
                "investigation_deadline_exceeded",
                degraded=True,
                degraded_reason="deadline_exceeded_before_analysis",
            )
            return self._wrap(AnalyzeResult(
                result=early, evidence=evidence, early_return=early,
            ))

        _stream.emit_phase(incident_id, "analyze", incident_type=incident_type)

        # --- TLS cache so harness.reanalyze() can re-use enriched evidence
        #     without repeating the expensive playbook ---
        sup._tls.last_evidence = dict(evidence)
        sup._tls.last_incident_type = incident_type

        # --- _analyze_evidence inside its own child trace span ---
        with trace_span("analyze_evidence", case_id=incident_id) as _ae_span:
            _ae_span.set_attribute("incident_type", incident_type)
            _ae_span.set_attribute("evidence_keys", len(evidence))
            result = sup._analyze_evidence(incident_id, incident, incident_type, evidence)
            _ae_span.set_attribute("confidence", result.get("confidence", 0))
            _ae_span.set_attribute("confidence_degraded", result.get("confidence_degraded", False))

        # --- Attach the post-collection gate result ---
        if gate_post_collection is not None:
            result["_gate_post_collection"] = gate_post_collection.to_dict()

        # --- Calibration ---
        confidence = result.get("confidence", 0)
        raw_confidence = confidence
        confidence = get_calibrator().calibrate(confidence, evidence_context=evidence)
        if confidence != raw_confidence:
            result["confidence"] = confidence
            result["raw_confidence"] = raw_confidence
        _stream.emit_confidence(
            incident_id, confidence, source="calibrator", previous=raw_confidence,
        )

        # --- Pop transient fields onto local variables for downstream phases ---
        hypothesis_count  = result.pop("_hypothesis_count", 0)
        winner_hypothesis = result.pop("_winner_hypothesis", "none")
        llm_metrics       = result.pop("_llm_metrics", {})

        # --- Step 4a-i: recurrence check (best-effort) ---
        _recurrence_info = None
        if _RECURRENCE_AVAILABLE:
            try:
                _recurrence_info = _recurrence_check(service, incident_type)
            except Exception:
                pass

        # --- Step 4a-ii: multi-dimensional grounding confidence (best-effort) ---
        if _GROUNDING_AVAILABLE:
            try:
                _grounding = _grounding_score(
                    result=result,
                    evidence=evidence,
                    incident_type=incident_type,
                    recurrence_info=_recurrence_info,
                )
                result["_grounding"] = _grounding.to_dict()
                _stream.emit_confidence(
                    incident_id,
                    int(_grounding.score * 100),
                    source="grounding_v2" if _grounding.model_version == "v2" else "grounding_v1",
                    previous=confidence,
                )
            except Exception as exc:
                logger.debug("Grounding confidence scoring skipped: %s", exc)

        # --- Step 4b: self-critique (may mutate result + evidence + confidence) ---
        result, evidence = sup._apply_self_critique(
            result, evidence, incident_id, incident_type, service,
            receipts, budget, circuits,
        )
        confidence = result.get("confidence", confidence)

        # --- Phase: Cite — ground every claim in source evidence ---
        # (before gate check so G5 has mechanically-matched citations,
        #  not just LLM-produced ones)
        annotate_citations(result, evidence)

        # --- Evidence Gate G2 + G3 + G5: anti-hallucination ---
        gate_post_analysis = check_post_analysis(result, evidence, budget.remaining())
        result["_gate_post_analysis"] = gate_post_analysis.to_dict()
        if not gate_post_analysis.passed and gate_post_analysis.verdict.value == "block":
            logger.warning(
                "Evidence gate BLOCK post-analysis: %s",
                gate_post_analysis.blocking_gate.reason
                if gate_post_analysis.blocking_gate else "unknown",
            )
            reason = (
                gate_post_analysis.blocking_gate.reason
                if gate_post_analysis.blocking_gate else "Evidence quality gate failed"
            )
            result["root_cause"] = f"BLOCKED: {reason}"
            result["confidence"] = 0
            result["hallucination_risk"] = True

        # --- Step 4c: online quality evaluation ---
        online_score = _online_evaluate(result, evidence, budget.calls_made, hypothesis_count)
        _annotate_online(result, online_score)

        # --- Evidence snapshot for experience_store (avoid holding full evidence) ---
        result["_evidence_snapshot"] = {
            k: bool(v) for k, v in evidence.items() if not k.startswith("_")
        }

        # --- git_blame_pinpoint extraction ---
        if evidence.get("git_blame"):
            blame = evidence["git_blame"]
            result["git_blame_pinpoint"] = {
                "file":           blame.get("culprit_file", ""),
                "line":           blame.get("culprit_line"),
                "sha":            blame.get("sha", "")[:12],
                "author":         blame.get("author", ""),
                "date":           blame.get("date", ""),
                "commit_message": blame.get("message", "")[:120],
                "repo":           blame.get("repo", ""),
            }

        # --- Top ITSM causal change extraction ---
        if evidence.get("_itsm_change_correlations"):
            top = evidence["_itsm_change_correlations"][0]
            if top.get("correlation_score", 0) >= 0.45:
                result["causal_change"] = {
                    "id":           top.get("id", top.get("number", "")),
                    "title":        top.get("title", top.get("summary", "")),
                    "change_type":  top.get("change_type", ""),
                    "risk_level":   top.get("risk_level", ""),
                    "minutes_before_incident": top.get("minutes_before_incident"),
                    "correlation_score":  top.get("correlation_score"),
                    "correlation_reason": top.get("correlation_reason", ""),
                    "commit_sha":   (top.get("matched_commit") or {}).get("sha", ""),
                }

        return self._wrap(AnalyzeResult(
            result=result,
            evidence=evidence,
            confidence=int(confidence),
            hypothesis_count=int(hypothesis_count),
            winner_hypothesis=str(winner_hypothesis),
            llm_metrics=dict(llm_metrics) if isinstance(llm_metrics, dict) else {},
            early_return=None,
        ))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _wrap(self, ares: AnalyzeResult) -> PhaseResult:
        return PhaseResult(
            phase=self.PHASE_NAME,
            status=PhaseStatus.COMPLETED,
            output=PhaseOutput(result={"analyze": ares}),
        )


__all__ = ["AnalyzePhase", "AnalyzeResult"]
