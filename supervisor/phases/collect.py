"""Phase: COLLECT — playbook dispatch, future awaits, and enrichment.

Third behavioral decomposition of supervisor.agent. Owns the sequence
executed today inside investigate() between the ClassificationPhase return
and the ANALYZE phase entry (lines ~382-583 in the post-Phase-11 file).

Responsibilities:
- emit the "collect" phase event (start + done)
- dispatch the playbook (or planner loop, when AGENTIC_PLANNER + LLM are on)
- submit trace correlation + visual evidence futures
- await + merge: ITSM, Confluence, historical, experience, tool_rec, KG
- evidence gate G1 + G4 check (post-collection); early return if blocked
- DevOps enrichment (proof-gated by deployment presence)
- CMDB blast-radius traversal
- diff analysis + git-blame pinpoint
- await + merge: trace correlation + visual evidence

NOT owned (caller responsibility):
- ANALYZE / persist / citation / online evaluation / receipts
- the trace_span("investigate") wrapper itself
- everything in investigate() after line ~585

Adapter pattern: CollectPhase holds a supervisor reference and delegates
to its existing helpers (``_execute_playbook``, ``_execute_planner_loop``,
``_fetch_devops_context``, ``_fetch_cmdb_blast_radius``, ``_extract_changes``,
``_find_deployment``, ``_find_deployment_in_blast_radius``,
``_fetch_diff_analysis``, ``_fetch_git_blame``, ``_parallel_executor``,
``_gateway``, ``_tls``, ``_call_timeout``). No worker / LLM / planner
logic is reimplemented.

Evidence dict authority: the legacy ``evidence: dict[str, Any]`` produced
by the playbook executor remains the source of truth. CollectPhase merges
new keys into it directly (replacing the inline mutations from investigate)
and, when EVIDENCE_LEDGER_SHADOW_ENABLED is on, mirrors each write into a
local ShadowMirror for parity validation. The ledger never influences
output.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from sentinel_core.context import InvestigationContext
from sentinel_core.evidence import EvidenceLedger
from supervisor.phases.contracts import PhaseOutput, PhaseResult, PhaseStatus

logger = logging.getLogger("sentinalai.collect")


# ---------------------------------------------------------------------------
# F-obs: dependency-failure observability
# ---------------------------------------------------------------------------

# PB-3 evidence lifecycle terminal states — every evidence object ends in
# exactly one of these; "unknown"/silent disappearance is forbidden.
EVIDENCE_STATES = ("used", "filtered", "suppressed", "unavailable", "error")


def _record_unavailable(evidence: dict[str, Any], source: str,
                        reason: str, *, state: str = "unavailable") -> None:
    """Record a non-``used`` terminal state for an evidence source so it is
    never silently swallowed. Appends a deterministic entry to
    ``_sources_unavailable`` (surfaced to operators in receipts + report) and
    logs it. Additive only; never changes RCA authority."""
    state = state if state in EVIDENCE_STATES else "unavailable"
    entry = {"source": str(source), "reason": str(reason)[:200], "state": state}
    bucket = evidence.setdefault("_sources_unavailable", [])
    if entry not in bucket:
        bucket.append(entry)
    logger.warning("evidence source %s: %s (%s)", state, source,
                   entry["reason"])


def _scan_worker_errors(evidence: dict[str, Any]) -> None:
    """Post-collection sweep: classify every worker response's terminal state.
    ``{"error": ...}`` → error; malformed ``{"raw_response": ...}`` (no usable
    keys) → unavailable. Deterministic (sorted key iteration)."""
    for key in sorted(evidence):
        if key.startswith("_"):
            continue
        val = evidence.get(key)
        if not isinstance(val, dict):
            continue
        if val.get("error"):
            _record_unavailable(evidence, key, str(val.get("error")),
                                state="error")
        elif "raw_response" in val and len(val) == 1:
            # malformed/non-JSON worker response — parsed nothing usable
            _record_unavailable(evidence, key, "malformed response "
                                "(unparseable raw_response)", state="unavailable")


def _evidence_lifecycle(evidence: dict[str, Any]) -> dict[str, Any]:
    """Summarise the terminal state of every evidence object (PB-3). Sources
    with usable data are 'used'; the rest carry their recorded non-used state.
    No source is 'unknown'."""
    unavailable = {e["source"]: e.get("state", "unavailable")
                   for e in evidence.get("_sources_unavailable", [])}
    received = sorted(k for k in evidence if not str(k).startswith("_"))
    states: dict[str, str] = {}
    for k in received:
        v = evidence.get(k)
        if k in unavailable:
            states[k] = unavailable[k]
        elif v in (None, "", [], {}):
            states[k] = "filtered"          # collected but empty
        else:
            states[k] = "used"
    # include unavailable sources that never produced an evidence key
    for src, st in unavailable.items():
        states.setdefault(src, st)
    counts: dict[str, int] = {s: 0 for s in EVIDENCE_STATES}
    for st in states.values():
        counts[st] = counts.get(st, 0) + 1
    return {"by_source": dict(sorted(states.items())), "counts": counts}


# ---------------------------------------------------------------------------
# Typed result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CollectResult:
    """All values produced by the COLLECT phase.

    - ``evidence`` is the accumulated evidence dict, post-enrichment. Same
      reference the caller will pass into ANALYZE.
    - ``early_return``: if set, investigate() returns this dict immediately.
      Used today only by the post-collection evidence gate (G1+G4 block).
    - ``gate_post_collection``: the GateResult dict, attached by the caller
      to the analyze result at ``result["_gate_post_collection"]``. None
      when early_return is set (the dict is already inside early_return's
      reasoning string).
    """
    evidence: dict[str, Any]
    early_return: Optional[dict[str, Any]] = None
    gate_post_collection: Optional[Any] = None  # GateResult object (not dict)


# ---------------------------------------------------------------------------
# Phase
# ---------------------------------------------------------------------------

class CollectPhase:
    """Run the COLLECT stage of an investigation.

    Adapter pattern: holds a reference to the supervisor and delegates all
    worker calls, planner dispatch, and enrichment helpers to its existing
    methods. The phase orchestrates the sequence and owns evidence
    mutation timing.

    Usage from investigate() (after ClassificationPhase):

        from supervisor.phases.collect import CollectPhase
        collect_result = CollectPhase(self).execute(_ctx, _fout, _cres)
        cout = collect_result.output.result["collect"]
        if cout.early_return is not None:
            return cout.early_return
        evidence = cout.evidence
        gate_post_collection = cout.gate_post_collection
        # ... ANALYZE continues unchanged ...
    """

    PHASE_NAME = "collect"

    def __init__(self, supervisor: Any) -> None:
        self._sup = supervisor

    def execute(
        self,
        ctx: InvestigationContext,
        fetch_out: dict[str, Any],
        classification: Any,                          # ClassificationResult
        ledger: EvidenceLedger | None = None,
    ) -> PhaseResult:
        """Execute the collect stage.

        ``ctx``: InvestigationContext (incident_id used).
        ``fetch_out``: FetchPhase.output.result dict — requires incident,
            summary, service, receipts, circuits.
        ``classification``: ClassificationResult — requires incident_type,
            budget (severity-scaled), itsm_context, confluence_context,
            experience_future, kg_future, historical_future.
        ``ledger``: optional EvidenceLedger; accepted for symmetry.

        Returns PhaseResult with output.result["collect"] = CollectResult.
        Status is always COMPLETED.
        """
        # Lazy imports keep this module decoupled from the supervisor agent
        # module at load time (no import cycle).
        from supervisor.evidence_gates import check_post_collection
        from supervisor.evidence_shadow import ShadowMirror
        from supervisor.experience_store import get_tool_recommendations as _get_tool_recommendations
        from supervisor.progress_stream import EventType, get_stream
        from supervisor.trace_correlation import correlate_traces
        from workers.visual_evidence_worker import collect_visual_evidence

        sup = self._sup
        incident_id = ctx.incident_id

        # --- Unpack fetch outputs ---
        incident   = fetch_out["incident"]
        summary    = fetch_out["summary"]
        service    = fetch_out["service"]
        receipts   = fetch_out["receipts"]
        circuits   = fetch_out["circuits"]

        # --- Unpack classification outputs ---
        incident_type      = classification.incident_type
        budget             = classification.budget
        itsm_context       = classification.itsm_context
        confluence_context = classification.confluence_context
        experience_future  = classification.experience_future
        kg_future          = classification.kg_future
        historical_future  = classification.historical_future

        _stream = get_stream()
        _shadow = ShadowMirror.create()

        # --- Collect phase event ---
        _stream.emit_phase(incident_id, "collect", incident_type=incident_type)

        # --- Step 3: Execute playbook OR planner loop (legacy dispatch logic) ---
        from supervisor.llm import is_enabled as _llm_enabled
        _use_planner = os.environ.get("AGENTIC_PLANNER", "false").lower() in ("1", "true", "yes")
        _use_lc = os.environ.get("LOOP_CONTROLLER_ENABLED", "false").lower() in ("1", "true", "yes")
        # LoopController degrades to fallback playbook when LLM is off — allow it regardless.
        # Raw AgenticPlanner still requires LLM (its Think step has no utility without it).
        if _use_planner and (_use_lc or _llm_enabled()):
            evidence = sup._execute_planner_loop(
                incident_type, incident_id, service, incident, receipts, budget, circuits,
            )
        else:
            evidence = sup._execute_playbook(
                incident_type, incident_id, service, receipts, budget, circuits,
            )
        _stream.emit_phase_done(
            incident_id, "collect",
            evidence_keys=list(evidence.keys()),
            evidence_count=len(evidence),
        )

        # Seed the shadow ledger with whatever the playbook already accumulated
        # (the inner playbook function has its own shadow mirror that handles
        # ITS writes; here we capture the resulting dict before our enrichment
        # writes so parity covers BOTH playbook and post-playbook mutations).
        if _shadow is not None:
            for _k, _v in evidence.items():
                _shadow.set(_k, _v)

        # --- Submit trace + visual futures (concurrent with the awaits below) ---
        incident_time = incident.get("created_at", incident.get("timestamp", ""))
        _gateway = sup._gateway if hasattr(sup, "_gateway") else None
        _trace_future = sup._parallel_executor.submit(
            correlate_traces, incident, evidence, _gateway,
        )
        _visual_future = sup._parallel_executor.submit(
            collect_visual_evidence, service, incident_time, incident_type, _gateway,
        )

        # --- Merge ITSM / Confluence context (from classification) into evidence ---
        if itsm_context:
            evidence["itsm_context"] = itsm_context
            if _shadow is not None:
                _shadow.set("itsm_context", itsm_context)

        if confluence_context:
            evidence["confluence_context"] = confluence_context
            if _shadow is not None:
                _shadow.set("confluence_context", confluence_context)

        # --- Await historical_context future ---
        try:
            historical = historical_future.result(timeout=sup._call_timeout)
        except Exception as exc:
            historical = None
            _record_unavailable(evidence, "historical_context", exc)
        if historical:
            evidence["historical_context"] = historical
            if _shadow is not None:
                _shadow.set("historical_context", historical)

        # --- Await experience_future + prime hypotheses ---
        try:
            past_experiences = experience_future.result(timeout=5)
        except Exception as exc:
            past_experiences = []
            _record_unavailable(evidence, "experience_store", exc)
        if past_experiences:
            evidence["_past_experiences"] = past_experiences
            if _shadow is not None:
                _shadow.set("_past_experiences", past_experiences)
            # Extract confirmed root causes from similar past successes
            suggested_causes = [
                exp.get("root_cause", "") for exp in past_experiences
                if exp.get("root_cause") and not exp.get("root_cause", "").startswith("INSUFFICIENT")
            ]
            if suggested_causes:
                evidence["_suggested_root_causes"] = suggested_causes
                if _shadow is not None:
                    _shadow.set("_suggested_root_causes", suggested_causes)
                logger.info(
                    "Priming with %d similar past experience(s) for %s/%s; "
                    "suggested causes: %s",
                    len(past_experiences), incident_type, service,
                    suggested_causes[:2],
                )
            else:
                logger.info(
                    "Priming with %d similar past experience(s) for %s/%s",
                    len(past_experiences), incident_type, service,
                )

        # --- Tool recommendations from historical performance ---
        try:
            tool_recs = _get_tool_recommendations(incident_type, service)
            if tool_recs:
                evidence["_tool_recommendations"] = tool_recs
                if _shadow is not None:
                    _shadow.set("_tool_recommendations", tool_recs)
                logger.debug(
                    "Tool recommendations for %s/%s: %s",
                    incident_type, service, list(tool_recs.keys())[:5],
                )
        except Exception as exc:
            _record_unavailable(evidence, "tool_recommendations", exc)

        # --- Await kg_future + prime hypotheses ---
        try:
            kg_similar = kg_future.result(timeout=3)
        except Exception as exc:
            kg_similar = []
            _record_unavailable(evidence, "knowledge_graph", exc)
        if kg_similar:
            evidence["_kg_similar_incidents"] = kg_similar
            if _shadow is not None:
                _shadow.set("_kg_similar_incidents", kg_similar)
            kg_causes = [
                inc.get("root_cause", "") for inc in kg_similar
                if inc.get("root_cause")
            ]
            if kg_causes:
                existing = evidence.get("_suggested_root_causes", [])
                merged = list(dict.fromkeys(existing + kg_causes))[:5]
                evidence["_suggested_root_causes"] = merged
                if _shadow is not None:
                    _shadow.set("_suggested_root_causes", merged)
            logger.info(
                "KG priming: %d similar historical incident(s) for %s/%s",
                len(kg_similar), incident_type, service,
            )

        logger.info("Playbook complete for %s: %d evidence items", incident_id, len(evidence))

        # PB-3: sweep worker-error/malformed responses into terminal states
        # BEFORE the gate check, so a gate BLOCK still records why sources were
        # unavailable (no silent loss on the block path).
        _scan_worker_errors(evidence)

        # --- Evidence Gate G1 + G4 check ---
        gate_post_collection = check_post_collection(evidence, budget.calls_made)
        if not gate_post_collection.passed and gate_post_collection.verdict.value == "block":
            logger.warning(
                "Evidence gate BLOCK post-collection: %s",
                gate_post_collection.blocking_gate.reason
                if gate_post_collection.blocking_gate else "unknown",
            )
            reason = (
                f"Evidence gate blocked: {gate_post_collection.blocking_gate.reason}"
                if gate_post_collection.blocking_gate else "Evidence gate blocked"
            )
            early = sup._empty_result(incident_id, reason)
            if _shadow is not None:
                _shadow.parity_log(evidence, context="CollectPhase.gate_block")
            return self._wrap(CollectResult(
                evidence=evidence,
                early_return=early,
                gate_post_collection=gate_post_collection,
            ))

        # --- Step 3b: DevOps enrichment (proof-gated) ---
        changes = sup._extract_changes(evidence)
        deployment = sup._find_deployment(changes)
        if deployment:
            devops_context = sup._fetch_devops_context(
                service, deployment, receipts, budget, circuits,
            )
            if devops_context:
                evidence["devops_context"] = devops_context
                if _shadow is not None:
                    _shadow.set("devops_context", devops_context)

        # --- Step 3c: CMDB blast radius ---
        cmdb_context = sup._fetch_cmdb_blast_radius(
            service, incident_id, receipts, budget, circuits,
        )
        if cmdb_context:
            evidence["cmdb_blast_radius"] = cmdb_context
            if _shadow is not None:
                _shadow.set("cmdb_blast_radius", cmdb_context)

        # --- Step 3d: Diff analysis on a dependency (if no direct devops_context) ---
        if cmdb_context and not evidence.get("devops_context"):
            dep_deployment = sup._find_deployment_in_blast_radius(cmdb_context)
            if dep_deployment:
                dep_devops_context = sup._fetch_devops_context(
                    dep_deployment.get("_ci", service),
                    dep_deployment,
                    receipts, budget, circuits,
                )
                if dep_devops_context:
                    evidence["devops_context"] = dep_devops_context
                    if _shadow is not None:
                        _shadow.set("devops_context", dep_devops_context)

        # --- Step 3e + 3f: Code diff analysis + git blame pinpoint ---
        if evidence.get("devops_context") and budget and budget.can_call():
            diff_analysis = sup._fetch_diff_analysis(
                service, evidence, receipts, budget, circuits,
            )
            if diff_analysis:
                evidence["diff_analysis"] = diff_analysis
                if _shadow is not None:
                    _shadow.set("diff_analysis", diff_analysis)
                culprit_file = diff_analysis.get("culprit_file", "")
                culprit_line = diff_analysis.get("culprit_line")
                devops_ctx = evidence.get("devops_context", {})
                blame_repo = (
                    devops_ctx.get("deployments", [{}])[0].get("repo", "")
                    if devops_ctx.get("deployments") else ""
                )
                if culprit_file and culprit_line and blame_repo:
                    blame_result = sup._fetch_git_blame(
                        blame_repo, culprit_file, int(culprit_line),
                        receipts, budget, circuits,
                    )
                    if blame_result:
                        evidence["git_blame"] = blame_result
                        if _shadow is not None:
                            _shadow.set("git_blame", blame_result)

        # --- Await trace correlation ---
        try:
            trace_corr = _trace_future.result(timeout=5)
            if trace_corr:
                evidence["trace_correlation"] = trace_corr
                if _shadow is not None:
                    _shadow.set("trace_correlation", trace_corr)
                _stream.emit(
                    incident_id, EventType.TRACE_CORRELATED,
                    {
                        "trace_id": trace_corr.get("trace_id", "")[:16],
                        "root_span_service": trace_corr.get("root_span_service", ""),
                        "chain_depth": trace_corr.get("chain_depth", 0),
                        "confidence": trace_corr.get("correlation_confidence", 0),
                    },
                    phase="collect",
                )
        except Exception as exc:
            _record_unavailable(evidence, "trace_correlation", exc)

        # --- Await visual evidence ---
        try:
            visual_ev = _visual_future.result(timeout=10)
            if visual_ev:
                evidence["visual_evidence"] = visual_ev
                if _shadow is not None:
                    _shadow.set("visual_evidence", visual_ev)
        except Exception as exc:
            _record_unavailable(evidence, "visual_evidence", exc)

        # --- F-obs: sweep worker-error responses into sources_unavailable ---
        _scan_worker_errors(evidence)

        # PB-3: standardized evidence lifecycle — every evidence object ends in
        # exactly one terminal state (used/filtered/suppressed/unavailable/error).
        evidence["_evidence_lifecycle"] = _evidence_lifecycle(evidence)

        # --- Final parity log ---
        if _shadow is not None:
            _shadow.parity_log(evidence, context="CollectPhase.final")

        return self._wrap(CollectResult(
            evidence=evidence,
            early_return=None,
            gate_post_collection=gate_post_collection,
        ))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _wrap(self, cres: CollectResult) -> PhaseResult:
        return PhaseResult(
            phase=self.PHASE_NAME,
            status=PhaseStatus.COMPLETED,
            output=PhaseOutput(result={"collect": cres}),
        )


__all__ = ["CollectPhase", "CollectResult"]
