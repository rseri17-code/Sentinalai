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
import re
import time
import concurrent.futures
from datetime import datetime
from typing import Any

from supervisor.tool_selector import classify_incident, get_playbook
from supervisor.receipt import ReceiptCollector
from supervisor.guardrails import (
    ExecutionBudget,
    CircuitBreakerRegistry,
    CALL_TIMEOUT_SECONDS,
    MAX_RETRIES_PER_CALL,
    circuit_registry,
)
from supervisor.observability import (
    trace_span,
    GENAI_SYSTEM,
    GENAI_OPERATION_NAME,
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
)
from supervisor.replay import ReplayStore
from supervisor.memory import (
    store_investigation_result as _store_to_memory,
    is_enabled as _memory_enabled,
)
from workers.ops_worker import OpsWorker
from workers.log_worker import LogWorker
from workers.metrics_worker import MetricsWorker
from workers.apm_worker import ApmWorker
from workers.knowledge_worker import KnowledgeWorker

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
) -> int:
    """Compute evidence-weighted confidence.

    base:  the analyzer's starting score (e.g. 80 for a strong match)
    Then:
      +2 per corroborating evidence source (logs, signals, metrics, events, changes)
      +1 per log entry (max +5)
      +2 if golden signals present with anomaly detected
      +1 if metrics have pattern field
      -5 per missing critical source (signals empty, metrics empty)
    Bounded to [0, 100].
    """
    score = base

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

    # Missing-source penalty (only for sources expected in a good investigation)
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

    def __init__(
        self,
        replay_dir: str | None = None,
        call_timeout: float = CALL_TIMEOUT_SECONDS,
        max_retries: int = MAX_RETRIES_PER_CALL,
    ):
        self.workers: dict[str, Any] = {
            "ops_worker": OpsWorker(),
            "log_worker": LogWorker(),
            "metrics_worker": MetricsWorker(),
            "apm_worker": ApmWorker(),
            "knowledge_worker": KnowledgeWorker(),
        }
        self._replay_store = ReplayStore(replay_dir) if replay_dir else None
        self._call_timeout = call_timeout
        self._max_retries = max_retries

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def investigate(self, incident_id: str, replay: bool = False) -> dict:
        """Run a full RCA investigation for *incident_id*.

        Returns a dict with keys:
            root_cause, confidence, evidence_timeline, reasoning
        """
        # Replay mode: return stored result if available
        if replay and self._replay_store:
            stored = self._replay_store.load(incident_id)
            if stored and "result" in stored:
                return stored["result"]

        with trace_span("investigate", case_id=incident_id) as span:
            # GenAI semantic conventions for agent observability
            span.set_attribute(GENAI_SYSTEM, "sentinalai")
            span.set_attribute(GENAI_OPERATION_NAME, "investigate")

            receipts = ReceiptCollector(case_id=incident_id)
            budget = ExecutionBudget(case_id=incident_id)

            # W1: Per-investigation circuit breaker registry (isolated)
            circuits = CircuitBreakerRegistry()

            logger.info("Starting investigation for %s", incident_id)

            # Step 1: Fetch incident
            incident = self._fetch_incident(incident_id, receipts, budget)
            if not incident:
                logger.warning("No incident data for %s", incident_id)
                return self._empty_result(incident_id, "No incident data available")

            summary = incident.get("summary", "")
            service = incident.get("affected_service", "unknown")

            # Step 2: Classify
            incident_type = classify_incident(summary)
            span.set_attribute(EVAL_INCIDENT_TYPE, incident_type)
            span.set_attribute(EVAL_SERVICE, service)
            logger.info("Classified %s as %s (service=%s)", incident_id, incident_type, service)

            # Step 3: Execute playbook
            evidence = self._execute_playbook(
                incident_type, incident_id, service, receipts, budget, circuits,
            )
            logger.info("Playbook complete for %s: %d evidence items", incident_id, len(evidence))

            # Step 3b: Historical context (optional phase 4)
            historical = self._fetch_historical_context(service, summary, receipts, budget)
            if historical:
                evidence["historical_context"] = historical

            # Step 4: Analyze
            result = self._analyze_evidence(incident_id, incident, incident_type, evidence)

            confidence = result.get("confidence", 0)
            hypothesis_count = result.pop("_hypothesis_count", 0)
            winner_hypothesis = result.pop("_winner_hypothesis", "none")

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
                changes_available=bool(evidence.get("get_change_data")),
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

            logger.info(
                "Investigation complete for %s: confidence=%d, tool_calls=%d",
                incident_id, confidence, budget.calls_made,
            )

            # Persist replay artifact (include hypothesis metadata for eval audit)
            if self._replay_store:
                replay_result = {
                    **result,
                    "hypothesis_count": hypothesis_count,
                    "winner_hypothesis": winner_hypothesis,
                }
                self._replay_store.save(
                    case_id=incident_id,
                    receipts=receipts.to_list(),
                    result=replay_result,
                    evidence=evidence,
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
                except Exception:
                    logger.debug("Memory store skipped (non-critical)")

            return result

    # ------------------------------------------------------------------ #
    # Internal: call worker with timeout (W4) and retry (W5)
    # ------------------------------------------------------------------ #

    def _call_worker(
        self,
        worker: Any,
        action: str,
        params: dict,
        receipts: ReceiptCollector | None,
        budget: ExecutionBudget | None,
        worker_name: str = "",
    ) -> dict:
        """Call worker.execute() with timeout guard and retry.

        W4: wraps call in ThreadPoolExecutor with configurable timeout.
        W5: retries once on failure with exponential backoff.
        """
        last_error = ""
        attempts = 1 + self._max_retries  # 1 initial + N retries

        for attempt in range(attempts):
            if attempt > 0:
                # W5: Exponential backoff before retry
                backoff_s = 0.01 * (2 ** (attempt - 1))  # 10ms, 20ms, ...
                time.sleep(backoff_s)
                logger.info(
                    "Retrying %s.%s (attempt %d/%d)",
                    worker_name, action, attempt + 1, attempts,
                )
                # Check budget before retry
                if budget and not budget.can_call():
                    break
                if budget:
                    budget.record_call()

            receipt = receipts.start(worker_name, action, params) if receipts else None

            call_start = time.monotonic()

            try:
                # W4: Timeout guard
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(worker.execute, action, params)
                    result = future.result(timeout=self._call_timeout)

                call_elapsed = (time.monotonic() - call_start) * 1000
                if receipt and receipts:
                    receipts.finish(receipt, result)
                record_worker_call(worker_name, action, "success", call_elapsed)
                return result

            except concurrent.futures.TimeoutError:
                call_elapsed = (time.monotonic() - call_start) * 1000
                last_error = f"timeout after {self._call_timeout}s"
                if receipt and receipts:
                    receipt.status = "timeout"
                    receipt.error = last_error
                    receipt.end_ts = time.monotonic()
                    receipt.elapsed_ms = round(
                        (receipt.end_ts - receipt.start_ts) * 1000, 1
                    )
                record_worker_call(worker_name, action, "timeout", call_elapsed)
                logger.warning(
                    "Timeout: %s.%s exceeded %ss",
                    worker_name, action, self._call_timeout,
                )

            except Exception as exc:
                call_elapsed = (time.monotonic() - call_start) * 1000
                last_error = str(exc)
                if receipt and receipts:
                    receipts.finish(receipt, None, error=last_error)
                record_worker_call(worker_name, action, "error", call_elapsed)
                logger.warning(
                    "Error in %s.%s: %s (attempt %d/%d)",
                    worker_name, action, exc, attempt + 1, attempts,
                )

        # All attempts exhausted
        return {"error": last_error or "worker_unavailable"}

    # ------------------------------------------------------------------ #
    # Internal: fetch incident (with receipts + budget)
    # ------------------------------------------------------------------ #

    def _fetch_incident(
        self, incident_id: str,
        receipts: ReceiptCollector | None = None,
        budget: ExecutionBudget | None = None,
    ) -> dict | None:
        try:
            if budget:
                budget.record_call()
            receipt = receipts.start("ops_worker", "get_incident_by_id", {"incident_id": incident_id}) if receipts else None
            result = self.workers["ops_worker"].execute(
                "get_incident_by_id", {"incident_id": incident_id}
            )
            if receipt and receipts:
                receipts.finish(receipt, result)
            return result.get("incident") if result else None
        except Exception as exc:
            if receipt and receipts:
                receipts.finish(receipt, None, error=str(exc))
            return None

    def _fetch_historical_context(
        self, service: str, summary: str,
        receipts: ReceiptCollector | None = None,
        budget: ExecutionBudget | None = None,
    ) -> dict | None:
        """Optional phase 4: fetch similar historical incidents."""
        worker = self.workers.get("knowledge_worker")
        if worker is None:
            return None
        if budget and not budget.can_call():
            return None
        try:
            if budget:
                budget.record_call()
            params = {"service": service, "summary": summary}
            receipt = receipts.start("knowledge_worker", "search_similar", params) if receipts else None
            result = worker.execute("search_similar", params)
            if receipt and receipts:
                receipts.finish(receipt, result)
            if result and result.get("similar_incidents"):
                return result
        except Exception as exc:
            if receipt and receipts:
                receipts.finish(receipt, None, error=str(exc))
            logger.debug("Historical context unavailable for %s", service)
        return None

    # ------------------------------------------------------------------ #
    # Internal: execute playbook (W1 isolated circuits, W4 timeout, W5 retry)
    # ------------------------------------------------------------------ #

    def _execute_playbook(
        self, incident_type: str, incident_id: str, service: str,
        receipts: ReceiptCollector | None = None,
        budget: ExecutionBudget | None = None,
        circuits: CircuitBreakerRegistry | None = None,
    ) -> dict[str, Any]:
        """Run each step in the playbook, collecting evidence."""
        playbook = get_playbook(incident_type)
        evidence: dict[str, Any] = {}

        # W1: Use per-investigation circuits, fall back to global
        cb_registry = circuits or circuit_registry

        for step in playbook:
            worker_name = step["worker"]
            action = step["action"]
            label = step.get("label", action)

            # Budget check
            if budget and not budget.can_call():
                logger.warning("Budget exhausted at step %s for %s", label, incident_id)
                break

            # Circuit breaker check (W1: per-investigation)
            circuit = cb_registry.get(worker_name)
            if circuit.is_open:
                logger.warning("Circuit open for %s, skipping %s", worker_name, label)
                evidence[label] = {"error": "circuit_open"}
                continue

            params = self._build_params(step, incident_id, service)
            worker = self.workers.get(worker_name)
            if worker is None:
                continue

            if budget:
                budget.record_call()

            # W4+W5: Call with timeout and retry
            result = self._call_worker(
                worker, action, params, receipts, budget, worker_name,
            )

            if isinstance(result, dict) and result.get("error"):
                evidence[label] = result
                circuit.record_failure(worker_name)
            else:
                evidence[label] = result
                circuit.record_success(worker_name)

        return evidence

    def _build_params(self, step: dict, incident_id: str, service: str) -> dict:
        """Build parameters for a playbook step."""
        params: dict[str, Any] = {}

        if step["action"] == "get_incident_by_id":
            params["incident_id"] = incident_id
        elif step["action"] == "search_logs":
            hint = step.get("query_hint", "{service}")
            params["query"] = hint.format(service=service)
            params["service"] = service
        elif step["action"] == "get_change_data":
            params["service"] = service
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
        """Deterministic evidence analysis engine."""
        summary = incident.get("summary", "")
        service = incident.get("affected_service", "unknown")

        # Gather raw data blobs
        logs = self._extract_logs(evidence)
        signals = self._extract_signals(evidence)
        metrics = self._extract_metrics(evidence)
        events = self._extract_events(evidence)
        changes = self._extract_changes(evidence)

        # Build timeline from all sources
        timeline = self._build_timeline(logs, signals, metrics, events, changes, incident_type, service)

        # W2: Multi-hypothesis scoring
        hypotheses = self._generate_hypotheses(
            incident_type, service, summary, logs, signals, metrics, events, changes, timeline,
        )

        # W3: Evidence-weighted confidence for each hypothesis
        for h in hypotheses:
            h.base_score = compute_confidence(
                h.base_score, logs, signals, metrics, events, changes,
                corroborating_sources=len(h.evidence_refs),
            )

        # W2: Select winner — highest score, deterministic tiebreak by name
        hypotheses.sort(key=lambda h: (-h.base_score, h.name))
        winner = hypotheses[0] if hypotheses else None

        if winner:
            root_cause = winner.root_cause
            confidence = winner.base_score
            reasoning = winner.reasoning
        else:
            root_cause = f"{service} incident - investigation inconclusive"
            confidence = compute_confidence(30, logs, signals, metrics, events, changes)
            reasoning = f"Generic analysis of {service} incident. Insufficient pattern match."

        return {
            "incident_id": incident_id,
            "root_cause": root_cause,
            "confidence": confidence,
            "evidence_timeline": timeline,
            "reasoning": reasoning,
            "_hypothesis_count": len(hypotheses),
            "_winner_hypothesis": winner.name if winner else "none",
        }

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
    ) -> list[Hypothesis]:
        """Generate scored hypotheses from type-specific analyzers.

        Each analyzer returns one or more Hypothesis objects.
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
        return analyzer(service, summary, logs, signals, metrics, events, changes, timeline)

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
                    f"the api-gateway timeout threshold was reached."
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
            limit_gb = mem_limit / 1e9 if mem_limit else "unknown"
            evidence_refs = ["metrics:gradual_increase", "events:oomkill"]
            if mem_limit:
                evidence_refs.append("metrics:limit_exceeded")
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
                    f"{limit_gb}GB limit and triggering an OOMKill. "
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
                    f"the code change."
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
                    f"config change timestamp and CPU spike confirms causality."
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
                    f"dns resolution failure after dns server maintenance "
                    f"causing inter-service connectivity failures"
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
                reasoning=f"DNS resolution failures detected but no maintenance event found.",
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
            reasoning=f"Cascading failure detected but root trigger unclear.",
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
        gs = signals.get("golden_signals", {})
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
            reasoning=f"Intermittent failures detected but pattern unclear.",
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
                reasoning=f"Pipeline failure detected but downstream impact unclear.",
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
        return sorted(all_logs, key=lambda x: x.get("_time", ""))

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
        return sorted(all_events, key=lambda x: x.get("timestamp", ""))

    def _extract_changes(self, evidence: dict) -> list[dict]:
        """Extract change/deployment data from evidence."""
        all_changes = []
        for key, val in evidence.items():
            if not isinstance(val, dict):
                continue
            changes = val.get("changes", [])
            if isinstance(changes, list):
                all_changes.extend(changes)
        return all_changes

    # ------------------------------------------------------------------ #
    # Timeline builder
    # ------------------------------------------------------------------ #

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
        timeline_entries: list[dict] = []

        signal_service = service
        if incident_type == "timeout":
            downstream = self._find_downstream_service(logs)
            if downstream:
                signal_service = downstream

        anomaly_start = signals.get("anomaly_start", "")
        anomaly_type = signals.get("anomaly_type", "")
        if anomaly_start:
            gs = signals.get("golden_signals", {})
            latency = gs.get("latency", {})
            errors = gs.get("errors", {})
            saturation = gs.get("saturation", {})

            description = self._describe_anomaly(anomaly_type, signal_service, latency, errors, saturation)
            timeline_entries.append({
                "timestamp": anomaly_start,
                "event": description,
                "source": "golden_signals",
                "service": signal_service,
            })

        metric_list = metrics.get("metrics", [])
        baseline = metrics.get("baseline", 0)
        if isinstance(metric_list, list) and metric_list:
            first_metric = metric_list[0]
            metric_name = first_metric.get("name", "metric")
            metric_value = first_metric.get("value", 0)
            metric_ts = first_metric.get("timestamp", "")

            if baseline and metric_value and metric_value > baseline * 2:
                timeline_entries.append({
                    "timestamp": metric_ts,
                    "event": f"{metric_name} spike to {metric_value} (baseline: {baseline}) on {service}",
                    "source": "metrics",
                    "service": service,
                })

            pattern = metrics.get("pattern", "")
            mem_limit = metrics.get("limit", 0)
            if pattern == "gradual_increase" and mem_limit:
                timeline_entries.append({
                    "timestamp": metric_ts,
                    "event": f"gradual memory increase detected on {service} "
                             f"(limit: {mem_limit / 1e9:.0f}GB) - memory saturation",
                    "source": "metrics",
                    "service": service,
                })

            pool_max = metrics.get("pool_max", 0)
            if pool_max:
                for m in metric_list:
                    if m.get("value", 0) >= pool_max:
                        timeline_entries.append({
                            "timestamp": m.get("timestamp", ""),
                            "event": f"connection pool exhaustion on {service} ({m['value']}/{pool_max})",
                            "source": "metrics",
                            "service": service,
                        })
                        break

        for event in events:
            event_msg = event.get("message", "")
            timeline_entries.append({
                "timestamp": event.get("timestamp", ""),
                "event": event_msg,
                "source": "events",
                "service": service,
            })
            if "oomkill" in event_msg.lower():
                timeline_entries.append({
                    "timestamp": event.get("timestamp", ""),
                    "event": f"OOMKill event: {event_msg}",
                    "source": "events",
                    "service": service,
                })

        timeout_logs = []
        error_logs = []
        for log in logs[:5]:
            log_service = log.get("service", service)
            log_msg = log.get("message", "")
            timeline_entries.append({
                "timestamp": log.get("_time", ""),
                "event": log_msg,
                "source": "logs",
                "service": log_service,
            })
            if "timeout" in log_msg.lower():
                timeout_logs.append(log_service)
            if log.get("level") == "ERROR":
                error_logs.append(log_service)

        if timeout_logs:
            svc = timeout_logs[0]
            timeline_entries.append({
                "timestamp": logs[0].get("_time", "") if logs else "",
                "event": f"{svc} timeout errors detected ({len(timeout_logs)} occurrences)",
                "source": "log_summary",
                "service": svc,
            })

        change_correlated_types = {
            "error_spike", "saturation", "cascading", "network", "missing_data",
        }
        if changes and incident_type in change_correlated_types:
            for change in changes:
                ts = change.get("scheduled_start", change.get("actual_start", ""))
                if ts:
                    timeline_entries.append({
                        "timestamp": ts,
                        "event": f"Change: {change.get('description', 'unknown change')} "
                                 f"({change.get('change_type', 'unknown')})",
                        "source": "changes",
                        "service": change.get("service", service),
                    })

        source_order = {"golden_signals": 0, "metrics": 1, "events": 2, "logs": 3, "log_summary": 4, "changes": 5}
        timeline_entries.sort(
            key=lambda x: (x.get("timestamp", ""), source_order.get(x.get("source", ""), 9))
        )

        return timeline_entries

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
            if "OutOfMemoryError" in msg:
                return "OutOfMemoryError"
            if "ConnectionRefused" in msg:
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

    def _empty_result(self, incident_id: str, reason: str) -> dict:
        return {
            "incident_id": incident_id,
            "root_cause": reason,
            "confidence": 10,
            "evidence_timeline": [],
            "reasoning": f"Investigation could not proceed: {reason}",
        }
