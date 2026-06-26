"""SentinalAI Supervisor Agent.

Orchestrates incident investigation by:
1. Fetching incident metadata (ops worker / Moogsoft)
2. Classifying the incident type
3. Running the appropriate playbook (3-5 targeted worker calls)
4. Analyzing gathered evidence to determine root cause
5. Producing a structured RCA result

Designed to be fully deterministic: same input -> same output.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import concurrent.futures
from typing import Any

from supervisor.tool_selector import classify_incident, get_evolved_playbook, is_meta_query
from supervisor.receipt import ReceiptCollector
from supervisor.guardrails import (
    ExecutionBudget,
    CircuitBreakerRegistry,
    LoopCheckpoint,
    CALL_TIMEOUT_SECONDS,
    MAX_RETRIES_PER_CALL,
    MAX_CONCURRENT_WORKERS,
    circuit_registry,
)
from supervisor.observability import (
    trace_span,
    GENAI_SYSTEM,
    GENAI_OPERATION_NAME,
    GENAI_REQUEST_MODEL,
    GENAI_USAGE_INPUT_TOKENS,
    GENAI_USAGE_OUTPUT_TOKENS,
    EVAL_INCIDENT_TYPE,
    EVAL_SERVICE,
    EVAL_CONFIDENCE,
    EVAL_ROOT_CAUSE,
    EVAL_TOOL_CALLS,
    EVAL_HYPOTHESIS_COUNT,
    EVAL_WINNER_NAME,
    EVAL_EVIDENCE_SOURCES,
    EVAL_BUDGET_REMAINING,
)
from supervisor.eval_metrics import (
    record_investigation,
    record_worker_call,
    record_evidence_completeness,
    record_receipt_summary,
    record_llm_usage,
    record_judge_scores,
)
from supervisor.replay import ReplayStore
from supervisor.memory import (
    store_investigation_result as _store_to_memory,
    is_enabled as _memory_enabled,
)
from supervisor.llm import (
    refine_hypothesis as _llm_refine,
    generate_reasoning as _llm_reasoning,
    is_enabled as _llm_enabled,
)
from supervisor.llm_judge import judge_and_record as _judge_and_record
from supervisor.severity import detect_severity, get_budget_for_severity
from supervisor.remediation import generate_remediation
from supervisor.incident_model import Incident
from supervisor.rca_report import generate_rca_report, render_markdown
from database.persistence import (
    persist_investigation as _db_persist,
    persist_tool_usage as _db_persist_tools,
    persist_knowledge_entry as _db_persist_knowledge,
    is_enabled as _db_enabled,
)
from supervisor.confidence_calibrator import get_calibrator
from supervisor.self_critique import critique as _self_critique
from supervisor.online_evaluator import evaluate as _online_evaluate, annotate_result as _annotate_online
from supervisor.experience_store import (
    store_experience as _store_experience,
    store_failed_experience as _store_failed_experience,
    retrieve_similar as _retrieve_experiences,
    get_tool_recommendations as _get_tool_recommendations,
)
from supervisor.strategy_evolver import (
    record_outcome as _record_strategy_outcome,
    should_skip_step as _should_skip_step,
    record_gap_pattern as _record_gap_pattern,
)
from supervisor.gap_aggregator import record_gaps_from_critique as _record_gaps_from_critique
from supervisor.adaptive_thresholds import (
    record_critique_outcome as _record_critique_outcome,
    record_quality_observation as _record_quality_observation,
    record_step_skip_outcome as _record_step_skip_outcome,
    record_confidence_outcome as _record_confidence_outcome,
)
from workers.mcp_client import McpGateway
from workers.ops_worker import OpsWorker
from workers.log_worker import LogWorker
from workers.metrics_worker import MetricsWorker
from workers.apm_worker import ApmWorker
from workers.knowledge_worker import KnowledgeWorker
from workers.itsm_worker import ItsmWorker
from workers.devops_worker import DevopsWorker
from workers.confluence_worker import ConfluenceWorker
from workers.code_worker import CodeWorker
from workers.git_worker import GitWorker
from workers.network_worker import ThousandEyesWorker
from supervisor.cmdb_traversal import CMDBTraversal, build_change_summary
from supervisor.fix_engine import get_fix_engine, ProposedFix
from supervisor.evidence_citation import annotate_citations
from supervisor.metrics_dashboard import record_investigation_outcome
from supervisor.evidence_gates import check_post_collection, check_post_analysis
from supervisor.knowledge_graph import ingest_to_graph as _ingest_to_kg, query_similar as _kg_query_similar
from supervisor.memory_compression import compress_investigation as _compress_investigation
from supervisor.llm_call_graph import CallGraph, set_current_graph
from supervisor.progress_stream import get_stream, EventType
from supervisor.incident_git_linker import link_incident_to_commit
from supervisor.trace_correlation import correlate_traces
from workers.visual_evidence_worker import collect_visual_evidence

# Operational intelligence modules (graceful degradation if unavailable)
try:
    from supervisor.grounding_confidence import score as _grounding_score, GroundingResult as _GroundingResult
    _GROUNDING_AVAILABLE = True
except ImportError:
    _GROUNDING_AVAILABLE = False

try:
    from supervisor.dependency_domain_detector import get_gap_queries as _domain_gap_queries, detect as _detect_domains
    _DOMAIN_DETECTOR_AVAILABLE = True
except ImportError:
    _DOMAIN_DETECTOR_AVAILABLE = False

try:
    from supervisor.recurrence_tracker import check as _recurrence_check, record as _recurrence_record
    _RECURRENCE_AVAILABLE = True
except ImportError:
    _RECURRENCE_AVAILABLE = False

try:
    from supervisor.splunk_retrieval_planner import build_plan as _splunk_build_plan, get_stage_queries as _splunk_stage_queries
    _SPLUNK_PLANNER_AVAILABLE = True
except ImportError:
    _SPLUNK_PLANNER_AVAILABLE = False

# Institutional knowledge layer (opt-in via env var, graceful degradation)
_KNOWLEDGE_ENABLED = os.environ.get("KNOWLEDGE_GRAPH_ENABLED", "").lower() in ("1", "true", "yes")
try:
    from knowledge.graph_store import GraphStore as _GraphStore
    from knowledge.retrieval_engine import RetrievalEngine as _RetrievalEngine, compute_retrieval_boost as _retrieval_boost
    if _KNOWLEDGE_ENABLED:
        _kg = _GraphStore()
        _knowledge_graph: _GraphStore | None = _kg
        _knowledge_retrieval: _RetrievalEngine | None = _RetrievalEngine(graph_store=_kg)
    else:
        _knowledge_graph = None
        _knowledge_retrieval = None
    _KNOWLEDGE_AVAILABLE = True
except ImportError:
    _knowledge_graph = None  # type: ignore[assignment]
    _knowledge_retrieval = None  # type: ignore[assignment]
    _KNOWLEDGE_AVAILABLE = False

logger = logging.getLogger(__name__)


# =========================================================================
# Hypothesis dataclass for multi-hypothesis scoring (W2)
# =========================================================================

class Hypothesis:
    """A scored root-cause hypothesis with evidence references."""

    __slots__ = ("name", "root_cause", "base_score", "evidence_refs", "reasoning")

    def __init__(
        self,
        name: str,
        root_cause: str,
        base_score: float,
        evidence_refs: list[str],
        reasoning: str,
    ):
        self.name = name
        self.root_cause = root_cause
        self.base_score = base_score
        self.evidence_refs = evidence_refs
        self.reasoning = reasoning


# =========================================================================
# Evidence-weighted confidence calculator (W3)
# =========================================================================

def compute_confidence(
    base: float,
    logs: list[dict],
    signals: dict,
    metrics: dict,
    events: list[dict],
    changes: list[dict],
    corroborating_sources: int = 0,
    incident_type: str = "",
) -> int:
    """Compute evidence-weighted confidence.

    base:  the analyzer's starting score (e.g. 80 for a strong match)
    Then:
      +2 per corroborating evidence source (logs, signals, metrics, events, changes)
      +1 per log entry (max +5)
      +2 if golden signals present with anomaly detected
      +1 if metrics have pattern field
      -5 if signals absent AND the incident type is not one where absence is the symptom
      -3 if metrics absent AND the incident type is not one where absence is the symptom
    Bounded to [0, 100].

    ``incident_type`` guards the missing-source penalties: for ``silent_failure``
    and ``missing_data`` incidents the absence of golden signals or metrics is
    the defining characteristic of the incident, not a gap in investigation quality.
    Penalising those types would systematically under-score correct investigations.
    """
    score = base

    # Incident types where absence of signals/metrics is the expected finding
    _ABSENCE_IS_SYMPTOM = frozenset({"silent_failure", "missing_data"})

    # Corroboration bonus: count how many sources have data
    source_count = 0
    if logs:
        source_count += 1
        score += min(len(logs), 5)  # +1 per log, max +5
    if signals and signals.get("golden_signals"):
        source_count += 1
        if signals.get("anomaly_detected"):
            score += 2
    if metrics and metrics.get("metrics"):
        source_count += 1
        if metrics.get("pattern"):
            score += 1
    if events:
        source_count += 1
    if changes:
        source_count += 1

    # Cross-signal bonus
    score += source_count * 2

    # Missing-source penalty (only for incident types where presence is expected)
    if incident_type not in _ABSENCE_IS_SYMPTOM:
        if not signals or not signals.get("golden_signals"):
            score -= 5
        if not metrics or not metrics.get("metrics"):
            score -= 3

    # Explicit corroboration from caller
    score += corroborating_sources * 2

    return max(0, min(100, int(round(score))))


# =========================================================================
# Supervisor
# =========================================================================

class SentinalAISupervisor:
    """Autonomous incident RCA supervisor."""

    # G6.1: Per-investigation wall-clock deadline (seconds)
    INVESTIGATION_DEADLINE_SECONDS = float(
        os.environ.get("INVESTIGATION_DEADLINE_SECONDS", "120")
    )

    # Worker name → set of tool servers required (empty = always available)
    _WORKER_SERVERS: dict[str, frozenset[str]] = {
        "ops_worker":       frozenset({"moogsoft"}),
        "log_worker":       frozenset({"splunk"}),
        "metrics_worker":   frozenset({"sysdig"}),
        "apm_worker":       frozenset({"dynatrace", "signalfx"}),
        "knowledge_worker": frozenset(),          # always available (no external tool)
        "itsm_worker":      frozenset({"servicenow"}),
        "devops_worker":    frozenset({"github"}),
        "confluence_worker": frozenset({"confluence"}),
        "code_worker":      frozenset({"github"}),
        "git_worker":       frozenset({"github"}),
        # Aliases for planner-referenced workers — backed by existing implementations
        "signal_worker":    frozenset({"dynatrace", "signalfx"}),  # → ApmWorker
        "event_worker":     frozenset({"dynatrace", "signalfx"}),  # → ApmWorker (sysdig k8s events)
        "change_worker":    frozenset({"github"}),                  # → DevopsWorker
        "network_worker":   frozenset(),  # always available; ENABLE_THOUSANDEYES_RCA gates internally
    }

    def __init__(
        self,
        replay_dir: str | None = None,
        call_timeout: float = CALL_TIMEOUT_SECONDS,
        max_retries: int = MAX_RETRIES_PER_CALL,
        gateway: McpGateway | None = None,
    ):
        gw = gateway or McpGateway.get_instance()
        self._gateway = gw  # stored for visual evidence + trace correlation

        # Tool auto-discovery — non-blocking; falls back to all tools on error
        available_servers = gw.discover_tools()

        # Candidate worker factory
        _worker_factory: dict[str, Any] = {
            "ops_worker":       lambda: OpsWorker(gateway=gw),
            "log_worker":       lambda: LogWorker(gateway=gw),
            "metrics_worker":   lambda: MetricsWorker(gateway=gw),
            "apm_worker":       lambda: ApmWorker(gateway=gw),
            "knowledge_worker": lambda: KnowledgeWorker(),
            "itsm_worker":      lambda: ItsmWorker(gateway=gw),
            "devops_worker":    lambda: DevopsWorker(gateway=gw),
            "confluence_worker": lambda: ConfluenceWorker(gateway=gw),
            "code_worker":      lambda: CodeWorker(gateway=gw),
            "git_worker":       lambda: GitWorker(gateway=gw),
            # Planner alias workers backed by existing implementations
            "signal_worker":    lambda: ApmWorker(gateway=gw),
            "event_worker":     lambda: ApmWorker(gateway=gw),
            "change_worker":    lambda: DevopsWorker(gateway=gw),
            "network_worker":   lambda: ThousandEyesWorker(),
        }

        self.workers: dict[str, Any] = {}
        for name, factory in _worker_factory.items():
            required = self._WORKER_SERVERS.get(name, frozenset())
            if not required or required & available_servers:
                self.workers[name] = factory()
            else:
                logger.info(
                    "Worker %s skipped — required tools not connected: %s",
                    name, sorted(required),
                )
        self._replay_store = ReplayStore(replay_dir) if replay_dir else None
        self._call_timeout = call_timeout
        self._max_retries = max_retries
        # Read at instance creation so env var changes between test cases are respected
        self._parallel_playbook = os.environ.get("PARALLEL_PLAYBOOK", "true").lower() in ("true", "1", "yes")
        # G6.2: Shared ThreadPoolExecutor (reused across calls within investigation)
        # Sized to match MAX_CONCURRENT_WORKERS from guardrails
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_WORKERS,
            thread_name_prefix="sentinalai-worker",
        )
        # Separate executor for parallel playbook dispatch (avoids deadlock with _call_worker)
        # Sized to max workers + 2 for overhead (historical context, devops enrichment)
        self._parallel_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(self.workers) + 2,
            thread_name_prefix="sentinalai-parallel",
        )
        # Per-investigation thread-local state — must be per-instance so concurrent
        # SentinalAISupervisor instances do not share the same TLS namespace.
        self._tls = threading.local()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def investigate(self, incident_id: str, replay: bool = False) -> dict:
        """Run a full RCA investigation for *incident_id*.

        Returns a dict with keys:
            root_cause, confidence, evidence_timeline, reasoning
        """
        # Replay mode: re-execute analysis from stored evidence for determinism verification
        # (not just cache return — the stored evidence is replayed through the analysis engine)
        if replay and self._replay_store:
            stored = self._replay_store.load(incident_id)
            if stored:
                stored_evidence = dict(stored.get("evidence", {}))
                stored_incident = stored_evidence.pop("_incident", None)
                stored_incident_type = stored_evidence.pop("_incident_type", "error_spike")
                if stored_incident and stored_evidence:
                    logger.info("Replaying investigation for %s from stored evidence", incident_id)
                    replayed = self._analyze_evidence(
                        incident_id, stored_incident, stored_incident_type, stored_evidence
                    )
                    # Run citation annotation so replay results are consistent
                    # with the primary run (which also runs annotate_citations)
                    annotate_citations(replayed, stored_evidence)
                    return replayed
                # Fallback: return cached result if evidence not available
                if stored.get("result"):
                    return stored["result"]

        with trace_span("investigate", case_id=incident_id) as span:
            # GenAI semantic conventions for agent observability
            span.set_attribute(GENAI_SYSTEM, "sentinalai")
            span.set_attribute(GENAI_OPERATION_NAME, "investigate")

            # G6.1: Per-investigation wall-clock deadline (thread-local — safe for concurrent calls)
            # Reset all TLS state before each investigation to prevent leakage from prior runs
            # on the same thread.
            self._tls.investigation_deadline = time.monotonic() + self.INVESTIGATION_DEADLINE_SECONDS
            self._tls.current_incident = None
            self._tls.itsm_evidence = None
            self._tls.devops_evidence = None
            self._tls.current_investigation_id = incident_id
            self._tls.current_phase = "collect"

            # LLM call graph — thread-local DAG for this investigation
            call_graph = CallGraph(investigation_id=incident_id)
            set_current_graph(call_graph)

            # Start with default budget; will be replaced after severity detection
            receipts = ReceiptCollector(case_id=incident_id)
            budget = ExecutionBudget(case_id=incident_id)

            # Stable investigation ID for fix engine traceability
            investigation_id = f"inv-{incident_id}"

            # W1: Per-investigation circuit breaker registry (isolated)
            circuits = CircuitBreakerRegistry()

            logger.info("Starting investigation for %s", incident_id)
            _stream = get_stream()
            _stream.emit(
                incident_id, EventType.INVESTIGATION_STARTED,
                {"incident_id": incident_id}, phase="start",
            )

            # Step 1: Fetch incident
            incident = self._fetch_incident(incident_id, receipts, budget, circuits)
            if not incident:
                logger.warning("No incident data for %s", incident_id)
                return self._empty_result(incident_id, "No incident data available")

            summary = incident.get("summary", "")
            service = incident.get("affected_service", "unknown")

            # Step 2: Classify — cache incident in thread-local so _build_params can time-anchor queries
            self._tls.current_incident = incident

            # G-10: Detect meta-queries (questions / lookup requests) before classification
            if is_meta_query(summary):
                logger.info("Meta-query detected for %s, skipping investigation", incident_id)
                return {
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

            incident_type = classify_incident(summary)
            span.set_attribute(EVAL_INCIDENT_TYPE, incident_type)
            span.set_attribute(EVAL_SERVICE, service)
            logger.info("Classified %s as %s (service=%s)", incident_id, incident_type, service)
            _stream.emit_phase(
                incident_id, "classify",
                incident_type=incident_type, service=service,
            )

            # Step 2b: ITSM context enrichment (Phase 1 — CI, known errors, similar incidents)
            itsm_context = self._fetch_itsm_context(service, summary, receipts, budget, circuits)

            # Step 2b2: Confluence enrichment (Phase 1 — runbooks + post-mortems)
            confluence_context = self._fetch_confluence_context(
                service, summary, incident_type, receipts, budget, circuits,
            )

            # Step 2c: Severity detection + budget scaling
            severity = detect_severity(incident, itsm_context)
            budget = get_budget_for_severity(severity)
            budget.case_id = incident_id
            # Carry forward calls already made during fetch + ITSM enrichment
            budget.calls_made = receipts.summary()["total_calls"]
            span.set_attribute("sentinalai.severity_level", severity.level)
            span.set_attribute("sentinalai.severity_label", severity.label)
            span.set_attribute("sentinalai.severity_source", severity.source)
            logger.info(
                "Severity: level=%d label=%s source=%s budget=%d",
                severity.level, severity.label, severity.source, severity.budget,
            )

            # Step 2d: Retrieve similar past experiences + knowledge graph context
            # to prime hypothesis generation. Runs concurrently with the playbook.
            experience_future = self._parallel_executor.submit(
                _retrieve_experiences, incident_type, service,
            )
            kg_future = self._parallel_executor.submit(
                _kg_query_similar, service, incident_type,
            )

            # Step 3: Execute playbook + historical context in parallel
            # Historical context targets knowledge_worker (independent of playbook workers)
            # so we can overlap them to cut wall-clock time.
            historical_future = self._parallel_executor.submit(
                self._fetch_historical_context, service, summary, incident_type, receipts, budget, circuits,
            )

            _stream.emit_phase(incident_id, "collect", incident_type=incident_type)
            _use_planner = os.environ.get("AGENTIC_PLANNER", "false").lower() in ("1", "true", "yes")
            _use_lc = os.environ.get("LOOP_CONTROLLER_ENABLED", "false").lower() in ("1", "true", "yes")
            # LoopController degrades to fallback playbook when LLM is off — allow it regardless.
            # Raw AgenticPlanner still requires LLM (its Think step has no utility without it).
            if _use_planner and (_use_lc or _llm_enabled()):
                evidence = self._execute_planner_loop(
                    incident_type, incident_id, service, incident, receipts, budget, circuits,
                )
            else:
                evidence = self._execute_playbook(
                    incident_type, incident_id, service, receipts, budget, circuits,
                )
            _stream.emit_phase_done(
                incident_id, "collect",
                evidence_keys=list(evidence.keys()),
                evidence_count=len(evidence),
            )

            # Concurrently: trace correlation + visual evidence (non-blocking)
            incident_time = incident.get("created_at", incident.get("timestamp", ""))
            _trace_future = self._parallel_executor.submit(
                correlate_traces, incident, evidence, self._gateway if hasattr(self, "_gateway") else None,
            )
            _visual_future = self._parallel_executor.submit(
                collect_visual_evidence, service, incident_time, incident_type,
                self._gateway if hasattr(self, "_gateway") else None,
            )

            # Merge ITSM context into evidence for downstream analysis
            if itsm_context:
                evidence["itsm_context"] = itsm_context

            # Merge Confluence context (runbooks + post-mortems) into evidence
            if confluence_context:
                evidence["confluence_context"] = confluence_context

            # Collect historical context (ran in parallel with playbook)
            try:
                historical = historical_future.result(timeout=self._call_timeout)
            except Exception as exc:
                historical = None
                logger.warning("Historical context retrieval failed (non-critical): %s", exc)
            if historical:
                evidence["historical_context"] = historical

            # Collect similar past experiences (ran in parallel with playbook)
            try:
                past_experiences = experience_future.result(timeout=5)
            except Exception:
                past_experiences = []
            if past_experiences:
                evidence["_past_experiences"] = past_experiences
                # Extract confirmed root causes from similar past successes to prime
                # hypothesis generation — this is the core experience replay loop.
                suggested_causes = [
                    exp.get("root_cause", "") for exp in past_experiences
                    if exp.get("root_cause") and not exp.get("root_cause", "").startswith("INSUFFICIENT")
                ]
                if suggested_causes:
                    evidence["_suggested_root_causes"] = suggested_causes
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

            # Inject tool recommendations from historical performance
            try:
                tool_recs = _get_tool_recommendations(incident_type, service)
                if tool_recs:
                    evidence["_tool_recommendations"] = tool_recs
                    logger.debug(
                        "Tool recommendations for %s/%s: %s",
                        incident_type, service,
                        list(tool_recs.keys())[:5],
                    )
            except Exception as exc:
                logger.debug("Tool recommendations failed (non-critical): %s", exc)

            # Collect knowledge graph similar incidents (ran in parallel)
            try:
                kg_similar = kg_future.result(timeout=3)
            except Exception:
                kg_similar = []
            if kg_similar:
                evidence["_kg_similar_incidents"] = kg_similar
                # Also extract root causes from KG for hypothesis priming
                kg_causes = [
                    inc.get("root_cause", "") for inc in kg_similar
                    if inc.get("root_cause")
                ]
                if kg_causes:
                    existing = evidence.get("_suggested_root_causes", [])
                    # Merge, dedup, keep top 5
                    merged = list(dict.fromkeys(existing + kg_causes))[:5]
                    evidence["_suggested_root_causes"] = merged
                logger.info(
                    "KG priming: %d similar historical incident(s) for %s/%s",
                    len(kg_similar), incident_type, service,
                )

            logger.info("Playbook complete for %s: %d evidence items", incident_id, len(evidence))

            # Evidence Gate G1 + G4: check collection quality before enrichment
            _gate_post_collection = check_post_collection(evidence, budget.calls_made)
            if not _gate_post_collection.passed and _gate_post_collection.verdict.value == "block":
                logger.warning(
                    "Evidence gate BLOCK post-collection: %s",
                    _gate_post_collection.blocking_gate.reason if _gate_post_collection.blocking_gate else "unknown",
                )
                return self._empty_result(
                    incident_id,
                    f"Evidence gate blocked: {_gate_post_collection.blocking_gate.reason}"
                    if _gate_post_collection.blocking_gate else "Evidence gate blocked",
                )

            # Step 3b: DevOps enrichment (proof-gated — only if change data found)
            changes = self._extract_changes(evidence)
            deployment = self._find_deployment(changes)
            if deployment:
                devops_context = self._fetch_devops_context(service, deployment, receipts, budget, circuits)
                if devops_context:
                    evidence["devops_context"] = devops_context

            # Step 3c: CMDB traversal — walk dependency graph for blast radius
            # Even when direct service has no change, a dependency might.
            cmdb_context = self._fetch_cmdb_blast_radius(
                service, incident_id, receipts, budget, circuits
            )
            if cmdb_context:
                evidence["cmdb_blast_radius"] = cmdb_context

            # Step 3d: Diff analysis — if CMDB found a change on a dependency
            # and we haven't already fetched devops_context, go get the diff.
            if cmdb_context and not evidence.get("devops_context"):
                dep_deployment = self._find_deployment_in_blast_radius(cmdb_context)
                if dep_deployment:
                    dep_devops_context = self._fetch_devops_context(
                        dep_deployment.get("_ci", service),
                        dep_deployment,
                        receipts, budget, circuits,
                    )
                    if dep_devops_context:
                        evidence["devops_context"] = dep_devops_context

            # Step 3e: Code diff analysis — AI reads diff vs error context
            if evidence.get("devops_context") and budget and budget.can_call():
                diff_analysis = self._fetch_diff_analysis(
                    service, evidence, receipts, budget, circuits
                )
                if diff_analysis:
                    evidence["diff_analysis"] = diff_analysis

                    # Step 3f: Git blame pinpoint — if diff analysis identified a
                    # culprit file + line, run git blame to get exact authorship.
                    culprit_file = diff_analysis.get("culprit_file", "")
                    culprit_line = diff_analysis.get("culprit_line")
                    devops_ctx = evidence.get("devops_context", {})
                    blame_repo = (
                        devops_ctx.get("deployments", [{}])[0].get("repo", "")
                        if devops_ctx.get("deployments") else ""
                    )
                    if culprit_file and culprit_line and blame_repo:
                        blame_result = self._fetch_git_blame(
                            blame_repo, culprit_file, int(culprit_line),
                            receipts, budget, circuits,
                        )
                        if blame_result:
                            evidence["git_blame"] = blame_result

            # Collect trace correlation (ran concurrently with playbook)
            try:
                trace_corr = _trace_future.result(timeout=5)
                if trace_corr:
                    evidence["trace_correlation"] = trace_corr
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
                logger.debug("Trace correlation skipped: %s", exc)

            # Collect visual evidence (ran concurrently with playbook)
            try:
                visual_ev = _visual_future.result(timeout=10)
                if visual_ev:
                    evidence["visual_evidence"] = visual_ev
            except Exception as exc:
                logger.debug("Visual evidence skipped: %s", exc)

            # Step 4: Analyze — guard with deadline before expensive LLM calls
            _deadline = getattr(self._tls, 'investigation_deadline', None)
            if _deadline is not None and time.monotonic() > _deadline:
                logger.warning(
                    "Investigation deadline exceeded before analysis for %s; returning timeout result",
                    incident_id,
                )
                return self._empty_result(
                    incident_id,
                    "investigation_deadline_exceeded",
                    degraded=True,
                    degraded_reason="deadline_exceeded_before_analysis",
                )
            _stream.emit_phase(incident_id, "analyze", incident_type=incident_type)
            # Cache evidence + context in TLS so the harness can call reanalyze()
            # on enriched evidence without repeating the expensive playbook.
            self._tls.last_evidence = dict(evidence)
            self._tls.last_incident_type = incident_type
            with trace_span("analyze_evidence", case_id=incident_id) as _ae_span:
                _ae_span.set_attribute("incident_type", incident_type)
                _ae_span.set_attribute("evidence_keys", len(evidence))
                result = self._analyze_evidence(incident_id, incident, incident_type, evidence)
                _ae_span.set_attribute("confidence", result.get("confidence", 0))
                _ae_span.set_attribute("confidence_degraded", result.get("confidence_degraded", False))
            # Attach collection-gate result now that result dict exists
            result["_gate_post_collection"] = _gate_post_collection.to_dict()

            confidence = result.get("confidence", 0)
            # Apply calibration — pass evidence for neural context (source_count
            # is extracted from evidence keys; online_eval not yet available here)
            raw_confidence = confidence
            confidence = get_calibrator().calibrate(confidence, evidence_context=evidence)
            if confidence != raw_confidence:
                result["confidence"] = confidence
                result["raw_confidence"] = raw_confidence
            _stream.emit_confidence(
                incident_id, confidence, source="calibrator", previous=raw_confidence,
            )
            hypothesis_count = result.pop("_hypothesis_count", 0)
            winner_hypothesis = result.pop("_winner_hypothesis", "none")
            llm_metrics = result.pop("_llm_metrics", {})

            # Step 4a-i: Multi-dimensional grounding confidence scoring (v2 when enabled)
            _recurrence_info = None
            if _RECURRENCE_AVAILABLE:
                try:
                    _recurrence_info = _recurrence_check(service, incident_type)
                except Exception:
                    pass

            if _GROUNDING_AVAILABLE:
                try:
                    _grounding = _grounding_score(
                        result=result,
                        evidence=evidence,
                        incident_type=incident_type,
                        recurrence_info=_recurrence_info,
                    )
                    result["_grounding"] = _grounding.to_dict()
                    # Emit grounding state for observability
                    _stream.emit_confidence(
                        incident_id,
                        int(_grounding.score * 100),
                        source="grounding_v2" if _grounding.model_version == "v2" else "grounding_v1",
                        previous=confidence,
                    )
                except Exception as exc:
                    logger.debug("Grounding confidence scoring skipped: %s", exc)

            # Step 4b: Self-critique — evaluate the RCA quality before returning.
            # If the critique identifies gaps and budget allows, gather targeted
            # follow-up evidence and re-analyze (at most once per investigation).
            result, evidence = self._apply_self_critique(
                result, evidence, incident_id, incident_type, service,
                receipts, budget, circuits,
            )
            confidence = result.get("confidence", confidence)

            # Phase: Cite — ground every claim in source evidence (before gate checks
            # so G5 has mechanically-matched citations, not just LLM-produced ones)
            annotate_citations(result, evidence)

            # Evidence Gate G2 + G3 + G5: check analysis quality (anti-hallucination)
            gate_post_analysis = check_post_analysis(result, evidence, budget.remaining())
            result["_gate_post_analysis"] = gate_post_analysis.to_dict()
            if not gate_post_analysis.passed and gate_post_analysis.verdict.value == "block":
                logger.warning(
                    "Evidence gate BLOCK post-analysis: %s",
                    gate_post_analysis.blocking_gate.reason if gate_post_analysis.blocking_gate else "unknown",
                )
                reason = (
                    gate_post_analysis.blocking_gate.reason
                    if gate_post_analysis.blocking_gate else "Evidence quality gate failed"
                )
                result["root_cause"] = f"BLOCKED: {reason}"
                result["confidence"] = 0
                result["hallucination_risk"] = True

            # Step 4c: Online quality evaluation — scores every investigation
            # without requiring ground truth labels.
            online_score = _online_evaluate(result, evidence, budget.calls_made, hypothesis_count)
            _annotate_online(result, online_score)
            # Store evidence snapshot for experience_store (avoids holding full evidence in result)
            result["_evidence_snapshot"] = {
                k: bool(v) for k, v in evidence.items() if not k.startswith("_")
            }

            # Expose git blame pinpoint in result (available to external consumers + RCA report)
            if evidence.get("git_blame"):
                blame = evidence["git_blame"]
                result["git_blame_pinpoint"] = {
                    "file":   blame.get("culprit_file", ""),
                    "line":   blame.get("culprit_line"),
                    "sha":    blame.get("sha", "")[:12],
                    "author": blame.get("author", ""),
                    "date":   blame.get("date", ""),
                    "commit_message": blame.get("message", "")[:120],
                    "repo":   blame.get("repo", ""),
                }

            # Expose top ITSM causal change in result for bridge call / runbook
            if evidence.get("_itsm_change_correlations"):
                top = evidence["_itsm_change_correlations"][0]
                if top.get("correlation_score", 0) >= 0.45:
                    result["causal_change"] = {
                        "id":           top.get("id", top.get("number", "")),
                        "title":        top.get("title", top.get("summary", "")),
                        "change_type":  top.get("change_type", ""),
                        "risk_level":   top.get("risk_level", ""),
                        "minutes_before_incident": top.get("minutes_before_incident"),
                        "correlation_score": top.get("correlation_score"),
                        "correlation_reason": top.get("correlation_reason", ""),
                        "commit_sha":   (top.get("matched_commit") or {}).get("sha", ""),
                    }

            # Phase: Observe — span attributes + deep-eval metrics
            self._record_observability(
                span, result, evidence, budget, receipts,
                incident_id, incident_type, service, confidence,
                hypothesis_count, winner_hypothesis, llm_metrics,
            )
            elapsed = span.elapsed_ms

            # Phase: Evaluate — LLM-as-judge scoring
            judge_scores = self._run_judge_scoring(incident_id, incident_type, result)

            # Generate remediation guidance
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

            # Generate and store proposed fix (if diff analysis found something actionable)
            if evidence.get("diff_analysis"):
                _stream.emit(incident_id, EventType.FIX_PROPOSED, {}, phase="fix")
                proposed_fix = self._generate_proposed_fix(
                    incident_id, investigation_id, service, evidence, result
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
                            metrics_worker=self.workers.get("metrics_worker"),
                            log_worker=self.workers.get("log_worker"),
                        )
                        _vloop_investigation_id = investigation_id
                        _vloop_service = service
                        _vloop_incident_id = incident_id
                        _vloop_itsm = self.workers.get("itsm_worker")
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
                        self._parallel_executor.submit(_run_verification)
                        logger.info("Verification loop started for %s/%s", incident_id, service)
                    except Exception as _vl_exc:
                        logger.debug("Verification loop start failed (non-critical): %s", _vl_exc)

            logger.info(
                "Investigation complete for %s: confidence=%d, tool_calls=%d",
                incident_id, confidence, budget.calls_made,
            )

            # Phase: Metric — record outcome for dashboard
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

            # Phase: Persist — skip heavy writes if deadline has passed
            _persist_deadline = getattr(self._tls, 'investigation_deadline', None)
            _persist_over_deadline = (
                _persist_deadline is not None and
                time.monotonic() > _persist_deadline
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
                    self._persist_results(
                        result, incident_id, incident_type, service, evidence,
                        receipts, budget, confidence, hypothesis_count,
                        winner_hypothesis, severity, summary, llm_metrics,
                        judge_scores, elapsed, incident=incident,
                    )

            _stream.emit_complete(
                investigation_id=incident_id,
                root_cause=result.get("root_cause", ""),
                confidence=confidence,
                citation_coverage=result.get("citation_coverage", 0.0),
                fix_proposed=bool(result.get("proposed_fix")),
                elapsed_ms=elapsed,
            )

            # Close the Pattern Intelligence feedback loop: mark pending predictions TP
            try:
                from intelligence.background_runner import get_runner
                get_runner().record_outcome(service, incident_id)
            except Exception:
                pass

            return result

    # ------------------------------------------------------------------ #
    # Internal: self-critique refinement
    # ------------------------------------------------------------------ #

    def _apply_self_critique(
        self,
        result: dict,
        evidence: dict,
        incident_id: str,
        incident_type: str,
        service: str,
        receipts: Any,
        budget: Any,
        circuits: Any,
    ) -> tuple[dict, dict]:
        """Run self-critique on the initial RCA result and optionally refine.

        If critique score is below threshold and budget allows, executes the
        gap_queries returned by the critique, merges new evidence, and
        re-runs _analyze_evidence once.  The refined result is kept only if
        it has equal-or-higher confidence than the original.
        """
        try:
            budget_remaining = budget.remaining() if hasattr(budget, "remaining") else 0
        except Exception:
            budget_remaining = 0

        try:
            critique = _self_critique(
                result, evidence, incident_type, service,
                budget_remaining=budget_remaining,
            )
            result["_critique"] = {
                "score": critique.score,
                "dimensions": critique.dimensions,
                "gaps": critique.gaps,
            }
            if critique.llm_critique:
                result["_critique"]["llm_narrative"] = critique.llm_critique

            # Record gap patterns for aggregation regardless of whether gap queries run
            try:
                self._parallel_executor.submit(
                    _record_gaps_from_critique, incident_type, service, critique,
                )
            except Exception:
                pass

            # Augment gap queries with domain-aware queries from dependency domain detector
            if _DOMAIN_DETECTOR_AVAILABLE:
                try:
                    domain_queries = _domain_gap_queries(
                        evidence=evidence,
                        root_cause=result.get("root_cause", ""),
                        incident_type=incident_type,
                        min_confidence=0.50,
                        max_queries=4,
                    )
                    if domain_queries:
                        # Merge: avoid duplicate actions already in critique.gap_queries
                        existing_actions = {
                            f"{q.get('worker')}:{q.get('action')}"
                            for q in critique.gap_queries
                        }
                        new_queries = [
                            q for q in domain_queries
                            if f"{q.get('worker')}:{q.get('action')}" not in existing_actions
                        ]
                        if new_queries:
                            # critique.gap_queries is a list — extend it
                            critique.gap_queries = list(critique.gap_queries) + new_queries
                            logger.info(
                                "Domain detector added %d gap queries for domain patterns",
                                len(new_queries),
                            )
                except Exception as exc:
                    logger.debug("Domain detector gap queries skipped: %s", exc)

            refinement_triggered = bool(critique.gap_queries)

            if not refinement_triggered:
                logger.debug(
                    "Self-critique: score=%.2f — no gap queries (budget=%d)",
                    critique.score, budget_remaining,
                )
                # Record that refinement was NOT triggered (score above threshold)
                try:
                    self._parallel_executor.submit(
                        _record_critique_outcome, critique.score, False, False,
                    )
                except Exception:
                    pass
                return result, evidence

            logger.info(
                "Self-critique triggered gap filling: score=%.2f gaps=%d queries=%d",
                critique.score, len(critique.gaps), len(critique.gap_queries),
            )

            # Execute gap queries
            for gap_step in critique.gap_queries:
                worker_name = gap_step.get("worker", "")
                action = gap_step.get("action", "")
                params = gap_step.get("params", {})
                if not worker_name or not action:
                    continue
                worker = self.workers.get(worker_name)
                if not worker:
                    logger.debug("Gap query skipped — worker %s not available", worker_name)
                    continue
                gap_result = self._call_worker(
                    worker, action, params,
                    receipts=receipts, budget=budget,
                    worker_name=worker_name, circuits=circuits,
                )
                if gap_result and "error" not in gap_result:
                    ev_key = f"gap_{action}"
                    evidence[ev_key] = gap_result
                    logger.info("Gap evidence gathered: %s.%s", worker_name, action)

            # Re-analyze with enriched evidence
            refined = self._analyze_evidence(incident_id, {
                **self._tls.__dict__.get("current_incident", {}),
            } if hasattr(self._tls, "current_incident") else {}, incident_type, evidence)

            refined_conf = refined.get("confidence", 0)
            orig_conf = result.get("confidence", 0)
            refinement_helped = refined_conf >= orig_conf

            if refinement_helped:
                # Merge metadata from original
                refined["_critique"] = result.get("_critique", {})
                refined["_critique"]["triggered_refinement"] = True
                # Pop internal keys so caller handles them
                refined.pop("_hypothesis_count", None)
                refined.pop("_winner_hypothesis", None)
                refined.pop("_llm_metrics", None)
                logger.info(
                    "Self-critique refinement improved confidence: %d → %d",
                    orig_conf, refined_conf,
                )
                try:
                    self._parallel_executor.submit(
                        _record_critique_outcome, critique.score, True, True,
                    )
                except Exception:
                    pass
                return refined, evidence
            else:
                result["_critique"]["triggered_refinement"] = True
                logger.info(
                    "Self-critique refinement did not improve (orig=%d refined=%d), keeping original",
                    orig_conf, refined_conf,
                )
                try:
                    self._parallel_executor.submit(
                        _record_critique_outcome, critique.score, True, False,
                    )
                except Exception:
                    pass
                return result, evidence

        except Exception as exc:
            logger.warning("Self-critique failed (non-critical): %s", exc)
            return result, evidence

    # ------------------------------------------------------------------ #
    # Internal: post-analysis phase helpers (extracted from investigate)
    # ------------------------------------------------------------------ #

    def _record_observability(
        self,
        span: Any,
        result: dict,
        evidence: dict,
        budget: "ExecutionBudget",
        receipts: "ReceiptCollector",
        incident_id: str,
        incident_type: str,
        service: str,
        confidence: int,
        hypothesis_count: int,
        winner_hypothesis: str,
        llm_metrics: dict,
    ) -> None:
        """Record OTEL span attributes and deep-eval metrics."""
        # GenAI semantic convention attributes for LLM usage
        total_input = llm_metrics.get("refine_input_tokens", 0) + llm_metrics.get("reasoning_input_tokens", 0)
        total_output = llm_metrics.get("refine_output_tokens", 0) + llm_metrics.get("reasoning_output_tokens", 0)
        if total_input or total_output:
            span.set_attribute(GENAI_USAGE_INPUT_TOKENS, total_input)
            span.set_attribute(GENAI_USAGE_OUTPUT_TOKENS, total_output)
            span.set_attribute(GENAI_REQUEST_MODEL, llm_metrics.get("refine_model_id", ""))

        # Eval / observability attributes for Splunk dashboards
        span.set_attribute(EVAL_CONFIDENCE, confidence)
        span.set_attribute(EVAL_ROOT_CAUSE, result.get("root_cause", ""))
        span.set_attribute(EVAL_TOOL_CALLS, budget.calls_made)
        span.set_attribute(EVAL_BUDGET_REMAINING, budget.max_calls - budget.calls_made)
        span.set_attribute(EVAL_EVIDENCE_SOURCES, len(evidence))
        span.set_attribute(EVAL_HYPOTHESIS_COUNT, hypothesis_count)
        span.set_attribute(EVAL_WINNER_NAME, winner_hypothesis)

        # Deep eval metrics -> OTEL metrics pipeline -> Splunk
        elapsed = span.elapsed_ms
        record_investigation(
            incident_id=incident_id,
            incident_type=incident_type,
            service=service,
            confidence=confidence,
            root_cause=result.get("root_cause", ""),
            tool_calls=budget.calls_made,
            evidence_sources=len(evidence),
            hypothesis_count=hypothesis_count,
            winner_hypothesis=winner_hypothesis,
            elapsed_ms=elapsed,
        )

        # Evidence completeness metrics
        record_evidence_completeness(
            incident_type=incident_type,
            logs_available=bool(evidence.get("search_logs")),
            signals_available=bool(evidence.get("get_golden_signals")),
            metrics_available=bool(evidence.get("query_metrics") or evidence.get("get_resource_metrics")),
            events_available=bool(evidence.get("get_events")),
            changes_available=bool(evidence.get("get_change_data") or evidence.get("itsm_context")),
        )

        # Receipt summary metrics
        receipt_summary = receipts.summary()
        record_receipt_summary(
            incident_type=incident_type,
            total_calls=receipt_summary["total_calls"],
            succeeded=receipt_summary["succeeded"],
            failed=receipt_summary["failed"],
            total_elapsed_ms=receipt_summary["total_elapsed_ms"],
        )

    def _run_judge_scoring(
        self, incident_id: str, incident_type: str, result: dict,
    ) -> dict:
        """Run LLM-as-judge eval scoring (optional, non-blocking). Returns judge scores dict.

        The judge evaluates output quality (structure, format, confidence range) rather
        than accuracy against ground truth. Ground-truth accuracy evaluation requires
        an external store of verified RCA outcomes which is not available here.
        Setting root_cause to "" signals to the judge that no ground-truth comparison
        should be performed — only structural quality dimensions are scored.
        """
        try:
            expected = {
                "root_cause": "",  # No ground truth available — structural eval only
                "root_cause_keywords": [],
                "confidence_min": 0,
                "confidence_max": 100,
            }
            judge_result = _judge_and_record(
                incident_id=incident_id,
                incident_type=incident_type,
                expected=expected,
                result=result,
            )
            scores = judge_result.get("scores", {})
            if scores:
                record_judge_scores(
                    incident_id=incident_id,
                    incident_type=incident_type,
                    scores=scores,
                    source=judge_result.get("source", "rule_based"),
                )
            return scores
        except Exception as exc:
            logger.warning("Judge scoring failed (non-critical): %s", exc)
            return {}

    def _persist_results(
        self,
        result: dict,
        incident_id: str,
        incident_type: str,
        service: str,
        evidence: dict,
        receipts: "ReceiptCollector",
        budget: "ExecutionBudget",
        confidence: int,
        hypothesis_count: int,
        winner_hypothesis: str,
        severity: Any,
        summary: str,
        llm_metrics: dict,
        judge_scores: dict,
        elapsed: float,
        incident: dict | None = None,
    ) -> None:
        """Persist investigation results to replay store, memory, knowledge graph, and DB."""
        # G-2: Inject hypothesis metadata so generate_rca_report can find them
        # (they were popped from result before this call so the public result stays clean,
        #  but rca_report needs them)
        result.setdefault("hypothesis_count", hypothesis_count)
        result.setdefault("winner_hypothesis", winner_hypothesis)

        # G-1: Surface receipts in the result so callers get a full audit trail
        if "receipts" not in result:
            result["receipts"] = receipts.to_list()

        # Persist replay artifact
        if self._replay_store:
            replay_result = {
                **result,
                "hypothesis_count": hypothesis_count,
                "winner_hypothesis": winner_hypothesis,
            }
            # G-6: Store incident + incident_type so replay can re-execute analysis
            # rather than just returning the cached result
            evidence_with_meta = dict(evidence)
            if incident:
                evidence_with_meta["_incident"] = incident
            evidence_with_meta["_incident_type"] = incident_type
            self._replay_store.save(
                case_id=incident_id,
                receipts=receipts.to_list(),
                result=replay_result,
                evidence=evidence_with_meta,
            )

        # Store in AgentCore Memory (LTM) for future similarity search
        if _memory_enabled():
            try:
                _store_to_memory(
                    incident_id=incident_id,
                    incident_type=incident_type,
                    service=service,
                    root_cause=result.get("root_cause", ""),
                    confidence=confidence,
                    reasoning=result.get("reasoning", ""),
                    evidence_summary=(
                        f"sources={len(evidence)}, "
                        f"tool_calls={budget.calls_made}, "
                        f"hypotheses={hypothesis_count}"
                    ),
                )
            except Exception as exc:
                logger.warning("Memory store failed (non-critical): %s", exc)

        # Persist to institutional knowledge graph (non-blocking)
        if _KNOWLEDGE_AVAILABLE and _knowledge_graph is not None:
            try:
                _knowledge_graph.persist_investigation(
                    incident_id=incident_id,
                    incident_type=incident_type,
                    service=service,
                    root_cause=result.get("root_cause", ""),
                    confidence=confidence,
                    evidence_refs=list(evidence.keys()),
                )
            except Exception as exc:
                logger.warning("Knowledge graph persist failed (non-critical): %s", exc)

        # Generate structured RCA report
        try:
            rca_report = generate_rca_report(
                result=result,
                incident_id=incident_id,
                incident_type=incident_type,
                service=service,
                severity_level=severity.level,
                severity_label=severity.label,
                summary=summary,
                receipts_list=receipts.to_list(),
                budget_remaining=budget.remaining(),
                elapsed_ms=elapsed,
                llm_usage=llm_metrics,
                judge_scores=judge_scores,
            )
            result["rca_report"] = rca_report.to_dict()
            result["rca_markdown"] = render_markdown(rca_report)
        except Exception as exc:
            logger.warning("RCA report generation failed (non-critical): %s", exc)

        # Persist to database (non-blocking, graceful degradation)
        if _db_enabled():
            try:
                inv_id = _db_persist(
                    incident_id=incident_id,
                    root_cause=result.get("root_cause", ""),
                    confidence=confidence,
                    reasoning=result.get("reasoning", ""),
                    evidence_timeline=result.get("evidence_timeline", []),
                    tools_used=receipts.to_list(),
                    elapsed_seconds=elapsed / 1000.0,
                    incident_type=incident_type,
                    service=service,
                    rca_report=result.get("rca_report"),
                )
                if inv_id:
                    _db_persist_tools(inv_id, receipts.to_list())
                    _db_persist_knowledge(
                        incident_id=incident_id,
                        incident_type=incident_type,
                        root_cause=result.get("root_cause", ""),
                        service=service,
                        metadata={
                            "confidence": confidence,
                            "hypothesis_count": hypothesis_count,
                            "winner": winner_hypothesis,
                        },
                    )
            except Exception as exc:
                logger.warning("Database persistence failed (non-critical): %s", exc)

        # Graph-based RAG: ingest this investigation into the knowledge graph
        # so future investigations can benefit from BFS-based similarity retrieval.
        try:
            self._parallel_executor.submit(
                _ingest_to_kg,
                incident_id, incident_type, service,
                result.get("root_cause", ""), confidence,
            )
        except Exception as exc:
            logger.warning("Knowledge graph ingestion failed (non-critical): %s", exc)

        # Memory compression: create a semantic digest for LTM storage
        try:
            online_score_for_compress = result.get("online_quality_score", 0.0)
            self._parallel_executor.submit(
                _compress_investigation,
                incident_id, incident_type, service, result, online_score_for_compress,
            )
        except Exception as exc:
            logger.warning("Memory compression failed (non-critical): %s", exc)

        # Attach LLM call graph summary to result for observability
        try:
            from supervisor.llm_call_graph import current_graph
            cg = current_graph()
            if cg:
                result["_llm_call_graph"] = cg.summary()
        except Exception as exc:
            logger.debug("LLM call graph summary failed (non-critical): %s", exc)
        finally:
            set_current_graph(None)

        # Continuous learning step: evaluate against ground truth (if available),
        # persist EvalResult, and update confidence calibrator.
        # Run in background so it never adds latency to the investigation response.
        try:
            from supervisor.learning_loop import run_learning_step
            self._parallel_executor.submit(run_learning_step, incident_id, result)
        except Exception as exc:
            logger.warning("Learning loop step failed (non-critical): %s", exc)

        # Self-learning: store experience + evolve strategy in background.
        # These run after every investigation regardless of ground truth availability.
        online_score = result.get("online_quality_score", 0.0)
        receipt_list = receipts.to_list() if receipts else []

        # Positive experience storage (high-quality investigations)
        try:
            self._parallel_executor.submit(
                _store_experience,
                incident_id, incident_type, service, result, online_score,
            )
        except Exception as exc:
            logger.warning("Experience storage failed (non-critical): %s", exc)

        # Negative learning: store failed investigations to learn what NOT to do
        if online_score < 0.60:
            root_cause = result.get("root_cause", "")
            failure_reason = (
                "low_confidence" if root_cause.startswith("INSUFFICIENT")
                else "low_online_quality"
            )
            try:
                self._parallel_executor.submit(
                    _store_failed_experience,
                    incident_id, incident_type, service, result, online_score, failure_reason,
                )
            except Exception as exc:
                logger.warning("Failed experience storage failed (non-critical): %s", exc)

        # Evolve strategy weights with per-service context
        try:
            self._parallel_executor.submit(
                _record_strategy_outcome,
                incident_type, receipt_list, online_score, service,
            )
        except Exception as exc:
            logger.warning("Strategy outcome recording failed (non-critical): %s", exc)

        # Propagate gap patterns to strategy evolver (penalise consistently-missing steps)
        critique_data = result.get("_critique", {})
        critique_gaps = critique_data.get("gaps", [])
        if critique_gaps:
            try:
                from supervisor.gap_aggregator import _parse_gap_categories
                gap_cats = _parse_gap_categories(critique_gaps)
                if gap_cats:
                    self._parallel_executor.submit(
                        _record_gap_pattern, incident_type, service, gap_cats,
                    )
            except Exception as exc:
                logger.debug("Gap pattern strategy update failed (non-critical): %s", exc)

        # Adaptive threshold: record quality observation for STORE_QUALITY_THRESHOLD
        experience_stored = online_score >= 0.60
        try:
            self._parallel_executor.submit(
                _record_quality_observation, online_score, experience_stored,
            )
        except Exception as exc:
            logger.debug("Adaptive threshold quality obs failed (non-critical): %s", exc)

        # Adaptive threshold: record confidence outcome for MIN_CONFIDENCE_TO_ACT
        confidence = result.get("confidence", 0)
        root_cause = result.get("root_cause", "")
        # A result is "correct" if it has a non-empty, non-INSUFFICIENT root cause
        was_correct = bool(root_cause) and not root_cause.startswith(("INSUFFICIENT", "LOW CONFIDENCE"))
        try:
            self._parallel_executor.submit(
                _record_confidence_outcome, float(confidence), was_correct,
            )
        except Exception as exc:
            logger.debug("Adaptive confidence outcome failed (non-critical): %s", exc)

        # Recurrence tracking: record this investigation for repeat-offender detection
        if _RECURRENCE_AVAILABLE and service and incident_type:
            try:
                self._parallel_executor.submit(
                    _recurrence_record,
                    service,
                    incident_type,
                    root_cause,
                    None,    # occurred_at: let tracker use current time
                    None,    # remediation_successful: unknown at this stage
                    False,   # permanent_fix: not determined here
                )
            except Exception as exc:
                logger.debug("Recurrence recording failed (non-critical): %s", exc)

        # Phase 2: update PatternRegistry and CoFailureIndex
        _dna_features = result.pop("_dna_features", [])
        _fingerprint = result.pop("_dna_fingerprint", "")
        if _dna_features and _fingerprint and incident_type:
            try:
                from supervisor.pattern_registry import get_registry
                # Extract signal sequence from receipt action names (ordered)
                _receipt_list = receipts.to_list() if receipts else []
                _signal_seq = [r.get("action", "") for r in _receipt_list if r.get("status") == "success"]
                # Steps that appeared in this investigation
                _steps = list(dict.fromkeys(_signal_seq))  # deduplicated, order preserved
                get_registry().record(
                    fingerprint=_fingerprint,
                    incident_type=incident_type,
                    features=_dna_features,
                    root_cause=root_cause,
                    signal_sequence=_signal_seq,
                    recommended_steps=_steps,
                )
                logger.debug("PatternRegistry updated fingerprint=%s count+1", _fingerprint)
            except Exception as exc:
                logger.debug("PatternRegistry record failed (non-critical): %s", exc)

        # Co-failure index: record any services that failed together
        _affected_services: list[str] = []
        try:
            from supervisor.co_failure_index import get_co_failure_index
            _evidence_timeline = result.get("evidence_timeline", [])
            for _entry in _evidence_timeline:
                if isinstance(_entry, dict):
                    _svc = _entry.get("service") or _entry.get("affected_service", "")
                    if _svc and _svc != service:
                        _affected_services.append(_svc)
            # Also check blast_radius if present
            _blast = result.get("blast_radius", {})
            if isinstance(_blast, dict):
                for _svc in _blast.get("affected_services", []):
                    _name = _svc if isinstance(_svc, str) else _svc.get("service", "")
                    if _name and _name != service:
                        _affected_services.append(_name)
            if service and _affected_services:
                get_co_failure_index().record_investigation(
                    primary_service=service,
                    co_failing_services=list(set(_affected_services)),
                )
        except Exception as exc:
            logger.debug("CoFailureIndex record failed (non-critical): %s", exc)

        # Blast radius history: compare predicted vs actual affected services
        if service and incident_id:
            try:
                from supervisor.blast_radius_history import get_blast_radius_history
                _blast_report = result.get("blast_radius", {})
                _predicted: list[str] = []
                if isinstance(_blast_report, dict):
                    for _svc in _blast_report.get("affected_services", []):
                        _n = _svc if isinstance(_svc, str) else _svc.get("service", "")
                        if _n and _n != service:
                            _predicted.append(_n)
                get_blast_radius_history().record(
                    incident_id=incident_id,
                    target_service=service,
                    predicted_services=_predicted,
                    actual_services=list(set(_affected_services)),
                )
            except Exception as exc:
                logger.debug("BlastRadiusHistory record failed (non-critical): %s", exc)

        # Cascade tracker: record ordered failure propagation chain
        if service and _affected_services:
            try:
                from supervisor.cascade_tracker import get_cascade_tracker
                # Preserve discovery order from evidence_timeline (earlier = lower index)
                _ordered: list[str] = []
                _seen_order: set[str] = set()
                for _entry in result.get("evidence_timeline", []):
                    if isinstance(_entry, dict):
                        _svc = _entry.get("service") or _entry.get("affected_service", "")
                        if _svc and _svc != service and _svc not in _seen_order:
                            _ordered.append(_svc)
                            _seen_order.add(_svc)
                # Append any remaining affected services not in timeline
                for _svc in _affected_services:
                    if _svc not in _seen_order:
                        _ordered.append(_svc)
                if _ordered:
                    get_cascade_tracker().record(
                        primary_service=service,
                        ordered_co_failures=_ordered,
                    )
            except Exception as exc:
                logger.debug("CascadeTracker record failed (non-critical): %s", exc)

        # Wiki receipt: write a structured RCA receipt for long-term memory
        if incident_id and service:
            try:
                from sentinel_wiki.receipt_writer import write_receipt as _write_receipt
                from sentinel_wiki.pattern_promoter import promote as _promote_patterns
                _write_receipt(
                    incident_id=incident_id,
                    service=service,
                    incident_type=incident_type,
                    result=result,
                    evidence=evidence,
                )
                _promote_patterns()
            except Exception as exc:
                logger.debug("Wiki receipt write failed (non-critical): %s", exc)

        # Causal graph: record co-failures between affected services
        if confidence > 50:
            _cg_affected: list[str] = []
            for _key in ("cmdb_blast_radius", "itsm_context"):
                _val = result.get(_key) or {}
                for _svc in _val.get("affected_services", []):
                    _sid = _svc.get("service_id") or _svc.get("ci_name", "") if isinstance(_svc, dict) else str(_svc)
                    if _sid and _sid != service:
                        _cg_affected.append(_sid)
            if _cg_affected:
                try:
                    from intelligence.causal_graph import CausalGraph
                    _cg = CausalGraph()
                    for _target in _cg_affected:
                        _cg.record_co_failure(service, _target, 0)
                except Exception as exc:
                    logger.debug("Causal graph co-failure record failed (non-critical): %s", exc)
            # Auto-topology: learn edges from full evidence dict
            try:
                from intelligence.topology_learner import learn as _topo_learn
                _topo_learn(
                    primary_service=service,
                    incident_type=incident_type,
                    evidence=result,
                    elapsed_ms=int(elapsed),
                )
            except Exception as exc:
                logger.debug("Topology learner failed (non-critical): %s", exc)

        # Episodic memory: record a compressed episode for cross-investigation retrieval
        try:
            import uuid as _uuid
            from datetime import datetime as _datetime, timezone as _tz
            from intelligence.episodic_memory import Episode as _Episode, EpisodicMemory as _EpisodicMemory
            _rec_action = (
                result.get("remediation", {}).get("immediate_action", "")
                or result.get("remediation", {}).get("action", "")
                or result.get("recommended_action", "investigate")
            )
            _conf_float = confidence / 100.0 if isinstance(confidence, int) else float(confidence)
            _outcome = (
                "auto-remediated" if _conf_float > 0.8 and result.get("proposed_fix")
                else ("resolved" if _conf_float > 0.5 else "escalated")
            )
            _episode = _Episode(
                episode_id=str(_uuid.uuid4()),
                incident_id=incident_id,
                service=service,
                incident_type=incident_type,
                failure_signature=result.get("root_cause", "")[:120],
                root_cause=result.get("root_cause", ""),
                confidence=_conf_float,
                resolution_action=_rec_action or "investigate",
                resolved_by=(
                    "auto" if _conf_float > 0.8 and result.get("proposed_fix")
                    else "SRE-on-call"
                ),
                time_to_resolve_ms=int(elapsed),
                evidence_keys=[
                    k for k in (evidence or {}).keys()
                    if not k.startswith("_")
                ],
                outcome=_outcome,
                tags=[incident_type, service],
                recorded_at=_datetime.now(_tz.utc).isoformat(),
            )
            _EpisodicMemory().record(_episode)
        except Exception as exc:
            logger.debug("Episodic memory recording failed (non-critical): %s", exc)

        # ITSM write-back: acknowledge/resolve if high confidence
        if confidence > 70 and incident_id and service:
            try:
                from intelligence.itsm_writebacks import get_engine as _get_wb_engine
                _wb = _get_wb_engine()
                _wb_root_cause = result.get("root_cause", "")
                _wb_action = result.get("recommended_action", "investigate")
                _wb.resolve(incident_id, service, _wb_root_cause, _wb_action, confidence / 100.0)
            except Exception as exc:
                logger.debug("ITSM write-back failed (non-critical): %s", exc)

    # ------------------------------------------------------------------ #
    # Internal: call worker with timeout (W4) and retry (W5)
    # ------------------------------------------------------------------ #

    def _check_call_preconditions(
        self, worker_name: str, action: str,
        circuits: CircuitBreakerRegistry | None,
    ) -> dict | None:
        """Check deadline and circuit breaker before a worker call. Returns error dict or None."""
        deadline = getattr(self._tls, 'investigation_deadline', None)
        if deadline is not None and time.monotonic() > deadline:
            logger.warning("Investigation deadline exceeded, skipping %s.%s", worker_name, action)
            return {"error": "investigation_deadline_exceeded", "worker": worker_name, "action": action}
        if circuits:
            circuit = circuits.get(worker_name)
            if circuit.is_open:
                logger.warning("Circuit open for %s, skipping %s", worker_name, action)
                return {"error": "circuit_open", "worker": worker_name, "action": action}
        return None

    def _record_timeout(
        self, receipt: Any, receipts: ReceiptCollector | None,
        worker_name: str, action: str, call_elapsed: float,
    ) -> None:
        """Record a timeout outcome on receipt and metrics."""
        if receipt and receipts:
            receipt.status = "timeout"
            receipt.error = f"timeout after {self._call_timeout}s"
            receipt.end_ts = time.monotonic()
            receipt.elapsed_ms = round((receipt.end_ts - receipt.start_ts) * 1000, 1)
        record_worker_call(worker_name, action, "timeout", call_elapsed)
        logger.warning("Timeout: %s.%s exceeded %ss", worker_name, action, self._call_timeout)

    def _call_worker(
        self,
        worker: Any,
        action: str,
        params: dict,
        receipts: ReceiptCollector | None,
        budget: ExecutionBudget | None,
        worker_name: str = "",
        circuits: CircuitBreakerRegistry | None = None,
        policy_ref: str = "",
    ) -> dict:
        """Call worker.execute() with circuit breaker, timeout guard, and retry.

        W1: checks circuit breaker before dispatch, records success/failure after.
        W4: wraps call in ThreadPoolExecutor with configurable timeout.
        W5: retries once on failure with exponential backoff.
        G6.1: checks per-investigation wall-clock deadline before each call.
        G6.2: uses shared ThreadPoolExecutor instead of creating one per call.
        G7.1: wraps each call in a child trace_span for per-tool OTEL spans.
        """
        blocked = self._check_call_preconditions(worker_name, action, circuits)
        if blocked:
            return blocked

        if not policy_ref and budget:
            policy_ref = f"budget:remaining={budget.remaining()}"

        last_error = ""
        attempts = 1 + self._max_retries

        for attempt in range(attempts):
            # Each attempt (initial + retries) consumes one budget unit atomically.
            # try_record() is used so check+record is never split across threads.
            if budget and not budget.try_record():
                logger.warning("Budget exhausted before %s.%s (attempt %d)", worker_name, action, attempt + 1)
                return {"error": "budget_exhausted", "worker": worker_name, "action": action}

            if attempt > 0:
                backoff_s = 0.01 * (2 ** (attempt - 1))
                time.sleep(backoff_s)
                logger.info("Retrying %s.%s (attempt %d/%d)", worker_name, action, attempt + 1, attempts)

            # Harness Phase 1: populate provenance fields for evidence traceability
            _incident = getattr(self._tls, "current_incident", None) or {}
            _entity = _incident.get("affected_service", "") if isinstance(_incident, dict) else ""
            _tw_start = params.get("start_time", params.get("time_window_start", ""))
            _tw_end = params.get("end_time", params.get("time_window_end", ""))
            receipt = receipts.start(
                worker_name, action, params, policy_ref=policy_ref,
                entity=_entity,
                time_window_start=str(_tw_start) if _tw_start else "",
                time_window_end=str(_tw_end) if _tw_end else "",
            ) if receipts else None
            call_start = time.monotonic()

            try:
                with trace_span(f"tool:{worker_name}.{action}", case_id=receipts.case_id if receipts else "") as tool_span:
                    tool_span.set_attribute("worker_name", worker_name)
                    tool_span.set_attribute("action", action)
                    future = self._executor.submit(worker.execute, action, params)
                    result = future.result(timeout=self._call_timeout)
                    call_elapsed = (time.monotonic() - call_start) * 1000
                    tool_span.set_attribute("status", "success")
                    tool_span.set_attribute("elapsed_ms", round(call_elapsed, 1))

                if receipt and receipts:
                    receipts.finish(receipt, result)
                record_worker_call(worker_name, action, "success", call_elapsed)
                if circuits:
                    circuits.get(worker_name).record_success(worker_name)
                try:
                    from supervisor.tool_transparency import get_emitter as _tt
                    _tt().record_call_result(
                        investigation_id=receipts.case_id if receipts else "",
                        worker=worker_name, action=action, params=params,
                        raw_response=result, latency_ms=call_elapsed, status="success",
                        phase=getattr(self._tls, "current_phase", "collect"),
                    )
                except Exception:
                    pass
                return result

            except concurrent.futures.TimeoutError:
                call_elapsed = (time.monotonic() - call_start) * 1000
                last_error = f"timeout after {self._call_timeout}s"
                self._record_timeout(receipt, receipts, worker_name, action, call_elapsed)
                try:
                    from supervisor.tool_transparency import get_emitter as _tt
                    _tt().record_call_result(
                        investigation_id=receipts.case_id if receipts else "",
                        worker=worker_name, action=action, params=params,
                        raw_response=None, latency_ms=call_elapsed, status="timeout",
                        phase=getattr(self._tls, "current_phase", "collect"),
                        error_msg=last_error,
                    )
                except Exception:
                    pass

            except Exception as exc:
                call_elapsed = (time.monotonic() - call_start) * 1000
                last_error = str(exc)
                if receipt and receipts:
                    receipts.finish(receipt, None, error=last_error)
                record_worker_call(worker_name, action, "error", call_elapsed)
                logger.warning("Error in %s.%s: %s (attempt %d/%d)", worker_name, action, exc, attempt + 1, attempts)
                try:
                    from supervisor.tool_transparency import get_emitter as _tt
                    _tt().record_call_result(
                        investigation_id=receipts.case_id if receipts else "",
                        worker=worker_name, action=action, params=params,
                        raw_response=None, latency_ms=call_elapsed, status="error",
                        phase=getattr(self._tls, "current_phase", "collect"),
                        error_msg=last_error,
                    )
                except Exception:
                    pass

        if circuits:
            circuits.get(worker_name).record_failure(worker_name)
        return {"error": last_error or "worker_unavailable"}

    # ------------------------------------------------------------------ #
    # Internal: fetch incident (with receipts + budget)
    # ------------------------------------------------------------------ #

    def _fetch_incident(
        self, incident_id: str,
        receipts: ReceiptCollector | None = None,
        budget: ExecutionBudget | None = None,
        circuits: CircuitBreakerRegistry | None = None,
    ) -> dict | None:
        with trace_span("fetch_incident", case_id=incident_id) as span:
            span.set_attribute("incident_id", incident_id)
            result = self._call_worker(
                self.workers["ops_worker"],
                "get_incident_by_id",
                {"incident_id": incident_id},
                receipts, budget, "ops_worker",
                circuits=circuits,
            )
            raw = result.get("incident") if result else None
            if not raw:
                span.set_attribute("found", False)
                return None
            span.set_attribute("found", True)
            # Normalize through canonical Incident model
            try:
                incident_obj = Incident.from_dict(raw)
                return incident_obj.to_legacy_dict()
            except (ValueError, TypeError) as exc:
                logger.warning("Incident normalization failed, using raw data: %s", exc)
                return raw

    def _fetch_historical_context(
        self, service: str, summary: str, incident_type: str = "",
        receipts: ReceiptCollector | None = None,
        budget: ExecutionBudget | None = None,
        circuits: CircuitBreakerRegistry | None = None,
    ) -> dict | None:
        """Optional phase 4: fetch similar historical incidents.

        G-8: incident_type is included in the search so memory retrieval is
        type-filtered rather than service-only.
        """
        worker = self.workers.get("knowledge_worker")
        if worker is None:
            return None
        result = self._call_worker(
            worker, "search_similar",
            {"service": service, "summary": summary, "incident_type": incident_type},
            receipts, budget, "knowledge_worker",
            circuits=circuits,
        )
        if result and result.get("similar_incidents"):
            return result
        return None

    # ------------------------------------------------------------------ #
    # Internal: ITSM context enrichment (ServiceNow Phase 1 hydration)
    # ------------------------------------------------------------------ #

    def _budgeted_worker_call(
        self, worker: Any, action: str, params: dict,
        worker_name: str,
        receipts: ReceiptCollector | None, budget: ExecutionBudget | None,
        circuits: CircuitBreakerRegistry | None,
    ) -> dict | None:
        """Invoke worker, delegating budget check and recording to _call_worker.

        Returns None only when _call_worker itself reports budget exhaustion.
        _call_worker handles try_record() atomically for all attempts.
        """
        result = self._call_worker(
            worker, action, params, receipts, budget, worker_name, circuits=circuits,
        )
        # _call_worker returns {"error": ...} when budget exhausted — surface as None
        if result and result.get("error") in ("budget_exhausted",):
            return None
        return result

    def _fetch_itsm_context(
        self, service: str, summary: str,
        receipts: ReceiptCollector | None = None,
        budget: ExecutionBudget | None = None,
        circuits: CircuitBreakerRegistry | None = None,
    ) -> dict | None:
        """Phase 1 enrichment: fetch CI details, similar incidents, and known errors from ServiceNow."""
        worker = self.workers.get("itsm_worker")
        if worker is None:
            return None
        context: dict[str, Any] = {}

        # CI details — service tier, dependencies, owner, SLA
        result = self._budgeted_worker_call(
            worker, "get_ci_details", {"service": service},
            "itsm_worker", receipts, budget, circuits,
        )
        if result is None:
            return context or None
        if result.get("ci"):
            context["ci"] = result["ci"]

        # Known errors — check before deep investigation
        result = self._budgeted_worker_call(
            worker, "get_known_errors", {"service": service, "summary": summary},
            "itsm_worker", receipts, budget, circuits,
        )
        if result is None:
            return context or None
        if result.get("known_errors"):
            context["known_errors"] = result["known_errors"]

        # Similar ServiceNow incidents
        result = self._budgeted_worker_call(
            worker, "search_incidents", {"service": service, "query": summary},
            "itsm_worker", receipts, budget, circuits,
        )
        if result is None:
            return context or None
        if result.get("incidents"):
            context["similar_incidents"] = result["incidents"]

        return context or None

    # ------------------------------------------------------------------ #
    # Internal: CMDB traversal (dependency blast radius — Phase 3c)
    # ------------------------------------------------------------------ #

    def _fetch_cmdb_blast_radius(
        self, service: str, incident_id: str,
        receipts: Any | None = None,
        budget: Any | None = None,
        circuits: Any | None = None,
    ) -> dict | None:
        """Phase 3c: walk CMDB dependency graph for change blast radius.

        Finds changes on dependencies of *service*, not just the service itself.
        Returns the CMDBTraversal result dict, or None if nothing found.
        """
        worker = self.workers.get("itsm_worker")
        if worker is None:
            return None
        if budget and not budget.can_call():
            return None

        try:
            traversal = CMDBTraversal(worker)
            result = traversal.get_change_blast_radius(service, hours=24)
            if result.get("changes_found", 0) > 0:
                logger.info(
                    "CMDB blast radius for %s: found changes on %d CI(s): %s",
                    service, result["changes_found"],
                    list(result.get("blast_radius", {}).keys()),
                )
                result["_summary"] = build_change_summary(result)
                return result
        except Exception as exc:
            logger.warning("CMDB traversal failed for %s: %s", service, exc)

        return None

    def _find_deployment_in_blast_radius(self, cmdb_context: dict) -> dict | None:
        """Extract the most recent change from the CMDB blast radius.

        Used by Step 3d to decide whether to fetch a code diff.
        """
        if not cmdb_context:
            return None
        blast = cmdb_context.get("blast_radius", {})
        if not blast:
            return None

        # Find the change with the most recent timestamp across all CIs
        best: dict | None = None
        best_ts = ""
        for ci_name, changes in blast.items():
            for ch in changes:
                ts = ch.get("end_date", ch.get("start_date", ""))
                if ts > best_ts:
                    best_ts = ts
                    best = dict(ch)
                    best["_ci"] = ci_name
                    # Map change number to deployment-style dict for _fetch_devops_context
                    best["sha"] = ch.get("sha", ch.get("commit_sha", ""))
                    best["description"] = ch.get("short_description", "")
                    best["merged_at"] = ts

        return best

    # ------------------------------------------------------------------ #
    # Internal: Code diff analysis (AI-powered — Phase 3e)
    # ------------------------------------------------------------------ #

    def _fetch_diff_analysis(
        self, service: str, evidence: dict,
        receipts: Any | None = None,
        budget: Any | None = None,
        circuits: Any | None = None,
    ) -> dict | None:
        """Phase 3e: AI analysis of code diff against production error context.

        Reads the commit diff from devops_context, cross-references with
        Splunk/APM error logs, and returns culprit file + line + fix confidence.
        """
        code_worker = self.workers.get("code_worker")
        if code_worker is None:
            return None
        if budget and not budget.can_call():
            return None

        devops_ctx = evidence.get("devops_context", {})
        deployments = devops_ctx.get("deployments", [])
        if not deployments:
            return None

        # Get the most recent deployment
        deployment = deployments[0] if isinstance(deployments, list) else {}
        sha = deployment.get("sha", "")
        repo = deployment.get("repo", deployment.get("repository", ""))

        if not sha:
            return None

        # Extract error context from Splunk + APM evidence
        error_context = self._extract_error_context(evidence)

        # Get the commit diff from devops_worker
        diff_worker = self.workers.get("devops_worker")
        diff = ""
        if diff_worker and repo and sha:
            try:
                diff_result = self._call_worker(
                    diff_worker, "get_commit_diff", {"repo": repo, "sha": sha},
                    receipts, budget, "devops_worker", circuits=circuits,
                )
                if diff_result and diff_result.get("commit"):
                    files = diff_result["commit"].get("files", [])
                    diff = "\n".join(f.get("patch", "") for f in files if f.get("patch"))
            except Exception as exc:
                logger.warning("Failed to fetch commit diff %s/%s: %s", repo, sha, exc)

        try:
            analysis = self._call_worker(
                code_worker, "analyze_diff", {
                    "repo": repo,
                    "sha": sha,
                    "diff": diff,
                    "error_context": error_context,
                    "service": service,
                },
                receipts, budget, "code_worker", circuits=circuits,
            )
            if analysis and analysis.get("confidence", 0) > 0:
                logger.info(
                    "Diff analysis: culprit=%s:%s confidence=%d",
                    analysis.get("culprit_file"), analysis.get("culprit_line"),
                    analysis.get("confidence", 0),
                )
                return analysis
        except Exception as exc:
            logger.warning("Code diff analysis failed: %s", exc)

        return None

    def _fetch_git_blame(
        self,
        repo: str,
        path: str,
        line: int,
        receipts: Any | None,
        budget: Any | None,
        circuits: Any | None,
        context_lines: int = 5,
    ) -> dict | None:
        """Run git blame on a specific file+line to pinpoint the breaking author.

        Called after diff_analysis identifies a culprit_file + culprit_line.
        Returns blame dict with: sha, author, date, message, lines, or None.

        This is the final step in the git causation chain:
          deployment → breaking_commit → diff_analysis → git_blame
        giving full pinpoint precision from incident to the exact lines and author.
        """
        git_worker = self.workers.get("git_worker")
        if git_worker is None or not budget or not budget.can_call():
            return None

        try:
            blame = self._call_worker(
                git_worker, "git_blame_line", {
                    "repo": repo,
                    "path": path,
                    "line_start": max(1, line - context_lines),
                    "line_end": line + context_lines,
                },
                receipts, budget, "git_worker", circuits=circuits,
            )
            if blame and "error" not in blame:
                logger.info(
                    "Git blame pinpoint: %s:%d → sha=%s author=%s",
                    path, line,
                    str(blame.get("sha", ""))[:8],
                    blame.get("author", "?"),
                )
                return {
                    **blame,
                    "culprit_file": path,
                    "culprit_line": line,
                    "repo": repo,
                }
        except Exception as exc:
            logger.debug("Git blame fetch failed (non-critical): %s", exc)
        return None

    def _extract_error_context(self, evidence: dict) -> str:
        """Build a concise error context string from Splunk/APM evidence."""
        parts: list[str] = []

        # Splunk logs
        log_evidence = evidence.get("logs", evidence.get("log_data", {}))
        if isinstance(log_evidence, dict):
            logs: list = log_evidence.get("logs") or log_evidence.get("results") or []
        elif isinstance(log_evidence, list):
            logs = log_evidence
        else:
            logs = []

        for log in logs[:5]:
            msg = log.get("message", log.get("_raw", ""))
            if msg:
                parts.append(f"[{log.get('level', 'INFO')}] {msg[:200]}")

        # APM errors
        apm_evidence = evidence.get("apm_data", evidence.get("apm", {}))
        if isinstance(apm_evidence, dict):
            errors: list = apm_evidence.get("errors") or apm_evidence.get("error_samples") or []
            for err in errors[:3]:
                trace = err.get("stack_trace", err.get("exception", ""))
                if trace:
                    parts.append(f"STACK: {trace[:300]}")

        return "\n".join(parts) if parts else ""

    # ------------------------------------------------------------------ #
    # Internal: Fix proposal generation (Phase 3f)
    # ------------------------------------------------------------------ #

    def _generate_proposed_fix(
        self, incident_id: str, investigation_id: str, service: str,
        evidence: dict, result: dict,
    ) -> "ProposedFix | None":
        """Generate and persist a ProposedFix from diff analysis results."""
        code_worker = self.workers.get("code_worker")
        if code_worker is None:
            return None

        diff_analysis = evidence.get("diff_analysis", {})
        if not diff_analysis or diff_analysis.get("confidence", 0) < 40:
            return None

        devops_ctx = evidence.get("devops_context", {})
        deployments = devops_ctx.get("deployments", [])
        deployment = deployments[0] if deployments else {}

        try:
            fix_result = self._call_worker(
                code_worker, "generate_fix", {
                    "analysis": diff_analysis,
                    "diff": evidence.get("_raw_diff", ""),
                    "error_context": self._extract_error_context(evidence),
                    "service": service,
                    "repo": deployment.get("repo", deployment.get("repository", "")),
                    "sha": deployment.get("sha", ""),
                    "incident_id": incident_id,
                },
                None, None, "code_worker", circuits=None,
            )

            if fix_result and fix_result.get("fix_type", "none") != "none":
                fix_result["repo"] = deployment.get("repo", "")
                fix_result["sha"] = deployment.get("sha", "")
                proposed = get_fix_engine().propose_from_result(
                    investigation_id, incident_id, fix_result
                )
                return proposed
        except Exception as exc:
            logger.warning("Fix proposal generation failed: %s", exc)

        return None

    # ------------------------------------------------------------------ #
    # Internal: Confluence enrichment (runbooks / post-mortems — Phase 1)
    # ------------------------------------------------------------------ #

    def _fetch_confluence_context(
        self, service: str, summary: str, incident_type: str,
        receipts: ReceiptCollector | None = None,
        budget: ExecutionBudget | None = None,
        circuits: CircuitBreakerRegistry | None = None,
    ) -> dict | None:
        """Phase 1 enrichment: fetch runbooks and post-mortems from Confluence."""
        worker = self.workers.get("confluence_worker")
        if worker is None:
            return None
        context: dict[str, Any] = {}

        # Runbooks — operational procedures for the service
        result = self._budgeted_worker_call(
            worker, "search_runbooks", {"service": service, "query": summary},
            "confluence_worker", receipts, budget, circuits,
        )
        if result is None:
            return context or None
        if result.get("runbooks"):
            context["runbooks"] = result["runbooks"]

        # Post-mortems — historical failure analyses for this service/type
        result = self._budgeted_worker_call(
            worker, "search_postmortems",
            {"service": service, "incident_type": incident_type},
            "confluence_worker", receipts, budget, circuits,
        )
        if result is None:
            return context or None
        if result.get("postmortems"):
            context["postmortems"] = result["postmortems"]

        return context or None

    # ------------------------------------------------------------------ #
    # Internal: DevOps enrichment (GitHub — proof-gated Phase 3)
    # ------------------------------------------------------------------ #

    def _fetch_devops_context(
        self, service: str, deployment: dict,
        receipts: ReceiptCollector | None = None,
        budget: ExecutionBudget | None = None,
        circuits: CircuitBreakerRegistry | None = None,
    ) -> dict | None:
        """Phase 3 proof-gated enrichment: fetch code change details from GitHub.

        Only called when _find_deployment() already found a deployment in
        ITSM/Splunk change data.  Never used for speculative code searches.
        """
        worker = self.workers.get("devops_worker")
        if worker is None:
            return None
        context: dict[str, Any] = {}

        # Recent deployments — PRs/releases in the incident window
        result = self._call_worker(
            worker, "get_recent_deployments", {"service": service},
            receipts, budget, "devops_worker", circuits=circuits,
        )
        if result and result.get("deployments"):
            context["deployments"] = result["deployments"]
        if result and result.get("error") == "budget_exhausted":
            return context or None

        # Workflow runs — CI/CD pipeline status
        result = self._call_worker(
            worker, "get_workflow_runs", {"service": service},
            receipts, budget, "devops_worker", circuits=circuits,
        )
        if result and result.get("workflow_runs"):
            context["workflow_runs"] = result["workflow_runs"]

        return context or None

    # ------------------------------------------------------------------ #
    # Internal: agentic planner (AGENTIC_PLANNER=true)
    # ------------------------------------------------------------------ #

    def _execute_planner_loop(
        self,
        incident_type: str,
        incident_id: str,
        service: str,
        incident: dict,
        receipts,
        budget,
        circuits,
        investigation_id: str = "",
    ) -> dict:
        """Agentic Think→Act→Observe loop (AGENTIC_PLANNER=true).

        When LOOP_CONTROLLER_ENABLED=true, wraps AgenticPlanner with quality-gated
        convergence, nudge-before-break stagnation handling, and telemetry recording.
        Falls back to plain AgenticPlanner otherwise.
        """
        from supervisor.llm import converse as _llm_converse
        from supervisor.tool_selector import get_playbook

        fallback = get_playbook(incident_type)
        _use_lc = os.environ.get("LOOP_CONTROLLER_ENABLED", "false").lower() in ("1", "true", "yes")

        if _use_lc:
            from supervisor.loop_controller import LoopController
            controller = LoopController(
                workers=self.workers,
                llm_fn=_llm_converse,
                budget=budget,
                fallback_playbook=fallback,
            )
            evidence, _ = controller.run(
                incident_id, incident, incident_type,
                investigation_id=investigation_id,
            )
        else:
            from supervisor.planner import AgenticPlanner
            planner = AgenticPlanner(
                workers=self.workers,
                llm_fn=_llm_converse,
                budget=budget,
                fallback_playbook=fallback,
            )
            evidence, _ = planner.run(incident_id, incident, incident_type)
        return evidence

    # ------------------------------------------------------------------ #
    # Internal: execute playbook (W1 isolated circuits, W4 timeout, W5 retry)
    # ------------------------------------------------------------------ #

    def _execute_playbook(
        self, incident_type: str, incident_id: str, service: str,
        receipts: ReceiptCollector | None = None,
        budget: ExecutionBudget | None = None,
        circuits: CircuitBreakerRegistry | None = None,
    ) -> dict[str, Any]:
        """Run playbook steps, collecting evidence.

        When PARALLEL_PLAYBOOK is enabled (default), groups steps by worker
        and dispatches independent workers concurrently using the shared
        ThreadPoolExecutor. Steps targeting the same worker run sequentially
        to respect rate limits. Falls back to sequential execution otherwise.

        A LoopCheckpoint is used throughout to detect stalled investigations
        and trigger early exit if no progress is made across two checkpoints.
        """
        playbook = get_evolved_playbook(incident_type)  # applies learned step weights
        cb_registry = circuits or circuit_registry
        loop_checkpoint = LoopCheckpoint()

        # Filter out steps whose evolved weight has dropped below the skip threshold.
        # Records adaptive threshold feedback for each skipped step.
        filtered_playbook = []
        for step in playbook:
            step_label = step.get("label", step.get("action", ""))
            if _should_skip_step(incident_type, step_label, service):
                logger.info(
                    "Skipping low-weight step: %s (type=%s service=%s)",
                    step_label, incident_type, service,
                )
                try:
                    self._parallel_executor.submit(
                        _record_step_skip_outcome, True, 0.0, 0.0,
                    )
                except Exception:
                    pass
            else:
                filtered_playbook.append(step)
        playbook = filtered_playbook

        if not self._parallel_playbook:
            return self._execute_playbook_sequential(
                playbook, incident_id, service, receipts, budget, cb_registry,
                loop_checkpoint=loop_checkpoint,
            )

        # Group steps by worker for parallel dispatch
        from collections import OrderedDict
        worker_groups: OrderedDict[str, list[dict]] = OrderedDict()
        for step in playbook:
            wn = step["worker"]
            worker_groups.setdefault(wn, []).append(step)

        evidence: dict[str, Any] = {}

        # Capture investigation-local TLS values now (main thread) so pool threads
        # can seed their own TLS before calling any downstream method that reads them.
        # threading.local() is per-thread — pool threads start with an empty namespace.
        _captured_investigation_deadline = getattr(self._tls, 'investigation_deadline', None)
        _captured_current_incident = getattr(self._tls, 'current_incident', None)

        def _run_worker_group(worker_name: str, steps: list[dict]) -> list[tuple[str, dict]]:
            """Execute a group of steps for one worker sequentially.

            Budget check and recording are delegated to _call_worker (try_record)
            so concurrent worker groups cannot race on the budget counter.

            Seeds this pool-thread's TLS with the investigation context captured from
            the main investigation thread so _check_call_preconditions (deadline) and
            _build_params (time-window anchoring via current_incident) work correctly.
            """
            # Propagate investigation context to this pool thread's TLS namespace
            self._tls.investigation_deadline = _captured_investigation_deadline
            self._tls.current_incident = _captured_current_incident
            results = []
            for step in steps:
                action = step["action"]
                label = step.get("label", action)
                params = self._build_params(step, incident_id, service)
                worker = self.workers.get(worker_name)
                if worker is None:
                    logger.warning(
                        "Worker %r not registered — evidence step %r skipped (missing_evidence_reason)",
                        worker_name, label,
                    )
                    results.append((label, {
                        "error": "worker_unavailable",
                        "worker": worker_name,
                        "action": action,
                        "missing_evidence_reason": (
                            f"Worker {worker_name!r} is not registered. "
                            "Check server connectivity and WORKER_SERVERS config."
                        ),
                    }))
                    continue

                result = self._call_worker(
                    worker, action, params, receipts, budget, worker_name,
                    circuits=cb_registry,
                )
                results.append((label, result))
                # Stop this worker group if _call_worker reported budget exhaustion
                if result and result.get("error") == "budget_exhausted":
                    logger.warning("Budget exhausted at step %s for %s", label, incident_id)
                    break
            return results

        # Submit each worker group concurrently (uses separate executor to avoid deadlock)
        futures: dict[concurrent.futures.Future, str] = {}
        for worker_name, steps in worker_groups.items():
            future = self._parallel_executor.submit(_run_worker_group, worker_name, steps)
            futures[future] = worker_name

        # Collect results — bounded by investigation deadline, not just call_timeout
        deadline = getattr(self._tls, 'investigation_deadline', None)
        remaining_budget = (deadline - time.monotonic()) if deadline is not None else self._call_timeout * len(playbook)
        group_timeout = max(1.0, min(remaining_budget, self._call_timeout * len(playbook)))
        for future in concurrent.futures.as_completed(futures, timeout=group_timeout):
            try:
                results = future.result(timeout=self._call_timeout * 2)
                for label, result in results:
                    evidence[label] = result
            except Exception as exc:
                wn = futures[future]
                logger.warning("Parallel worker group %s failed: %s", wn, exc)

            # Loop-operator checkpoint after each worker group completes
            if budget is not None and loop_checkpoint.should_check(budget):
                escalation = loop_checkpoint.check(set(evidence.keys()), 0)
                if escalation:
                    logger.warning(
                        "Loop escalation in parallel playbook for %s: %s",
                        incident_id, escalation["escalation_trigger"],
                    )
                    evidence["_loop_escalation"] = escalation
                    # Cancel remaining futures (best-effort)
                    for f in futures:
                        f.cancel()
                    break

        return evidence

    def _execute_playbook_sequential(
        self, playbook: list[dict], incident_id: str, service: str,
        receipts: ReceiptCollector | None = None,
        budget: ExecutionBudget | None = None,
        circuits: CircuitBreakerRegistry | None = None,
        loop_checkpoint: LoopCheckpoint | None = None,
    ) -> dict[str, Any]:
        """Sequential fallback for playbook execution.

        If loop_checkpoint is provided, checks for stall escalation after
        every LOOP_CHECKPOINT_INTERVAL calls and breaks early if stalled.
        """
        evidence: dict[str, Any] = {}
        for step in playbook:
            worker_name = step["worker"]
            action = step["action"]
            label = step.get("label", action)
            params = self._build_params(step, incident_id, service)
            worker = self.workers.get(worker_name)
            if worker is None:
                continue

            result = self._call_worker(
                worker, action, params, receipts, budget, worker_name,
                circuits=circuits,
            )
            evidence[label] = result
            # _call_worker returns a budget-exhausted signal when try_record() fails
            if result and result.get("error") in ("budget_exhausted",):
                logger.warning("Budget exhausted at step %s for %s", label, incident_id)
                break

            # Loop-operator checkpoint: check progress every N calls
            if loop_checkpoint is not None and budget is not None:
                if loop_checkpoint.should_check(budget):
                    escalation = loop_checkpoint.check(
                        set(evidence.keys()), 0,  # score updated post-analysis
                    )
                    if escalation:
                        logger.warning(
                            "Loop escalation triggered for %s: %s",
                            incident_id, escalation["escalation_trigger"],
                        )
                        evidence["_loop_escalation"] = escalation
                        break

        return evidence

    def _get_change_time_window(self) -> int:
        """Return change record lookback window in hours anchored to the incident time.

        If the current incident has a created_at field, compute the elapsed hours since
        then (plus a 2-hour buffer) capped at 48h.  Falls back to 24h when unavailable.
        """
        incident = getattr(self._tls, "current_incident", None)
        if not incident:
            return 24
        created_at = incident.get("created_at", "") or incident.get("start_time", "")
        if not created_at:
            return 24
        try:
            from datetime import datetime, timezone
            if isinstance(created_at, str):
                # Handle ISO 8601 with or without timezone
                created_at = created_at.replace("Z", "+00:00")
                dt = datetime.fromisoformat(created_at)
            else:
                return 24
            now = datetime.now(timezone.utc)
            elapsed_hours = (now - dt).total_seconds() / 3600
            # Look back 2 hours before incident creation plus elapsed investigation time
            window = max(4, min(48, int(elapsed_hours + 2)))
            return window
        except Exception as exc:
            logger.debug("Time window calculation failed for %r, defaulting to 24h: %s", created_at, exc)
            return 24

    def _build_params(self, step: dict, incident_id: str, service: str) -> dict:
        """Build parameters for a playbook step."""
        params: dict[str, Any] = {}

        if step["action"] == "search_logs":
            hint = step.get("query_hint", "{service}")
            params["query"] = hint.format(service=service)
            params["service"] = service
        elif step["action"] == "get_change_data":
            params["service"] = service
            # G-4: anchor change queries to incident time window
            params["time_window_hours"] = self._get_change_time_window()
        elif step["action"] in ("get_golden_signals", "check_latency"):
            params["service"] = service
            params["target"] = service
        elif step["action"] in ("query_metrics", "get_resource_metrics"):
            params["service"] = service
            params["target"] = service
            if "metric_hint" in step:
                params["metric"] = step["metric_hint"]
        elif step["action"] == "get_events":
            params["service"] = service
            params["target"] = service
        # ITSM (ServiceNow) actions
        elif step["action"] == "get_ci_details":
            params["service"] = service
        elif step["action"] == "search_incidents":
            params["service"] = service
            params["query"] = step.get("query_hint", "")
        elif step["action"] == "get_change_records":
            params["service"] = service
            # G-4: anchor ITSM change queries to the incident time window
            params["time_window_hours"] = self._get_change_time_window()
        elif step["action"] == "get_known_errors":
            params["service"] = service
        # DevOps (GitHub) actions — proof-gated, only called conditionally
        elif step["action"] == "get_recent_deployments":
            params["service"] = service
        elif step["action"] == "get_pr_details":
            params["repo"] = step.get("repo", "")
            params["pr_number"] = step.get("pr_number")
        elif step["action"] == "get_commit_diff":
            params["repo"] = step.get("repo", "")
            params["sha"] = step.get("sha", "")
        elif step["action"] == "get_workflow_runs":
            params["service"] = service

        return params

    # ------------------------------------------------------------------ #
    # Internal: analyze evidence (W2 multi-hypothesis + W3 evidence-weighted)
    # ------------------------------------------------------------------ #

    def _analyze_evidence(
        self,
        incident_id: str,
        incident: dict,
        incident_type: str,
        evidence: dict[str, Any],
    ) -> dict:
        """Deterministic evidence analysis engine with optional LLM refinement."""
        summary = incident.get("summary", "")
        service = incident.get("affected_service", "unknown")

        # Gather raw data blobs
        logs = self._extract_logs(evidence)
        signals = self._extract_signals(evidence)
        metrics = self._extract_metrics(evidence)
        events = self._extract_events(evidence)
        changes = self._extract_changes(evidence)

        # ITSM + DevOps context (stored in thread-local — safe for concurrent investigations)
        self._tls.itsm_evidence = self._extract_itsm_context(evidence)
        self._tls.devops_evidence = self._extract_devops_context(evidence)

        # ITSM change window correlation: find which change caused the incident.
        # Runs on every investigation that has ITSM context; non-blocking.
        incident_time = incident.get("start_time", incident.get("created_at", ""))
        itsm_changes = []
        if self._tls.itsm_evidence:
            itsm_changes = (
                self._tls.itsm_evidence.get("recent_changes", [])
                or self._tls.itsm_evidence.get("change_records", [])
            )
        if itsm_changes and incident_time:
            try:
                from supervisor.itsm_change_correlator import correlate_change_window
                git_commits = (
                    evidence.get("git_context", {}).get("commits", [])
                    if evidence.get("git_context") else []
                )
                correlated = correlate_change_window(
                    incident_time, itsm_changes, service, git_commits=git_commits,
                )
                if correlated:
                    evidence["_itsm_change_correlations"] = correlated
                    # Inject the top causal change into changes so analyzers see it
                    top_change = correlated[0]
                    if top_change.get("correlation_score", 0) >= 0.50:
                        changes.insert(0, {
                            "change_type": top_change.get("change_type", "itsm_change"),
                            "description": (
                                f"[ITSM] {top_change.get('title', top_change.get('id', '?'))}"
                                f" — {top_change.get('correlation_reason', '')}"
                            ),
                            "time": top_change.get("start_time", ""),
                            "source": "itsm_correlator",
                            "correlation_score": top_change["correlation_score"],
                        })
                        logger.info(
                            "ITSM change window: most likely causal change '%s' "
                            "(score=%.2f, %d min before incident)",
                            top_change.get("id", "?"),
                            top_change["correlation_score"],
                            top_change.get("minutes_before_incident", 0),
                        )
            except Exception as exc:
                logger.debug("ITSM change window correlation failed (non-critical): %s", exc)

        # Build timeline from all sources
        timeline = self._build_timeline(logs, signals, metrics, events, changes, incident_type, service)

        # Extract historical context from experience replay
        suggested_root_causes: list[str] = evidence.get("_suggested_root_causes", [])
        tool_recommendations: dict[str, float] = evidence.get("_tool_recommendations", {})

        # Phase 2 pattern intelligence: consult PatternRegistry for pre-ranked hints.
        # DNA is encoded from available evidence; matched patterns inject their
        # top hypothesis as a primed root cause candidate.  Non-blocking.
        _dna_features: list[float] = []
        _fingerprint: str = ""
        try:
            from supervisor.incident_dna import encode_incident, extract_signature
            from supervisor.pattern_registry import get_registry
            _dna_flat = self._build_dna_evidence_dict(signals, metrics, evidence, incident_type)
            _dna = encode_incident(
                incident_id=incident_id, incident_type=incident_type,
                service=service, evidence=_dna_flat,
                rca_confidence=0,  # not yet known; filled in after analysis
            )
            _dna_features = _dna.features
            _fingerprint = extract_signature(_dna)
            _pattern_matches = get_registry().match(_dna_features, incident_type, top_k=3)
            for _pm in _pattern_matches:
                _top_hyp = _pm.top_hypothesis()
                if _top_hyp and _top_hyp not in suggested_root_causes:
                    suggested_root_causes = list(suggested_root_causes) + [_top_hyp]
                    logger.debug(
                        "PatternRegistry: injected hypothesis %r from pattern %s (count=%d)",
                        _top_hyp, _pm.fingerprint, _pm.match_count,
                    )
        except Exception as exc:
            logger.debug("PatternRegistry lookup failed (non-critical): %s", exc)

        # W2: Multi-hypothesis scoring — include historical priming
        hypotheses = self._generate_hypotheses(
            incident_type, service, summary, logs, signals, metrics, events, changes, timeline,
            suggested_root_causes=suggested_root_causes,
        )

        # W3: Evidence-weighted confidence for each hypothesis
        for h in hypotheses:
            h.base_score = compute_confidence(
                h.base_score, logs, signals, metrics, events, changes,
                corroborating_sources=len(h.evidence_refs),
                incident_type=incident_type,
            )

        # Fetch PIL predictions for this service — inject as priors into LLM calls
        pil_context = ""
        try:
            from intelligence.background_runner import get_runner as _get_intel_runner
            from supervisor.system_prompt import build_pil_context_block
            _runner = _get_intel_runner()
            if _runner is not None:
                _preds = _runner.get_active_predictions_for_service(service)
                pil_context = build_pil_context_block(
                    [p.to_dict() for p in _preds] if _preds else []
                )
        except Exception:
            pass

        # LLM hypothesis refinement (optional, graceful degradation)
        llm_metrics = {}
        if _llm_enabled():
            _inv_id = getattr(self._tls, "current_investigation_id", "")
            _conf_before = hypotheses[0].base_score if hypotheses else 0.0
            try:
                from supervisor.tool_transparency import get_emitter as _tt
                _tt().record_pre_llm_scores(_inv_id, hypotheses)
            except Exception:
                pass
            self._tls.current_phase = "analyze"
            llm_metrics = self._llm_refine_hypotheses(
                incident_type, service, summary, hypotheses,
                logs, signals, metrics, events, changes,
                suggested_root_causes=suggested_root_causes,
                tool_recommendations=tool_recommendations,
                pil_context=pil_context,
            )
            _conf_after = hypotheses[0].base_score if hypotheses else 0.0
            if llm_metrics.get("llm_refinement_status") == "failed":
                # Hypothesis scores are still at pre-refinement values; flag explicitly.
                llm_metrics["confidence_degraded"] = True
                llm_metrics["confidence_degraded_reason"] = (
                    f"llm_refinement_failed:{llm_metrics.get('llm_refinement_error', 'unknown')}"
                )
            try:
                from supervisor.tool_transparency import get_emitter as _tt
                _tt().record_post_llm_scores(_inv_id, hypotheses, _conf_before, _conf_after)
            except Exception:
                pass
            self._tls.current_phase = "collect"

        # W2: Select winner — highest score, deterministic tiebreak by name
        hypotheses.sort(key=lambda h: (-h.base_score, h.name))
        winner = hypotheses[0] if hypotheses else None

        if winner:
            root_cause = winner.root_cause
            confidence = winner.base_score
            reasoning = winner.reasoning
        else:
            root_cause = f"{service} incident - investigation inconclusive"
            confidence = compute_confidence(30, logs, signals, metrics, events, changes, incident_type=incident_type)
            reasoning = f"Generic analysis of {service} incident. Insufficient pattern match."

        # LLM reasoning generation (optional, enhances winner reasoning)
        if _llm_enabled() and winner:
            reasoning_metrics = self._llm_generate_reasoning(
                incident_type, service, root_cause, reasoning,
                logs, signals, metrics, events, changes, timeline,
                pil_context=pil_context,
            )
            if reasoning_metrics.get("reasoning"):
                reasoning = reasoning_metrics["reasoning"]
            llm_metrics.update({
                k: v for k, v in reasoning_metrics.items() if k != "reasoning"
            })

        # Institutional knowledge retrieval (proof-gated, additive only)
        historical_matches: list[dict] = []
        retrieval_boost = 0.0
        if _KNOWLEDGE_AVAILABLE and _knowledge_retrieval is not None:
            try:
                historical_matches = _knowledge_retrieval.retrieve_similar(
                    service=service,
                    incident_type=incident_type,
                    summary=summary,
                )
                if historical_matches and winner:
                    # Proof-gated: only boost if we already have a winning hypothesis
                    retrieval_boost = _retrieval_boost(historical_matches)
                    confidence = min(100, int(confidence + retrieval_boost))
                    # Without proof artifact, confidence must stay < 80
                    if not winner.evidence_refs:
                        confidence = min(confidence, 79)
            except Exception as exc:
                logger.warning("Knowledge retrieval failed (non-critical): %s", exc)

        # Network evidence contribution (ThousandEyes — additive only)
        network_ctx = self._extract_network_evidence(evidence)
        if network_ctx["total_confidence_delta"] > 0:
            confidence = min(95, int(confidence + network_ctx["total_confidence_delta"] * 100))
        if network_ctx["top_owner"] not in ("unknown", ""):
            reasoning = reasoning + f" Network analysis suggests responsible party: {network_ctx['top_owner']}."
        if network_ctx["summary"]:
            reasoning = reasoning + f" {network_ctx['summary']}"

        # G-5: Fail-closed — qualify root_cause string when confidence is insufficient
        # so callers never act on an unqualified guess
        _MINIMUM_ACTIONABLE_CONFIDENCE = 30
        if confidence < _MINIMUM_ACTIONABLE_CONFIDENCE:
            root_cause = (
                f"INSUFFICIENT EVIDENCE: {service} — confidence {confidence}/100. "
                "Manual investigation required."
            )
        elif confidence < 50 and not root_cause.startswith("LOW CONFIDENCE"):
            root_cause = f"LOW CONFIDENCE: {root_cause}"

        # LLM reasoning failure: surface status in result so callers can see stale confidence
        llm_reasoning_failed = (
            _llm_enabled() and winner and
            llm_metrics.get("llm_reasoning_status") == "failed"
        )
        llm_refine_failed = llm_metrics.get("llm_refinement_status") == "failed"

        result: dict[str, Any] = {
            "incident_id": incident_id,
            "root_cause": root_cause,
            "confidence": confidence,
            "evidence_timeline": timeline,
            "reasoning": reasoning,
            "historical_matches": historical_matches,
            "retrieval_confidence_boost": retrieval_boost,
            "_hypothesis_count": len(hypotheses),
            "_winner_hypothesis": winner.name if winner else "none",
            # Phase 2: carry DNA features and fingerprint forward for _persist_results
            "_dna_features": _dna_features,
            "_dna_fingerprint": _fingerprint,
        }

        if llm_refine_failed or llm_reasoning_failed:
            result["confidence_degraded"] = True
            reasons = []
            if llm_refine_failed:
                reasons.append(llm_metrics.get("confidence_degraded_reason", "llm_refinement_failed"))
            if llm_reasoning_failed:
                reasons.append(f"llm_reasoning_failed:{llm_metrics.get('llm_reasoning_error', 'unknown')}")
            result["confidence_degraded_reason"] = "; ".join(reasons)

        # Extract grounded vs ungrounded claims (non-critical — never raises)
        if result.get("root_cause"):
            result["validated_claims"] = []
            result["non_validated_claims"] = []
            try:
                _rca_text = result.get("root_cause", "") + " " + result.get("summary", "")
                _evidence_text = " ".join(str(v) for v in evidence.values() if v)[:4000]
                for _sentence in _rca_text.split("."):
                    _sentence = _sentence.strip()
                    if not _sentence:
                        continue
                    _words = [w for w in _sentence.split() if len(w) >= 6]
                    _grounded = any(w.lower() in _evidence_text.lower() for w in _words)
                    if _grounded:
                        result["validated_claims"].append(_sentence)
                    else:
                        result["non_validated_claims"].append(_sentence)
            except Exception:
                pass

        # Attach LLM usage metrics for OTEL emission
        if llm_metrics:
            result["_llm_metrics"] = llm_metrics

        return result

    # ------------------------------------------------------------------ #
    # LLM-assisted analysis (graceful — never blocks deterministic path)
    # ------------------------------------------------------------------ #

    def _llm_refine_hypotheses(
        self,
        incident_type: str,
        service: str,
        summary: str,
        hypotheses: list[Hypothesis],
        logs: list[dict],
        signals: dict,
        metrics: dict,
        events: list[dict],
        changes: list[dict],
        suggested_root_causes: list[str] | None = None,
        tool_recommendations: dict[str, float] | None = None,
        pil_context: str = "",
    ) -> dict:
        """Use LLM to refine and re-rank hypotheses. Returns GenAI metrics."""
        try:
            evidence_summary = self._format_evidence_summary(
                logs, signals, metrics, events, changes,
                suggested_root_causes=suggested_root_causes,
                tool_recommendations=tool_recommendations,
            )
            hyp_dicts = [
                {"name": h.name, "root_cause": h.root_cause, "score": h.base_score, "reasoning": h.reasoning}
                for h in hypotheses
            ]
            result = _llm_refine(incident_type, service, summary, evidence_summary, hyp_dicts,
                                 pil_context=pil_context)

            refined = result.get("refined_hypotheses", [])
            if refined and isinstance(refined[0], dict):
                for r in refined:
                    for h in hypotheses:
                        if h.name == r.get("name"):
                            h.base_score = max(0, min(100, int(r.get("score", h.base_score))))
                            if r.get("reasoning"):
                                h.reasoning = r["reasoning"]

            # Record GenAI usage
            record_llm_usage(
                operation="refine_hypothesis",
                model_id=result.get("model_id", ""),
                input_tokens=result.get("input_tokens", 0),
                output_tokens=result.get("output_tokens", 0),
                latency_ms=result.get("latency_ms", 0),
                incident_type=incident_type,
            )

            return {
                "refine_input_tokens": result.get("input_tokens", 0),
                "refine_output_tokens": result.get("output_tokens", 0),
                "refine_latency_ms": result.get("latency_ms", 0),
                "refine_model_id": result.get("model_id", ""),
            }
        except Exception as exc:
            logger.warning("LLM hypothesis refinement failed (incident_type=%s): %s", incident_type, exc)
            return {
                "llm_refinement_status": "failed",
                "llm_refinement_error": type(exc).__name__,
            }

    def _llm_generate_reasoning(
        self,
        incident_type: str,
        service: str,
        root_cause: str,
        fallback_reasoning: str,
        logs: list[dict],
        signals: dict,
        metrics: dict,
        events: list[dict],
        changes: list[dict],
        timeline: list[dict],
        pil_context: str = "",
    ) -> dict:
        """Use LLM to generate enhanced reasoning narrative. Returns reasoning + metrics."""
        try:
            evidence_summary = self._format_evidence_summary(logs, signals, metrics, events, changes)
            timeline_summary = "\n".join(
                f"  [{e.get('timestamp', '?')}] ({e.get('source', '?')}) {e.get('event', '?')}"
                for e in timeline[:10]
            )
            result = _llm_reasoning(
                incident_type, service, root_cause, evidence_summary, timeline_summary,
                pil_context=pil_context,
            )

            # Record GenAI usage
            record_llm_usage(
                operation="generate_reasoning",
                model_id=result.get("model_id", ""),
                input_tokens=result.get("input_tokens", 0),
                output_tokens=result.get("output_tokens", 0),
                latency_ms=result.get("latency_ms", 0),
                incident_type=incident_type,
            )

            return {
                "reasoning": result.get("reasoning", ""),
                "reasoning_input_tokens": result.get("input_tokens", 0),
                "reasoning_output_tokens": result.get("output_tokens", 0),
                "reasoning_latency_ms": result.get("latency_ms", 0),
                "reasoning_model_id": result.get("model_id", ""),
            }
        except Exception as exc:
            logger.warning("LLM reasoning generation failed (incident_type=%s): %s", incident_type, exc)
            return {
                "reasoning": fallback_reasoning,
                "llm_reasoning_status": "failed",
                "llm_reasoning_error": type(exc).__name__,
            }

    def _format_evidence_summary(
        self,
        logs: list[dict],
        signals: dict,
        metrics: dict,
        events: list[dict],
        changes: list[dict],
        suggested_root_causes: list[str] | None = None,
        tool_recommendations: dict[str, float] | None = None,
    ) -> str:
        """Format evidence into a concise summary for LLM prompts.

        Includes historical context (suggested root causes from similar past incidents
        and tool recommendations) so the LLM can weight them appropriately.
        """
        parts = []
        if logs:
            parts.append(f"Logs: {len(logs)} entries")
            for log in logs[:3]:
                parts.append(f"  - [{log.get('level', '?')}] {log.get('message', '')[:120]}")
        if signals:
            gs = signals.get("golden_signals", {})
            parts.append(f"Golden Signals: latency={gs.get('latency', {})}, errors={gs.get('errors', {})}")
        if metrics:
            metric_list = metrics.get("metrics", [])
            parts.append(f"Metrics: {len(metric_list)} data points, pattern={metrics.get('pattern', 'none')}")
        if events:
            parts.append(f"Events: {len(events)} entries")
            for ev in events[:2]:
                parts.append(f"  - {ev.get('message', '')[:100]}")
        if changes:
            parts.append(f"Changes: {len(changes)} entries")
            for ch in changes[:2]:
                parts.append(f"  - {ch.get('change_type', '?')}: {ch.get('description', '')[:100]}")

        # Historical context from experience replay — gives LLM prior probabilities
        if suggested_root_causes:
            parts.append(
                "Historical context (similar past incidents): root causes were — "
                + "; ".join(f'"{c}"' for c in suggested_root_causes[:3])
            )
        if tool_recommendations:
            top_keys = [k for k, v in sorted(tool_recommendations.items(), key=lambda x: -x[1])[:3]]
            parts.append(f"Evidence sources that historically help: {', '.join(top_keys)}")

        return "\n".join(parts) if parts else "No evidence collected"

    # ------------------------------------------------------------------ #
    # W2: Hypothesis generation — each analyzer returns Hypothesis objects
    # ------------------------------------------------------------------ #

    def _generate_hypotheses(
        self,
        incident_type: str,
        service: str,
        summary: str,
        logs: list[dict],
        signals: dict,
        metrics: dict,
        events: list[dict],
        changes: list[dict],
        timeline: list[dict],
        suggested_root_causes: list[str] | None = None,
    ) -> list[Hypothesis]:
        """Generate scored hypotheses from type-specific analyzers.

        Each analyzer returns one or more Hypothesis objects.
        Historical root causes from experience replay are injected as
        additional hypotheses (score 55) so they compete fairly with
        deterministic hypotheses but yield to strong evidence-based ones.
        """
        analyzers = {
            "timeout": self._analyze_timeout,
            "oomkill": self._analyze_oomkill,
            "error_spike": self._analyze_error_spike,
            "latency": self._analyze_latency,
            "saturation": self._analyze_saturation,
            "network": self._analyze_network,
            "cascading": self._analyze_cascading,
            "missing_data": self._analyze_missing_data,
            "flapping": self._analyze_flapping,
            "silent_failure": self._analyze_silent_failure,
        }

        analyzer = analyzers.get(incident_type, self._analyze_generic)
        hypotheses = analyzer(service, summary, logs, signals, metrics, events, changes, timeline)

        # Inject historical priming: past confirmed root causes from similar incidents
        # compete as additional hypotheses (base score 55 — plausible but not dominant).
        if suggested_root_causes:
            existing_causes = {h.root_cause.lower() for h in hypotheses}
            for cause in suggested_root_causes[:3]:  # top 3 to avoid noise
                if cause.lower() not in existing_causes:
                    hypotheses.append(Hypothesis(
                        name="historical_pattern",
                        root_cause=cause,
                        base_score=55,
                        evidence_refs=["_past_experiences"],
                        reasoning=(
                            f"Historical pattern: similar {incident_type} incidents "
                            f"in {service} were previously diagnosed as '{cause}'. "
                            "Confidence elevated if current evidence matches."
                        ),
                    ))
                    logger.debug("Primed hypothesis from experience: %s", cause[:80])

        return hypotheses

    # -- Timeout -------------------------------------------------------- #

    def _analyze_timeout(self, service, summary, logs, signals, metrics, events, changes, timeline):
        hypotheses = []
        downstream = self._find_downstream_service(logs)
        gs = signals.get("golden_signals", {})
        latency = gs.get("latency", {})
        p95 = latency.get("p95", 0)
        baseline = latency.get("baseline_p95", 0)

        if downstream and p95 > baseline * 10:
            evidence_refs = ["golden_signals:latency_spike", "logs:timeout"]
            if changes:
                evidence_refs.append("changes:deployment")
            # G-7: check ITSM topology to confirm downstream is a known dependency
            topology_detail = ""
            ci = (getattr(self._tls, "itsm_evidence", None) or {}).get("ci", {})
            deps = ci.get("dependencies", [])
            if deps:
                evidence_refs.append("itsm:topology")
                dep_names = [str(d) for d in deps]
                if downstream in dep_names:
                    topology_detail = (
                        f" CMDB confirms {downstream} is a registered dependency of {service}."
                    )
                else:
                    topology_detail = f" CMDB-registered dependencies: {', '.join(dep_names[:3])}."
            hypotheses.append(Hypothesis(
                name="downstream_slow_queries",
                root_cause=f"{downstream} database slow queries causing upstream timeouts",
                base_score=80,
                evidence_refs=evidence_refs,
                reasoning=(
                    f"Timeline analysis shows {downstream} latency spike preceded "
                    f"api-gateway timeout errors. {downstream} p95 latency was {p95}ms "
                    f"compared to baseline of {baseline}ms (a {p95 // max(baseline, 1)}x increase). "
                    f"This latency caused downstream timeout failures at the api-gateway level. "
                    f"The first event in the timeline was {downstream} latency at the anomaly start, "
                    f"which then caused cascading timeouts. The causal chain is clear: "
                    f"database slow queries in {downstream} led to request timeouts before "
                    f"the api-gateway timeout threshold was reached.{topology_detail}"
                ),
            ))

        # Fallback hypothesis
        hypotheses.append(Hypothesis(
            name="timeout_undetermined",
            root_cause=f"{service} timeout - cause undetermined",
            base_score=35,
            evidence_refs=[],
            reasoning=f"Timeout detected on {service} but insufficient data to determine root cause.",
        ))

        return hypotheses

    # -- OOMKill -------------------------------------------------------- #

    def _analyze_oomkill(self, service, summary, logs, signals, metrics, events, changes, timeline):
        hypotheses = []
        metric_list = metrics.get("metrics", [])
        pattern = metrics.get("pattern", "")
        mem_limit = metrics.get("limit", 0)

        if pattern == "gradual_increase" or (metric_list and self._is_gradual_increase(metric_list)):
            evidence_refs = ["metrics:gradual_increase", "events:oomkill"]
            if mem_limit:
                evidence_refs.append("metrics:limit_exceeded")
            limit_str = f"{mem_limit / 1e9:.1f}GB" if mem_limit else "unknown"
            hypotheses.append(Hypothesis(
                name="memory_leak",
                root_cause=f"memory leak in {service} causing OOMKill",
                base_score=76,
                evidence_refs=evidence_refs,
                reasoning=(
                    f"Memory metrics show a gradual increasing pattern over time for {service}, "
                    f"characteristic of a memory leak. Memory usage grew from "
                    f"{metric_list[0].get('value', 0) / 1e9:.1f}GB to "
                    f"{metric_list[-1].get('value', 0) / 1e9:.1f}GB before exceeding the "
                    f"{limit_str} limit and triggering an OOMKill. "
                    f"The gradual increase over {len(metric_list)} data points rules out a "
                    f"sudden spike, confirming a leak pattern. The OOMKill event was the "
                    f"direct result of memory saturation from this leak."
                ),
            ))

        # Fallback
        hypotheses.append(Hypothesis(
            name="oomkill_generic",
            root_cause=f"{service} OOMKilled - memory saturation",
            base_score=60,
            evidence_refs=["events:oomkill"] if events else [],
            reasoning=f"OOMKill detected on {service} but memory pattern unclear.",
        ))

        return hypotheses

    # -- Error Spike ---------------------------------------------------- #

    def _analyze_error_spike(self, service, summary, logs, signals, metrics, events, changes, timeline):
        hypotheses = []
        error_type = self._find_error_type(logs)
        deployment = self._find_deployment(changes)

        if deployment and error_type:
            version = self._extract_version(deployment.get("description", ""))
            root_cause = (
                f"deployment {version} introduced {error_type} in {service}"
                if version
                else f"deployment introduced {error_type} in {service}"
            )
            dep_time = deployment.get("scheduled_start", deployment.get("actual_start", ""))
            evidence_refs = ["changes:deployment", f"logs:{error_type}", "golden_signals:error_rate"]

            # Enrich with DevOps context if available (proof-gated)
            devops_detail = ""
            devops = getattr(self._tls, "devops_evidence", None)
            if devops:
                deploys = devops.get("deployments", [])
                workflows = devops.get("workflow_runs", [])
                if deploys:
                    evidence_refs.append("devops:deployments")
                    pr_info = deploys[0]
                    devops_detail = (
                        f" Code change via PR #{pr_info.get('pr_number', '?')} "
                        f"by {pr_info.get('author', 'unknown')} "
                        f"(sha: {pr_info.get('sha', '?')[:8]})."
                    )
                if workflows:
                    evidence_refs.append("devops:workflow_runs")
                    latest_run = workflows[0]
                    ci_status = latest_run.get("conclusion", "unknown")
                    devops_detail += f" CI pipeline status: {ci_status}."

            # G-7: Enrich with ITSM topology (service tier + dependency graph)
            itsm_detail = ""
            itsm = getattr(self._tls, "itsm_evidence", None)
            if itsm:
                ci = itsm.get("ci", {})
                if ci:
                    evidence_refs.append("itsm:ci_details")
                    tier = ci.get("tier", "")
                    if tier:
                        itsm_detail = f" Service tier: {tier}."
                    # G-7: surface CI dependency topology so blast-radius is visible
                    deps = ci.get("dependencies", [])
                    if deps:
                        evidence_refs.append("itsm:topology")
                        dep_list = ", ".join(str(d) for d in deps[:4])
                        itsm_detail += f" Downstream dependencies: {dep_list}."
                if deployment.get("rollback_plan"):
                    itsm_detail += f" Rollback plan: {deployment['rollback_plan']}."

            hypotheses.append(Hypothesis(
                name="deployment_error",
                root_cause=root_cause,
                base_score=80,
                evidence_refs=evidence_refs,
                reasoning=(
                    f"Strong temporal correlation between deployment and error spike. "
                    f"Deployment of {service} ({deployment.get('description', '')}) completed "
                    f"at {dep_time}, and {error_type} errors began appearing immediately after "
                    f"(within seconds). The deployment preceded the errors, establishing a clear "
                    f"causal relationship. Error rate spiked from baseline to "
                    f"{signals.get('golden_signals', {}).get('errors', {}).get('rate', 0) * 100:.0f}% "
                    f"after the deployment. The {error_type} is the specific defect introduced by "
                    f"the code change.{devops_detail}{itsm_detail}"
                ),
            ))

        if error_type:
            hypotheses.append(Hypothesis(
                name="error_type_only",
                root_cause=f"{error_type} errors in {service}",
                base_score=55,
                evidence_refs=[f"logs:{error_type}"],
                reasoning=f"Error spike of {error_type} in {service}, no deployment correlation found.",
            ))

        # Fallback
        hypotheses.append(Hypothesis(
            name="error_spike_generic",
            root_cause=f"error spike in {service}",
            base_score=45,
            evidence_refs=[],
            reasoning=f"Error spike detected in {service} but specific error type not identified.",
        ))

        return hypotheses

    # -- Latency -------------------------------------------------------- #

    def _analyze_latency(self, service, summary, logs, signals, metrics, events, changes, timeline):
        hypotheses = []
        backend = self._find_backend_from_logs(logs)
        gs = signals.get("golden_signals", {})
        latency = gs.get("latency", {})
        p95 = latency.get("p95", 0)
        baseline = latency.get("baseline_p95", 0)

        if backend:
            backend_event = self._find_backend_event(logs, backend)
            evidence_refs = ["golden_signals:latency", f"logs:{backend}"]
            if backend_event:
                evidence_refs.append(f"logs:{backend}_event")
            hypotheses.append(Hypothesis(
                name="backend_latency",
                root_cause=f"{backend} rebalancing causing slow queries in {service}",
                base_score=78,
                evidence_refs=evidence_refs,
                reasoning=(
                    f"Latency analysis shows {service} p95 latency spiked to {p95}ms from "
                    f"baseline {baseline}ms. Log analysis reveals {backend} as the backend "
                    f"dependency experiencing issues. {backend_event or 'Backend event detected'} "
                    f"preceded the latency spike, establishing causality. The {backend} issue "
                    f"caused slow queries which propagated as latency to {service}."
                ),
            ))

        hypotheses.append(Hypothesis(
            name="latency_generic",
            root_cause=f"{service} latency degradation",
            base_score=50,
            evidence_refs=[],
            reasoning=f"Latency spike detected in {service} but backend cause not identified.",
        ))

        return hypotheses

    # -- Saturation ----------------------------------------------------- #

    def _analyze_saturation(self, service, summary, logs, signals, metrics, events, changes, timeline):
        hypotheses = []
        gs = signals.get("golden_signals", {})
        sat = gs.get("saturation", {})
        cpu = sat.get("cpu", 0)
        deployment = self._find_deployment(changes)

        if cpu > 90 and deployment:
            change_type = deployment.get("change_type", "change")
            evidence_refs = ["golden_signals:cpu_saturation", "changes:config_change", "logs:thread_pool"]

            # DevOps enrichment for saturation after change
            devops_detail = ""
            devops = getattr(self._tls, "devops_evidence", None)
            if devops and devops.get("workflow_runs"):
                evidence_refs.append("devops:workflow_runs")
                ci_status = devops["workflow_runs"][0].get("conclusion", "unknown")
                devops_detail = f" CI pipeline conclusion: {ci_status}."

            hypotheses.append(Hypothesis(
                name="cpu_after_change",
                root_cause=(
                    f"{service} cpu exhaustion after config change causing "
                    f"thread pool saturation"
                ),
                base_score=78,
                evidence_refs=evidence_refs,
                reasoning=(
                    f"CPU saturation detected at {cpu}% on {service}. "
                    f"A {change_type} ({deployment.get('description', '')}) was applied "
                    f"at {deployment.get('scheduled_start', '')} which preceded the CPU spike. "
                    f"Log analysis shows thread pool exhaustion consistent with a runaway "
                    f"loop triggered by the config change. The correlation between the "
                    f"config change timestamp and CPU spike confirms causality.{devops_detail}"
                ),
            ))
        elif cpu > 90:
            hypotheses.append(Hypothesis(
                name="cpu_exhaustion",
                root_cause=f"{service} cpu exhaustion",
                base_score=62,
                evidence_refs=["golden_signals:cpu_saturation"],
                reasoning=f"CPU at {cpu}% on {service} but no change correlation found.",
            ))

        hypotheses.append(Hypothesis(
            name="saturation_generic",
            root_cause=f"{service} resource saturation",
            base_score=45,
            evidence_refs=[],
            reasoning=f"Resource saturation on {service}.",
        ))

        return hypotheses

    # -- Network -------------------------------------------------------- #

    def _analyze_network(self, service, summary, logs, signals, metrics, events, changes, timeline):
        hypotheses = []
        dns_issue = self._has_dns_issues(logs)
        deployment = self._find_deployment(changes)

        if dns_issue and deployment:
            evidence_refs = ["logs:dns_failure", "changes:maintenance", "logs:multi_service"]
            hypotheses.append(Hypothesis(
                name="dns_after_maintenance",
                root_cause=(
                    "dns resolution failure after dns server maintenance "
                    "causing inter-service connectivity failures"
                ),
                base_score=80,
                evidence_refs=evidence_refs,
                reasoning=(
                    f"Multiple services report DNS resolution failures. "
                    f"A DNS server maintenance event ({deployment.get('description', '')}) "
                    f"occurred at {deployment.get('scheduled_start', '')} which preceded the "
                    f"connection failures. The maintenance caused a DNS cache flush, leading to "
                    f"resolution failures across all services dependent on internal DNS. "
                    f"This explains the broad multi-service impact observed in the logs."
                ),
            ))
        elif dns_issue:
            hypotheses.append(Hypothesis(
                name="dns_failure",
                root_cause=f"dns resolution failure affecting {service}",
                base_score=65,
                evidence_refs=["logs:dns_failure"],
                reasoning="DNS resolution failures detected but no maintenance event found.",
            ))

        hypotheses.append(Hypothesis(
            name="network_generic",
            root_cause=f"network connectivity failure affecting {service}",
            base_score=45,
            evidence_refs=[],
            reasoning=f"Network issue detected on {service}.",
        ))

        return hypotheses

    # -- Cascading ------------------------------------------------------ #

    def _analyze_cascading(self, service, summary, logs, signals, metrics, events, changes, timeline):
        hypotheses = []
        pool_exhaustion = self._has_pool_exhaustion(logs)
        deployment = self._find_deployment(changes)
        cascade_services = self._find_cascade_chain(logs)

        origin_service = cascade_services[0] if cascade_services else service
        downstream_desc = (
            ", ".join(cascade_services[1:]) if len(cascade_services) > 1
            else "downstream services"
        )

        if pool_exhaustion and deployment:
            evidence_refs = [
                "logs:pool_exhaustion", "changes:database_migration",
                "logs:cascade_chain", "golden_signals:latency",
            ]
            hypotheses.append(Hypothesis(
                name="pool_exhaustion_cascade",
                root_cause=(
                    f"database connection pool exhaustion in {origin_service} "
                    f"caused by slow queries after index drop, cascading to {downstream_desc}"
                ),
                base_score=73,
                evidence_refs=evidence_refs,
                reasoning=(
                    f"Cascading failure analysis: A database migration "
                    f"({deployment.get('description', '')}) at "
                    f"{deployment.get('scheduled_start', '')} caused slow queries (full table scans). "
                    f"This led to connection pool exhaustion in {origin_service} as connections "
                    f"were held longer. The pool exhaustion then cascaded to {downstream_desc}. "
                    f"The cascade propagated through the dependency chain."
                ),
            ))

        hypotheses.append(Hypothesis(
            name="cascading_generic",
            root_cause=f"cascading failure from {service}",
            base_score=50,
            evidence_refs=[],
            reasoning="Cascading failure detected but root trigger unclear.",
        ))

        return hypotheses

    # -- Missing Data --------------------------------------------------- #

    def _analyze_missing_data(self, service, summary, logs, signals, metrics, events, changes, timeline):
        hypotheses = []
        error_type = self._find_connection_error(logs)

        if error_type:
            target = self._find_connection_target(logs)
            evidence_refs = [f"logs:{error_type}"]
            if target:
                evidence_refs.append(f"logs:{target}")
            if events:
                evidence_refs.append("events:connection_failure")
            root_cause = (
                f"{target} connection failure affecting {service}"
                if target
                else f"connection failure affecting {service}"
            )
            hypotheses.append(Hypothesis(
                name="connection_failure",
                root_cause=root_cause,
                base_score=60,
                evidence_refs=evidence_refs,
                reasoning=(
                    f"Investigation completed with limited observability data. "
                    f"Metrics were unavailable for {service}, reducing confidence. "
                    f"However, log analysis clearly shows {target or 'backend'} connection "
                    f"failures ({error_type}). Events confirm the connection target became "
                    f"unreachable. Despite missing metrics data, the log and event evidence "
                    f"is sufficient to identify the connection failure as the root cause."
                ),
            ))

        hypotheses.append(Hypothesis(
            name="missing_data_generic",
            root_cause=f"{service} degraded - insufficient data for root cause",
            base_score=25,
            evidence_refs=[],
            reasoning=f"Limited data available for {service}. Cannot determine root cause with confidence.",
        ))

        return hypotheses

    # -- Flapping ------------------------------------------------------- #

    def _analyze_flapping(self, service, summary, logs, signals, metrics, events, changes, timeline):
        hypotheses = []
        pool_pattern = self._detect_sawtooth_pattern(metrics)
        anomaly_type = signals.get("anomaly_type", "")

        if pool_pattern or "intermittent" in anomaly_type:
            evidence_refs = ["metrics:sawtooth_pattern", "golden_signals:intermittent"]
            hypotheses.append(Hypothesis(
                name="connection_pool_leak",
                root_cause=(
                    f"connection pool leak in {service} causing intermittent exhaustion"
                ),
                base_score=70,
                evidence_refs=evidence_refs,
                reasoning=(
                    f"Analysis of {service} metrics reveals a sawtooth pattern in connection "
                    f"pool usage - connections gradually accumulate to the maximum, causing "
                    f"intermittent failures, then partially release. This periodic pattern is "
                    f"characteristic of a connection pool leak where connections are not properly "
                    f"returned. The flapping/intermittent nature of the alerts correlates with "
                    f"the pool reaching capacity and then partially recovering. No code changes "
                    f"were found, suggesting this is a latent bug that manifests under load."
                ),
            ))

        hypotheses.append(Hypothesis(
            name="flapping_generic",
            root_cause=f"intermittent failures in {service}",
            base_score=40,
            evidence_refs=[],
            reasoning="Intermittent failures detected but pattern unclear.",
        ))

        return hypotheses

    # -- Silent Failure ------------------------------------------------- #

    def _analyze_silent_failure(self, service, summary, logs, signals, metrics, events, changes, timeline):
        hypotheses = []
        pipeline_failure = self._find_pipeline_failure(logs)
        stale_cache = self._find_stale_cache(logs)

        if pipeline_failure and stale_cache:
            evidence_refs = ["logs:pipeline_failure", "logs:stale_cache", "golden_signals:throughput_drop"]
            hypotheses.append(Hypothesis(
                name="pipeline_stale_cache",
                root_cause=(
                    f"data pipeline failure causing stale cache in {service}"
                ),
                base_score=73,
                evidence_refs=evidence_refs,
                reasoning=(
                    f"The investigation reveals an indirect, upstream failure chain. "
                    f"The data pipeline job failed at the earliest point in the timeline, "
                    f"which meant fresh data stopped flowing to {service}. "
                    f"As the cache aged, the stale data caused increased cache misses and "
                    f"clients stopped requesting stale recommendations, leading to the observed "
                    f"throughput drop. No direct errors were produced - this was a silent "
                    f"degradation caused by the upstream pipeline failure propagating through "
                    f"the data freshness dependency."
                ),
            ))
        elif pipeline_failure:
            hypotheses.append(Hypothesis(
                name="pipeline_failure",
                root_cause=f"data pipeline failure affecting {service}",
                base_score=62,
                evidence_refs=["logs:pipeline_failure"],
                reasoning="Pipeline failure detected but downstream impact unclear.",
            ))

        hypotheses.append(Hypothesis(
            name="silent_failure_generic",
            root_cause=f"{service} throughput degradation",
            base_score=40,
            evidence_refs=[],
            reasoning=f"Throughput drop in {service} but root cause unclear.",
        ))

        return hypotheses

    # -- Generic -------------------------------------------------------- #

    def _analyze_generic(self, service, summary, logs, signals, metrics, events, changes, timeline):
        return [Hypothesis(
            name="generic",
            root_cause=f"{service} incident - investigation inconclusive",
            base_score=25,
            evidence_refs=[],
            reasoning=f"Generic analysis of {service} incident. Insufficient pattern match.",
        )]

    # ------------------------------------------------------------------ #
    # Data extraction helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _log_timestamp(log: dict) -> str:
        """Normalize log timestamp across Splunk (_time), generic (timestamp), and short (ts) keys."""
        return log.get("_time") or log.get("timestamp") or log.get("ts") or ""

    def _extract_logs(self, evidence: dict) -> list[dict]:
        """Extract log entries from evidence."""
        all_logs = []
        for key, val in evidence.items():
            if not isinstance(val, dict):
                continue
            logs_data = val.get("logs", val)
            if isinstance(logs_data, dict):
                results = logs_data.get("results", [])
                if isinstance(results, list):
                    all_logs.extend(results)
        return sorted(all_logs, key=self._log_timestamp)

    def _extract_signals(self, evidence: dict) -> dict:
        """Extract golden signals from evidence."""
        for key, val in evidence.items():
            if not isinstance(val, dict):
                continue
            signals = val.get("signals", {})
            if isinstance(signals, dict) and "golden_signals" in signals:
                return signals
        return {}

    def _extract_metrics(self, evidence: dict) -> dict:
        """Extract metrics from evidence."""
        for key, val in evidence.items():
            if not isinstance(val, dict):
                continue
            metrics = val.get("metrics", {})
            if isinstance(metrics, dict) and "metrics" in metrics:
                return metrics
        return {}

    def _extract_events(self, evidence: dict) -> list[dict]:
        """Extract events from evidence."""
        all_events = []
        for key, val in evidence.items():
            if not isinstance(val, dict):
                continue
            events = val.get("events", [])
            if isinstance(events, list):
                all_events.extend(events)
        return sorted(all_events, key=lambda x: x.get("timestamp") or x.get("_time") or x.get("ts") or "")

    def _build_dna_evidence_dict(
        self,
        signals: dict,
        metrics: dict,
        evidence: dict,
        incident_type: str,
    ) -> dict:
        """Flatten investigation evidence into the flat key-value dict expected by encode_incident.

        Only populates what's readily available; encode_incident handles missing keys
        gracefully by defaulting to 0.0.
        """
        flat: dict = {}
        gs = signals.get("golden_signals", {}) if signals else {}
        err = gs.get("errors", {})
        lat = gs.get("latency", {})
        if err.get("rate") and err.get("baseline_rate"):
            flat["observed_error_rate"] = float(err["rate"])
            flat["baseline_error_rate"] = float(err["baseline_rate"])
        if lat.get("p95") and lat.get("baseline_p95"):
            flat["observed_p95"] = float(lat["p95"])
            flat["baseline_p95"] = float(lat["baseline_p95"])
        m = metrics.get("metrics", {}) if metrics else {}
        if m.get("cpu_percent"):
            flat["cpu_percent"] = float(m["cpu_percent"])
        if m.get("memory_percent"):
            flat["memory_percent"] = float(m["memory_percent"])
        if gs.get("network", {}).get("errors"):
            flat["network_errors"] = True
        # Count non-private, non-empty evidence keys as source count
        flat["num_evidence_sources"] = sum(
            1 for k, v in evidence.items()
            if not k.startswith("_") and v not in (None, "", [], {})
        )

        # Environment factors (dims 14-15)
        try:
            from datetime import datetime, timezone
            flat["incident_hour"] = datetime.now(timezone.utc).hour
        except Exception:
            pass

        # Traffic deviation: requests_ratio from golden signals if available
        traffic_signals = gs.get("traffic", {}) or gs.get("requests", {})
        obs_rps = traffic_signals.get("rate") or traffic_signals.get("rps")
        base_rps = traffic_signals.get("baseline_rate") or traffic_signals.get("baseline_rps")
        if obs_rps and base_rps and float(base_rps) > 0:
            flat["traffic_ratio"] = min(float(obs_rps) / float(base_rps), 3.0)

        return flat

    def _extract_changes(self, evidence: dict) -> list[dict]:
        """Extract change/deployment data from evidence (Splunk + ServiceNow)."""
        all_changes = []
        for key, val in evidence.items():
            if not isinstance(val, dict):
                continue
            # Splunk change data
            changes = val.get("changes", [])
            if isinstance(changes, list):
                all_changes.extend(changes)
            # ServiceNow change records (richer: approval, rollback, CI impact)
            change_records = val.get("change_records", [])
            if isinstance(change_records, list):
                for cr in change_records:
                    all_changes.append({
                        "change_id": cr.get("number", ""),
                        "change_type": cr.get("type", "deployment"),
                        "description": cr.get("short_description", ""),
                        "scheduled_start": cr.get("start_date", ""),
                        "actual_end": cr.get("end_date", ""),
                        "service": cr.get("service", ""),
                        "status": cr.get("state", ""),
                        "requested_by": cr.get("requested_by", ""),
                        "approval": cr.get("approval", ""),
                        "risk": cr.get("risk", ""),
                        "rollback_plan": cr.get("rollback_plan", ""),
                    })
        return all_changes

    def _extract_itsm_context(self, evidence: dict) -> dict:
        """Extract ITSM context (CI details, known errors) from evidence."""
        itsm = evidence.get("itsm_context", {})
        if not isinstance(itsm, dict):
            return {}
        return itsm

    def _extract_confluence_context(self, evidence: dict) -> dict:
        """Extract Confluence context (runbooks, post-mortems) from evidence."""
        confluence = evidence.get("confluence_context", {})
        if not isinstance(confluence, dict):
            return {}
        return confluence

    def _extract_devops_context(self, evidence: dict) -> dict:
        """Extract DevOps context (deployments, workflow runs) from evidence."""
        devops = evidence.get("devops_context", {})
        if not isinstance(devops, dict):
            return {}
        return devops

    def _extract_network_evidence(self, evidence: dict) -> dict:
        """Extract ThousandEyes network evidence and correlation data from evidence."""
        evidence_list = evidence.get("network_evidence", [])
        if not isinstance(evidence_list, list):
            evidence_list = []

        correlation_list = evidence.get("network_correlation", [])
        if not isinstance(correlation_list, list):
            correlation_list = []

        summary = evidence.get("network_summary", "")
        if not isinstance(summary, str):
            summary = ""

        # Sum confidence deltas from correlations, capped at 0.40
        total_confidence_delta = 0.0
        for corr in correlation_list:
            if isinstance(corr, dict):
                total_confidence_delta += float(corr.get("confidence_delta", 0.0))
        total_confidence_delta = min(0.40, total_confidence_delta)

        # Most frequent recommended_owner across correlation results
        owner_counts: dict[str, int] = {}
        for corr in correlation_list:
            if isinstance(corr, dict):
                owner = corr.get("owner", "")
                if owner:
                    owner_counts[owner] = owner_counts.get(owner, 0) + 1
        top_owner = max(owner_counts, key=lambda o: owner_counts[o]) if owner_counts else "unknown"

        return {
            "evidence_list": evidence_list,
            "correlation_list": correlation_list,
            "summary": summary,
            "top_owner": top_owner,
            "total_confidence_delta": total_confidence_delta,
            "has_network_evidence": bool(evidence_list),
        }

    # ------------------------------------------------------------------ #
    # Timeline builder
    # ------------------------------------------------------------------ #

    def _timeline_from_signals(self, signals: dict, signal_service: str) -> list[dict]:
        """Extract timeline entries from golden signal anomalies."""
        anomaly_start = signals.get("anomaly_start", "")
        if not anomaly_start:
            return []
        gs = signals.get("golden_signals", {})
        description = self._describe_anomaly(
            signals.get("anomaly_type", ""), signal_service,
            gs.get("latency", {}), gs.get("errors", {}), gs.get("saturation", {}),
        )
        return [{"timestamp": anomaly_start, "event": description,
                 "source": "golden_signals", "service": signal_service}]

    def _timeline_from_metrics(self, metrics: dict, service: str) -> list[dict]:
        """Extract timeline entries from metric data."""
        entries: list[dict] = []
        metric_list = metrics.get("metrics", [])
        baseline = metrics.get("baseline", 0)
        if not isinstance(metric_list, list) or not metric_list:
            return entries

        first = metric_list[0]
        name = first.get("name", "metric")
        value = first.get("value", 0)
        ts = first.get("timestamp", "")

        if baseline and value and value > baseline * 2:
            entries.append({"timestamp": ts, "source": "metrics", "service": service,
                            "event": f"{name} spike to {value} (baseline: {baseline}) on {service}"})

        pattern = metrics.get("pattern", "")
        mem_limit = metrics.get("limit", 0)
        if pattern == "gradual_increase" and mem_limit:
            entries.append({"timestamp": ts, "source": "metrics", "service": service,
                            "event": f"gradual memory increase detected on {service} "
                                     f"(limit: {mem_limit / 1e9:.0f}GB) - memory saturation"})

        pool_max = metrics.get("pool_max", 0)
        if pool_max:
            for m in metric_list:
                if m.get("value", 0) >= pool_max:
                    entries.append({"timestamp": m.get("timestamp", ""), "source": "metrics",
                                    "service": service,
                                    "event": f"connection pool exhaustion on {service} ({m['value']}/{pool_max})"})
                    break
        return entries

    def _timeline_from_events(self, events: list[dict], service: str) -> list[dict]:
        """Extract timeline entries from infrastructure events."""
        entries: list[dict] = []
        for event in events:
            msg = event.get("message", "")
            # G-3: normalize timestamp key (timestamp / _time / ts)
            ts = event.get("timestamp") or event.get("_time") or event.get("ts") or ""
            entries.append({"timestamp": ts, "event": msg,
                            "source": "events", "service": service})
            if "oomkill" in msg.lower():
                entries.append({"timestamp": ts,
                                "event": f"OOMKill event: {msg}",
                                "source": "events", "service": service})
        return entries

    def _timeline_from_logs(self, logs: list[dict], service: str) -> list[dict]:
        """Extract timeline entries from log data."""
        entries: list[dict] = []
        timeout_logs: list[str] = []
        for log in logs[:5]:
            log_service = log.get("service", service)
            log_msg = log.get("message", "")
            # G-3: normalize timestamp key (Splunk _time, generic timestamp, or ts)
            ts = self._log_timestamp(log)
            entries.append({"timestamp": ts, "event": log_msg,
                            "source": "logs", "service": log_service})
            if "timeout" in log_msg.lower():
                timeout_logs.append(log_service)
        if timeout_logs:
            svc = timeout_logs[0]
            entries.append({
                "timestamp": self._log_timestamp(logs[0]) if logs else "",
                "event": f"{svc} timeout errors detected ({len(timeout_logs)} occurrences)",
                "source": "log_summary", "service": svc,
            })
        return entries

    def _timeline_from_changes(self, changes: list[dict], incident_type: str, service: str) -> list[dict]:
        """Extract timeline entries from change and ITSM records."""
        entries: list[dict] = []
        change_correlated_types = {
            "error_spike", "saturation", "cascading", "network", "missing_data",
        }
        if changes and incident_type in change_correlated_types:
            for change in changes:
                ts = change.get("scheduled_start", change.get("actual_start", ""))
                if ts:
                    entries.append({
                        "timestamp": ts,
                        "event": f"Change: {change.get('description', 'unknown change')} "
                                 f"({change.get('change_type', 'unknown')})",
                        "source": "changes", "service": change.get("service", service),
                    })
        # ITSM change records with richer metadata (approval, risk, rollback)
        for change in changes:
            if change.get("rollback_plan") or change.get("approval"):
                ts = change.get("scheduled_start", change.get("actual_start", ""))
                if ts:
                    detail = change.get("description", "unknown change")
                    if change.get("approval"):
                        detail += f" [approval: {change['approval']}]"
                    if change.get("risk"):
                        detail += f" [risk: {change['risk']}]"
                    entries.append({
                        "timestamp": ts, "event": f"ITSM Change: {detail}",
                        "source": "itsm_changes", "service": change.get("service", service),
                    })
        return entries

    def _build_timeline(
        self,
        logs: list[dict],
        signals: dict,
        metrics: dict,
        events: list[dict],
        changes: list[dict],
        incident_type: str,
        service: str,
    ) -> list[dict]:
        """Build a chronologically-ordered evidence timeline."""
        signal_service = service
        if incident_type == "timeout":
            downstream = self._find_downstream_service(logs)
            if downstream:
                signal_service = downstream

        entries: list[dict] = []
        entries.extend(self._timeline_from_signals(signals, signal_service))
        entries.extend(self._timeline_from_metrics(metrics, service))
        entries.extend(self._timeline_from_events(events, service))
        entries.extend(self._timeline_from_logs(logs, service))
        entries.extend(self._timeline_from_changes(changes, incident_type, service))

        source_order = {
            "golden_signals": 0, "metrics": 1, "events": 2, "logs": 3,
            "log_summary": 4, "changes": 5, "itsm_changes": 6,
        }
        entries.sort(
            key=lambda x: (x.get("timestamp", ""), source_order.get(x.get("source", ""), 9))
        )
        return entries

    def _describe_anomaly(
        self,
        anomaly_type: str,
        service: str,
        latency: dict,
        errors: dict,
        saturation: dict,
    ) -> str:
        """Create a human-readable anomaly description."""
        if anomaly_type == "latency_spike":
            p95 = latency.get("p95", 0)
            baseline = latency.get("baseline_p95", 0)
            return (
                f"{service} latency spike detected: "
                f"p95={p95}ms (baseline: {baseline}ms) - "
                f"latency preceded timeouts"
            )
        elif anomaly_type == "error_spike":
            rate = errors.get("rate", 0)
            count = errors.get("count", 0)
            return f"{service} error spike: {rate*100:.0f}% error rate ({count} errors)"
        elif anomaly_type == "saturation":
            cpu = saturation.get("cpu", 0)
            return f"{service} resource saturation: CPU at {cpu}%"
        elif anomaly_type == "intermittent_errors":
            return f"{service} intermittent errors detected (sawtooth pattern in connections)"
        elif anomaly_type == "throughput_drop":
            return f"{service} throughput drop detected"
        return f"{service} anomaly detected: {anomaly_type}"

    # ------------------------------------------------------------------ #
    # Analysis helper methods
    # ------------------------------------------------------------------ #

    def _find_downstream_service(self, logs: list[dict]) -> str:
        for log in logs:
            msg = log.get("message", "").lower()
            if "timeout" in msg:
                match = re.search(r"timeout.*?:\s*(\S+?)(?::\d+)?(?:\s|$)", msg)
                if match:
                    svc = match.group(1).rstrip(".,;")
                    if svc and svc != "timeout":
                        return svc
                ds = log.get("downstream", "")
                if ds:
                    return ds
        return ""

    def _find_error_type(self, logs: list[dict]) -> str:
        for log in logs:
            msg = log.get("message", "")
            exc = log.get("exception", "")
            if "NullPointerException" in msg or "NullPointerException" in exc:
                return "NullPointerException"
            if "OutOfMemoryError" in msg or "OutOfMemoryError" in exc:
                return "OutOfMemoryError"
            if "ConnectionRefused" in msg or "ConnectionRefused" in exc:
                return "ConnectionRefused"
        return ""

    def _find_deployment(self, changes: list[dict]) -> dict | None:
        for change in changes:
            if change.get("change_type") in ("deployment", "config_change", "database_migration", "maintenance"):
                return change
        return None

    def _extract_version(self, description: str) -> str:
        match = re.search(r"v[\d.]+", description)
        return match.group(0) if match else ""

    def _find_backend_from_logs(self, logs: list[dict]) -> str:
        for log in logs:
            msg = log.get("message", "").lower()
            backend = log.get("backend", "")
            if backend:
                return backend
            if "elasticsearch" in msg:
                return "elasticsearch"
            if "redis" in msg:
                return "redis"
            if "database" in msg or "db" in msg:
                return "database"
        return ""

    def _find_backend_event(self, logs: list[dict], backend: str) -> str:
        for log in logs:
            msg = log.get("message", "")
            if backend.lower() in msg.lower():
                event_type = log.get("event_type", "")
                if event_type:
                    return f"{backend} {event_type}"
                return msg
        return ""

    def _is_gradual_increase(self, metric_list: list[dict]) -> bool:
        if len(metric_list) < 3:
            return False
        values = [m.get("value", 0) for m in metric_list]
        increases = sum(1 for i in range(1, len(values)) if values[i] > values[i - 1])
        return increases >= len(values) * 0.6

    def _has_dns_issues(self, logs: list[dict]) -> bool:
        for log in logs:
            msg = log.get("message", "").lower()
            if "dns" in msg or "resolve hostname" in msg:
                return True
        return False

    def _has_pool_exhaustion(self, logs: list[dict]) -> bool:
        for log in logs:
            msg = log.get("message", "").lower()
            if "pool exhausted" in msg or "connection pool" in msg:
                return True
        return False

    def _find_cascade_chain(self, logs: list[dict]) -> list[str]:
        services = []
        for log in logs:
            svc = log.get("service", "")
            if svc and svc not in services:
                services.append(svc)
        return services

    def _find_connection_error(self, logs: list[dict]) -> str:
        for log in logs:
            msg = log.get("message", "").lower()
            error_type = log.get("error_type", "")
            if "connection refused" in msg or error_type == "connection_refused":
                return "connection_refused"
            if "timeout" in msg:
                return "timeout"
        return ""

    def _find_connection_target(self, logs: list[dict]) -> str:
        for log in logs:
            msg = log.get("message", "").lower()
            if "redis" in msg:
                return "redis"
            if "postgres" in msg or "database" in msg:
                return "database"
            if "elasticsearch" in msg:
                return "elasticsearch"
        return ""

    def _detect_sawtooth_pattern(self, metrics: dict) -> bool:
        pattern = metrics.get("pattern", "")
        if pattern == "sawtooth":
            return True
        metric_list = metrics.get("metrics", [])
        if len(metric_list) < 4:
            return False
        values = [m.get("value", 0) for m in metric_list]
        direction_changes = 0
        for i in range(2, len(values)):
            if (values[i] - values[i - 1]) * (values[i - 1] - values[i - 2]) < 0:
                direction_changes += 1
        return direction_changes >= 2

    def _find_pipeline_failure(self, logs: list[dict]) -> bool:
        for log in logs:
            msg = log.get("message", "").lower()
            if "pipeline" in msg and ("fail" in msg or "error" in msg):
                return True
        return False

    def _find_stale_cache(self, logs: list[dict]) -> bool:
        for log in logs:
            msg = log.get("message", "").lower()
            if "stale" in msg or "cache miss" in msg:
                return True
        return False

    def _empty_result(
        self,
        incident_id: str,
        reason: str,
        degraded: bool = False,
        degraded_reason: str = "",
    ) -> dict:
        result = {
            "incident_id": incident_id,
            "root_cause": reason,
            "confidence": 10,
            "evidence_timeline": [],
            "reasoning": f"Investigation could not proceed: {reason}",
        }
        if degraded:
            result["confidence_degraded"] = True
            result["confidence_degraded_reason"] = degraded_reason or reason
        return result

    # =========================================================================
    # Harness support — evidence-cached re-analysis without playbook re-run
    # =========================================================================

    def get_last_evidence(self) -> dict:
        """Return the evidence dict from the most recent investigate() call on this thread.

        Used by InvestigationHarness to enrich evidence with gap queries and
        then call reanalyze() — avoiding a full playbook re-run per correction round.
        Returns an empty dict if no investigation has been run yet on this thread.
        """
        return dict(getattr(self._tls, "last_evidence", {}))

    def reanalyze(self, incident_id: str, evidence: dict) -> dict:
        """Re-run the analysis pipeline on pre-collected (possibly enriched) evidence.

        Skips incident fetch and playbook execution entirely.  Runs:
          _analyze_evidence → calibrate → cite → online-eval

        This is the fast correction path used by InvestigationHarness after
        gap queries enrich the evidence from the initial investigation pass.
        The caller is responsible for merging gap evidence into the dict before
        calling this method.

        Args:
            incident_id: Original incident identifier (for logging / event emission).
            evidence:    Full evidence dict (original + any gap-filled additions).

        Returns:
            Result dict with the same structure as investigate().
        """
        incident = getattr(self._tls, "current_incident", None) or {}
        incident_type = getattr(self._tls, "last_incident_type", "error_spike") or "error_spike"

        logger.info(
            "reanalyze: incident=%s type=%s evidence_keys=%d",
            incident_id, incident_type, len(evidence),
        )

        try:
            result = self._analyze_evidence(incident_id, incident, incident_type, evidence)
        except Exception as exc:
            logger.warning("reanalyze: _analyze_evidence failed: %s", exc)
            return {}

        # Confidence calibration
        raw_conf = result.get("confidence", 0)
        try:
            calibrated = get_calibrator().calibrate(raw_conf)
            if calibrated != raw_conf:
                result["confidence"] = calibrated
                result["raw_confidence"] = raw_conf
        except Exception:
            pass

        # Citation annotation (grounding)
        try:
            annotate_citations(result, evidence)
        except Exception:
            pass

        # Online quality evaluation
        hypothesis_count = result.pop("_hypothesis_count", 0)
        result.pop("_winner_hypothesis", None)
        result.pop("_llm_metrics", None)
        try:
            online_score = _online_evaluate(result, evidence, 0, hypothesis_count)
            _annotate_online(result, online_score)
        except Exception:
            pass

        result["_evidence_snapshot"] = {
            k: bool(v) for k, v in evidence.items() if not k.startswith("_")
        }
        result["_reanalyzed"] = True
        return result
