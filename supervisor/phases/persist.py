"""Phase: PERSIST — observability, evaluation, remediation, fix proposal, persistence.

Fifth (final) behavioral decomposition of supervisor.agent. Owns the sequence
executed today inside investigate() between AnalyzePhase return and the
top-level ``return result`` (current investigate() lines ~415-579).

Responsibilities (from a fresh source-of-truth scan):
1. Observability — ``self._record_observability(span, ...)`` and reading
   ``elapsed = span.elapsed_ms``.
2. LLM-as-judge scoring — ``self._run_judge_scoring(incident_id, incident_type, result)``
   → ``judge_scores`` (consumed later by ``_persist_results``).
3. Remediation generation — ``generate_remediation(...)`` → mutates
   ``result["remediation"]``.
4. Proposed fix (conditional on ``evidence.get("diff_analysis")``) —
   emits FIX_PROPOSED, calls ``self._generate_proposed_fix``, mutates
   ``result["proposed_fix"]``. On non-``"none"`` fix:
     - bidirectional git-incident link (best-effort, ``debug`` on failure)
     - verification-loop dispatch to ``self._parallel_executor`` +
       FIX_VERIFYING emit (best-effort, ``debug`` on failure).
5. Investigation-complete log line.
6. Dashboard metrics — ``record_investigation_outcome(...)``.
7. Persist proper — persist-deadline guard:
   - over deadline: mark ``result["confidence_degraded"] = True`` and
     append ``persist_skipped:deadline_exceeded`` to
     ``result["confidence_degraded_reason"]``; skip ``_persist_results``.
   - within deadline: open child ``trace_span("persist_results")``, set
     ``incident_type`` + ``confidence`` attributes, and call
     ``self._persist_results(result, incident_id, incident_type, service,
     evidence, receipts, budget, confidence, hypothesis_count,
     winner_hypothesis, severity, summary, llm_metrics, judge_scores,
     elapsed, incident=incident)``.
8. Stream ``emit_complete`` with root_cause / confidence / citation_coverage
   / fix_proposed / elapsed.
9. Pattern Intelligence feedback loop — ``get_runner().record_outcome(
   service, incident_id)`` inside a bare ``try/except: pass``.

NOT owned (stays in investigate()):
- the outer ``trace_span("investigate")`` wrapper.
- the pre-span replay short-circuit.
- the final ``return result`` — investigate() unwraps ``PersistResult`` and
  returns the mutated dict, so no behavior change at the return boundary.

Adapter pattern: PersistPhase holds a supervisor reference and delegates
the four self-coupled helpers (``_record_observability``,
``_run_judge_scoring``, ``_generate_proposed_fix``, ``_persist_results``)
plus ``self.workers`` (for VerificationLoop) and ``self._parallel_executor``
(for the verification-loop dispatch) plus ``self._tls`` (for the persist
deadline guard). All other dependencies are pure module-level functions.

Evidence dict authority: unchanged. PersistPhase reads ``evidence`` in a
few places (``diff_analysis``, ``devops_context``, ``get_golden_signals``)
but never writes to it. EvidenceLedger semantics unchanged.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from sentinel_core.context import InvestigationContext
from sentinel_core.evidence import EvidenceLedger
from supervisor.phases.contracts import PhaseOutput, PhaseResult, PhaseStatus

logger = logging.getLogger("sentinalai.persist")


# ---------------------------------------------------------------------------
# Typed result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PersistResult:
    """The final mutated result dict, ready for investigate() to return."""
    result: dict[str, Any]


# ---------------------------------------------------------------------------
# Phase
# ---------------------------------------------------------------------------

class PersistPhase:
    """Run the PERSIST stage of an investigation.

    Adapter pattern: delegates to the supervisor's existing helpers
    (``_record_observability``, ``_run_judge_scoring``,
    ``_generate_proposed_fix``, ``_persist_results``). Pure module-level
    functions (``generate_remediation``, ``link_incident_to_commit``,
    ``record_investigation_outcome``, ``get_stream`` / ``EventType``,
    ``trace_span``, ``get_runner``, ``VerificationLoop``) are used directly.

    Usage from investigate() (after AnalyzePhase):

        from supervisor.phases.persist import PersistPhase
        persist_result = PersistPhase(self).execute(
            _ctx, _fout, _cres, _aout, span=span,
        )
        return persist_result.output.result["persist"].result
    """

    PHASE_NAME = "persist"

    def __init__(self, supervisor: Any) -> None:
        self._sup = supervisor

    def execute(
        self,
        ctx: InvestigationContext,
        fetch_out: dict[str, Any],
        classification: Any,                          # ClassificationResult
        analyze_out: Any,                             # AnalyzeResult
        span: Any,                                    # outer trace_span("investigate")
        ledger: EvidenceLedger | None = None,
    ) -> PhaseResult:
        """Execute the persist stage.

        ``ctx``: InvestigationContext (incident_id + investigation_id used).
        ``fetch_out``: dict from FetchPhase.output.result — requires
            receipts, summary, incident.
        ``classification``: ClassificationResult — requires incident_type,
            severity, budget, itsm_context.
        ``analyze_out``: AnalyzeResult — requires result, evidence,
            confidence, hypothesis_count, winner_hypothesis, llm_metrics.
        ``span``: the outer ``trace_span("investigate")`` context manager
            reference. Read for observability attribute setting AND for
            ``elapsed = span.elapsed_ms`` after ``_record_observability``.
        ``ledger``: accepted for symmetry; PERSIST does not emit ledger items.

        Returns PhaseResult with output.result["persist"] = PersistResult(
        result=<mutated result dict>).
        """
        # Lazy imports keep this module decoupled from the supervisor agent
        # module at load time (no import cycle).
        from supervisor.observability import trace_span
        from supervisor.progress_stream import EventType, get_stream
        from supervisor.remediation import generate_remediation
        from supervisor.incident_git_linker import link_incident_to_commit
        from supervisor.metrics_dashboard import record_investigation_outcome

        sup = self._sup
        incident_id      = ctx.incident_id
        investigation_id = ctx.investigation_id

        # --- Unpack inputs ---
        incident  = fetch_out["incident"]
        summary   = fetch_out["summary"]
        service   = fetch_out["service"]
        receipts  = fetch_out["receipts"]
        incident_type = classification.incident_type
        severity      = classification.severity
        budget        = classification.budget
        itsm_context  = classification.itsm_context
        result            = analyze_out.result
        evidence          = analyze_out.evidence
        confidence        = analyze_out.confidence
        hypothesis_count  = analyze_out.hypothesis_count
        winner_hypothesis = analyze_out.winner_hypothesis
        llm_metrics       = analyze_out.llm_metrics

        _stream = get_stream()

        # --- (1) Observability: span attributes + deep-eval metrics ---
        sup._record_observability(
            span, result, evidence, budget, receipts,
            incident_id, incident_type, service, confidence,
            hypothesis_count, winner_hypothesis, llm_metrics,
        )
        elapsed = span.elapsed_ms

        # --- (2) LLM-as-judge scoring ---
        judge_scores = sup._run_judge_scoring(incident_id, incident_type, result)

        # --- (3) Remediation guidance ---
        remediation = generate_remediation(
            incident_type=incident_type,
            root_cause=result.get("root_cause", ""),
            confidence=confidence,
            evidence_summary=(
                f"sources={len(evidence)}, tool_calls={budget.calls_made}, "
                f"hypotheses={hypothesis_count}"
            ),
            itsm_context=itsm_context,
            devops_context=evidence.get("devops_context"),
        )
        result["remediation"] = remediation

        # --- (4) Proposed fix (conditional) ---
        if evidence.get("diff_analysis"):
            _stream.emit(incident_id, EventType.FIX_PROPOSED, {}, phase="fix")
            proposed_fix = sup._generate_proposed_fix(
                incident_id, investigation_id, service, evidence, result,
            )
            if proposed_fix and proposed_fix.fix_type != "none":
                result["proposed_fix"] = proposed_fix.to_dict()
                logger.info(
                    "Proposed fix stored: type=%s confidence=%.0f risk=%s",
                    proposed_fix.fix_type, proposed_fix.confidence, proposed_fix.risk_level,
                )
                # Bidirectional git-incident link: record the breaking commit
                _breaking_sha = (
                    proposed_fix.sha
                    if hasattr(proposed_fix, "sha") and proposed_fix.sha
                    else result.get("proposed_fix", {}).get("sha", "")
                )
                _breaking_repo = (
                    proposed_fix.repo
                    if hasattr(proposed_fix, "repo") and proposed_fix.repo
                    else result.get("proposed_fix", {}).get("repo", "")
                )
                if _breaking_sha and _breaking_repo:
                    try:
                        link_incident_to_commit(
                            incident_id=incident_id,
                            commit_sha=_breaking_sha,
                            repo=_breaking_repo,
                            relationship="caused_by",
                            confidence=proposed_fix.confidence / 100.0
                            if hasattr(proposed_fix, "confidence") else 0.8,
                            commit_message=result.get("proposed_fix", {}).get("description", ""),
                        )
                        logger.info(
                            "Git-incident link recorded: %s caused_by %s",
                            incident_id, _breaking_sha[:8],
                        )
                    except Exception as _link_exc:
                        logger.debug("Git-incident link failed (non-critical): %s", _link_exc)

                # Wire verification loop: poll metrics post-fix in background
                try:
                    import asyncio as _asyncio
                    from supervisor.verification_loop import VerificationLoop
                    _vloop = VerificationLoop(
                        metrics_worker=sup.workers.get("metrics_worker"),
                        log_worker=sup.workers.get("log_worker"),
                    )
                    _vloop_investigation_id = investigation_id
                    _vloop_service = service
                    _vloop_incident_id = incident_id
                    _vloop_itsm = sup.workers.get("itsm_worker")
                    _baseline = evidence.get("get_golden_signals", {}).get("metrics", {})

                    def _run_verification():
                        _asyncio.run(_vloop.watch(
                            investigation_id=_vloop_investigation_id,
                            service=_vloop_service,
                            incident_id=_vloop_incident_id,
                            itsm_worker=_vloop_itsm,
                            baseline=_baseline,
                        ))

                    _stream.emit(incident_id, EventType.FIX_VERIFYING, {}, phase="fix")
                    sup._parallel_executor.submit(_run_verification)
                    logger.info("Verification loop started for %s/%s", incident_id, service)
                except Exception as _vl_exc:
                    logger.debug("Verification loop start failed (non-critical): %s", _vl_exc)

        # --- (5) Investigation-complete log ---
        logger.info(
            "Investigation complete for %s: confidence=%d, tool_calls=%d",
            incident_id, confidence, budget.calls_made,
        )

        # --- (6) Dashboard metrics ---
        _llm_m = result.get("_llm_metrics", llm_metrics or {})
        record_investigation_outcome(
            investigation_id=investigation_id,
            incident_id=incident_id,
            incident_type=incident_type,
            service=service,
            root_cause=result.get("root_cause", ""),
            confidence=confidence,
            severity=severity.level,
            elapsed_ms=elapsed,
            tool_calls=budget.calls_made,
            llm_input_tokens=_llm_m.get("input_tokens", 0),
            llm_output_tokens=_llm_m.get("output_tokens", 0),
            citation_coverage=result.get("citation_coverage", 0.0),
            fix_proposed=bool(result.get("proposed_fix")),
            fix_applied=False,   # updated later by FixEngine
            fix_verified=False,  # updated later by VerificationLoop
        )

        # --- (7) Persist proper — skip heavy writes if deadline passed ---
        _persist_deadline = getattr(sup._tls, "investigation_deadline", None)
        _persist_over_deadline = (
            _persist_deadline is not None
            and time.monotonic() > _persist_deadline
        )
        if _persist_over_deadline:
            logger.warning(
                "Deadline exceeded before persist for %s; skipping non-critical writes",
                incident_id,
            )
            result["confidence_degraded"] = True
            result.setdefault("confidence_degraded_reason", "")
            result["confidence_degraded_reason"] = (
                (result["confidence_degraded_reason"] + "; " if result["confidence_degraded_reason"] else "")
                + "persist_skipped:deadline_exceeded"
            )
        else:
            with trace_span("persist_results", case_id=incident_id) as _pr_span:
                _pr_span.set_attribute("incident_type", incident_type)
                _pr_span.set_attribute("confidence", confidence)
                sup._persist_results(
                    result, incident_id, incident_type, service, evidence,
                    receipts, budget, confidence, hypothesis_count,
                    winner_hypothesis, severity, summary, llm_metrics,
                    judge_scores, elapsed, incident=incident,
                )

        # --- (8) emit_complete ---
        _stream.emit_complete(
            investigation_id=incident_id,
            root_cause=result.get("root_cause", ""),
            confidence=confidence,
            citation_coverage=result.get("citation_coverage", 0.0),
            fix_proposed=bool(result.get("proposed_fix")),
            elapsed_ms=elapsed,
        )

        # --- (9) Pattern Intelligence feedback loop (best-effort) ---
        try:
            from intelligence.background_runner import get_runner
            get_runner().record_outcome(service, incident_id)
        except Exception:
            pass

        return self._wrap(PersistResult(result=result))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _wrap(self, pres: PersistResult) -> PhaseResult:
        return PhaseResult(
            phase=self.PHASE_NAME,
            status=PhaseStatus.COMPLETED,
            output=PhaseOutput(result={"persist": pres}),
        )


__all__ = ["PersistPhase", "PersistResult"]
