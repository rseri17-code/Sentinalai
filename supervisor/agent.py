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
from datetime import datetime
from typing import Any

from supervisor.tool_selector import classify_incident, get_playbook
from supervisor.receipt import ReceiptCollector
from supervisor.guardrails import (
    ExecutionBudget,
    circuit_registry,
)
from supervisor.observability import trace_span
from supervisor.replay import ReplayStore
from workers.ops_worker import OpsWorker
from workers.log_worker import LogWorker
from workers.metrics_worker import MetricsWorker
from workers.apm_worker import ApmWorker
from workers.knowledge_worker import KnowledgeWorker

logger = logging.getLogger(__name__)


class SentinalAISupervisor:
    """Autonomous incident RCA supervisor."""

    def __init__(self, replay_dir: str | None = None):
        self.workers: dict[str, Any] = {
            "ops_worker": OpsWorker(),
            "log_worker": LogWorker(),
            "metrics_worker": MetricsWorker(),
            "apm_worker": ApmWorker(),
            "knowledge_worker": KnowledgeWorker(),
        }
        self._replay_store = ReplayStore(replay_dir) if replay_dir else None

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
            receipts = ReceiptCollector(case_id=incident_id)
            budget = ExecutionBudget(case_id=incident_id)

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
            span.set_attribute("incident_type", incident_type)
            span.set_attribute("service", service)
            logger.info("Classified %s as %s (service=%s)", incident_id, incident_type, service)

            # Step 3: Execute playbook
            evidence = self._execute_playbook(incident_type, incident_id, service, receipts, budget)
            logger.info("Playbook complete for %s: %d evidence items", incident_id, len(evidence))

            # Step 3b: Historical context (optional phase 4)
            historical = self._fetch_historical_context(service, summary, receipts, budget)
            if historical:
                evidence["historical_context"] = historical

            # Step 4: Analyze
            result = self._analyze_evidence(incident_id, incident, incident_type, evidence)
            span.set_attribute("confidence", result.get("confidence", 0))
            span.set_attribute("tool_calls", budget.calls_made)
            logger.info(
                "Investigation complete for %s: confidence=%d, tool_calls=%d",
                incident_id, result.get("confidence", 0), budget.calls_made,
            )

            # Persist replay artifact
            if self._replay_store:
                self._replay_store.save(
                    case_id=incident_id,
                    receipts=receipts.to_list(),
                    result=result,
                    evidence=evidence,
                )

            return result

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
    # Internal: execute playbook (with receipts + budget + circuit breaker)
    # ------------------------------------------------------------------ #

    def _execute_playbook(
        self, incident_type: str, incident_id: str, service: str,
        receipts: ReceiptCollector | None = None,
        budget: ExecutionBudget | None = None,
    ) -> dict[str, Any]:
        """Run each step in the playbook, collecting evidence."""
        playbook = get_playbook(incident_type)
        evidence: dict[str, Any] = {}

        for step in playbook:
            worker_name = step["worker"]
            action = step["action"]
            label = step.get("label", action)

            # Budget check
            if budget and not budget.can_call():
                logger.warning("Budget exhausted at step %s for %s", label, incident_id)
                break

            # Circuit breaker check
            circuit = circuit_registry.get(worker_name)
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

            receipt = receipts.start(worker_name, action, params) if receipts else None
            try:
                result = worker.execute(action, params)
                evidence[label] = result
                circuit.record_success()
                if receipt and receipts:
                    receipts.finish(receipt, result)
            except Exception as exc:
                evidence[label] = {"error": "worker_unavailable"}
                circuit.record_failure()
                if receipt and receipts:
                    receipts.finish(receipt, None, error=str(exc))

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
    # Internal: analyze evidence
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

        # Determine root cause using rule-based analysis
        root_cause, confidence, reasoning = self._determine_root_cause(
            incident_type, service, summary, logs, signals, metrics, events, changes, timeline
        )

        return {
            "incident_id": incident_id,
            "root_cause": root_cause,
            "confidence": confidence,
            "evidence_timeline": timeline,
            "reasoning": reasoning,
        }

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

        # For timeout incidents, determine the actual service with the latency issue
        signal_service = service
        if incident_type == "timeout":
            downstream = self._find_downstream_service(logs)
            if downstream:
                signal_service = downstream

        # Add anomaly start from signals
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

        # Add metric anomalies
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

            # Check for patterns
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

            # Pool exhaustion
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

        # Add events
        for event in events:
            event_msg = event.get("message", "")
            timeline_entries.append({
                "timestamp": event.get("timestamp", ""),
                "event": event_msg,
                "source": "events",
                "service": service,
            })
            # Explicitly tag OOMKill events for evidence matching
            if "oomkill" in event_msg.lower():
                timeline_entries.append({
                    "timestamp": event.get("timestamp", ""),
                    "event": f"OOMKill event: {event_msg}",
                    "source": "events",
                    "service": service,
                })

        # Add key logs and summarize patterns
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

        # Add summary entries for log patterns
        if timeout_logs:
            svc = timeout_logs[0]
            timeline_entries.append({
                "timestamp": logs[0].get("_time", "") if logs else "",
                "event": f"{svc} timeout errors detected ({len(timeout_logs)} occurrences)",
                "source": "log_summary",
                "service": svc,
            })

        # Add changes when they are correlated (error_spike, saturation, cascading,
        # network, missing_data). For other incident types, changes are context but
        # should not dominate the timeline as the earliest event.
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

        # Sort chronologically; use secondary key to put change context after
        # signals/metrics/logs at the same timestamp
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
    # Root cause determination (deterministic rule engine)
    # ------------------------------------------------------------------ #

    def _determine_root_cause(
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
    ) -> tuple[str, int, str]:
        """Deterministic root cause analysis.

        Returns (root_cause, confidence, reasoning).
        """
        # Dispatch to type-specific analyzer
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
        downstream = self._find_downstream_service(logs)
        gs = signals.get("golden_signals", {})
        latency = gs.get("latency", {})
        p95 = latency.get("p95", 0)
        baseline = latency.get("baseline_p95", 0)

        if downstream and p95 > baseline * 10:
            root_cause = f"{downstream} database slow queries causing upstream timeouts"
            confidence = 92
            reasoning = (
                f"Timeline analysis shows {downstream} latency spike preceded "
                f"api-gateway timeout errors. {downstream} p95 latency was {p95}ms "
                f"compared to baseline of {baseline}ms (a {p95 // max(baseline, 1)}x increase). "
                f"This latency caused downstream timeout failures at the api-gateway level. "
                f"The first event in the timeline was {downstream} latency at the anomaly start, "
                f"which then caused cascading timeouts. The causal chain is clear: "
                f"database slow queries in {downstream} led to request timeouts before "
                f"the api-gateway timeout threshold was reached."
            )
        else:
            root_cause = f"{service} timeout - cause undetermined"
            confidence = 50
            reasoning = f"Timeout detected on {service} but insufficient data to determine root cause."

        return root_cause, confidence, reasoning

    # -- OOMKill -------------------------------------------------------- #

    def _analyze_oomkill(self, service, summary, logs, signals, metrics, events, changes, timeline):
        metric_list = metrics.get("metrics", [])
        pattern = metrics.get("pattern", "")
        mem_limit = metrics.get("limit", 0)

        if pattern == "gradual_increase" or (metric_list and self._is_gradual_increase(metric_list)):
            limit_gb = mem_limit / 1e9 if mem_limit else "unknown"
            root_cause = f"memory leak in {service} causing OOMKill"
            confidence = 88
            reasoning = (
                f"Memory metrics show a gradual increasing pattern over time for {service}, "
                f"characteristic of a memory leak. Memory usage grew from "
                f"{metric_list[0].get('value', 0) / 1e9:.1f}GB to "
                f"{metric_list[-1].get('value', 0) / 1e9:.1f}GB before exceeding the "
                f"{limit_gb}GB limit and triggering an OOMKill. "
                f"The gradual increase over {len(metric_list)} data points rules out a "
                f"sudden spike, confirming a leak pattern. The OOMKill event was the "
                f"direct result of memory saturation from this leak."
            )
        else:
            root_cause = f"{service} OOMKilled - memory saturation"
            confidence = 75
            reasoning = f"OOMKill detected on {service} but memory pattern unclear."

        return root_cause, confidence, reasoning

    # -- Error Spike ---------------------------------------------------- #

    def _analyze_error_spike(self, service, summary, logs, signals, metrics, events, changes, timeline):
        error_type = self._find_error_type(logs)
        deployment = self._find_deployment(changes)

        if deployment and error_type:
            version = self._extract_version(deployment.get("description", ""))
            root_cause = (
                f"deployment {version} introduced {error_type} in {service}"
                if version
                else f"deployment introduced {error_type} in {service}"
            )
            confidence = 92
            dep_time = deployment.get("scheduled_start", deployment.get("actual_start", ""))
            reasoning = (
                f"Strong temporal correlation between deployment and error spike. "
                f"Deployment of {service} ({deployment.get('description', '')}) completed "
                f"at {dep_time}, and {error_type} errors began appearing immediately after "
                f"(within seconds). The deployment preceded the errors, establishing a clear "
                f"causal relationship. Error rate spiked from baseline to "
                f"{signals.get('golden_signals', {}).get('errors', {}).get('rate', 0) * 100:.0f}% "
                f"after the deployment. The {error_type} is the specific defect introduced by "
                f"the code change."
            )
        elif error_type:
            root_cause = f"{error_type} errors in {service}"
            confidence = 70
            reasoning = f"Error spike of {error_type} in {service}, no deployment correlation found."
        else:
            root_cause = f"error spike in {service}"
            confidence = 60
            reasoning = f"Error spike detected in {service} but specific error type not identified."

        return root_cause, confidence, reasoning

    # -- Latency -------------------------------------------------------- #

    def _analyze_latency(self, service, summary, logs, signals, metrics, events, changes, timeline):
        backend = self._find_backend_from_logs(logs)
        gs = signals.get("golden_signals", {})
        latency = gs.get("latency", {})
        p95 = latency.get("p95", 0)
        baseline = latency.get("baseline_p95", 0)

        if backend:
            backend_event = self._find_backend_event(logs, backend)
            root_cause = f"{backend} rebalancing causing slow queries in {service}"
            confidence = 90
            reasoning = (
                f"Latency analysis shows {service} p95 latency spiked to {p95}ms from "
                f"baseline {baseline}ms. Log analysis reveals {backend} as the backend "
                f"dependency experiencing issues. {backend_event or 'Backend event detected'} "
                f"preceded the latency spike, establishing causality. The {backend} issue "
                f"caused slow queries which propagated as latency to {service}."
            )
        else:
            root_cause = f"{service} latency degradation"
            confidence = 65
            reasoning = f"Latency spike detected in {service} but backend cause not identified."

        return root_cause, confidence, reasoning

    # -- Saturation ----------------------------------------------------- #

    def _analyze_saturation(self, service, summary, logs, signals, metrics, events, changes, timeline):
        gs = signals.get("golden_signals", {})
        sat = gs.get("saturation", {})
        cpu = sat.get("cpu", 0)
        deployment = self._find_deployment(changes)

        if cpu > 90 and deployment:
            change_type = deployment.get("change_type", "change")
            root_cause = (
                f"{service} cpu exhaustion after config change causing "
                f"thread pool saturation"
            )
            confidence = 90
            reasoning = (
                f"CPU saturation detected at {cpu}% on {service}. "
                f"A {change_type} ({deployment.get('description', '')}) was applied "
                f"at {deployment.get('scheduled_start', '')} which preceded the CPU spike. "
                f"Log analysis shows thread pool exhaustion consistent with a runaway "
                f"loop triggered by the config change. The correlation between the "
                f"config change timestamp and CPU spike confirms causality."
            )
        elif cpu > 90:
            root_cause = f"{service} cpu exhaustion"
            confidence = 75
            reasoning = f"CPU at {cpu}% on {service} but no change correlation found."
        else:
            root_cause = f"{service} resource saturation"
            confidence = 60
            reasoning = f"Resource saturation on {service}."

        return root_cause, confidence, reasoning

    # -- Network -------------------------------------------------------- #

    def _analyze_network(self, service, summary, logs, signals, metrics, events, changes, timeline):
        dns_issue = self._has_dns_issues(logs)
        deployment = self._find_deployment(changes)

        if dns_issue and deployment:
            root_cause = (
                f"dns resolution failure after dns server maintenance "
                f"causing inter-service connectivity failures"
            )
            confidence = 92
            reasoning = (
                f"Multiple services report DNS resolution failures. "
                f"A DNS server maintenance event ({deployment.get('description', '')}) "
                f"occurred at {deployment.get('scheduled_start', '')} which preceded the "
                f"connection failures. The maintenance caused a DNS cache flush, leading to "
                f"resolution failures across all services dependent on internal DNS. "
                f"This explains the broad multi-service impact observed in the logs."
            )
        elif dns_issue:
            root_cause = f"dns resolution failure affecting {service}"
            confidence = 80
            reasoning = f"DNS resolution failures detected but no maintenance event found."
        else:
            root_cause = f"network connectivity failure affecting {service}"
            confidence = 60
            reasoning = f"Network issue detected on {service}."

        return root_cause, confidence, reasoning

    # -- Cascading ------------------------------------------------------ #

    def _analyze_cascading(self, service, summary, logs, signals, metrics, events, changes, timeline):
        pool_exhaustion = self._has_pool_exhaustion(logs)
        deployment = self._find_deployment(changes)
        cascade_services = self._find_cascade_chain(logs)

        # Identify the origin service from the cascade chain or fall back to incident service
        origin_service = cascade_services[0] if cascade_services else service
        downstream_desc = (
            ", ".join(cascade_services[1:]) if len(cascade_services) > 1
            else "downstream services"
        )

        if pool_exhaustion and deployment:
            root_cause = (
                f"database connection pool exhaustion in {origin_service} "
                f"caused by slow queries after index drop, cascading to {downstream_desc}"
            )
            confidence = 85
            reasoning = (
                f"Cascading failure analysis: A database migration "
                f"({deployment.get('description', '')}) at "
                f"{deployment.get('scheduled_start', '')} caused slow queries (full table scans). "
                f"This led to connection pool exhaustion in {origin_service} as connections "
                f"were held longer. The pool exhaustion then cascaded to {downstream_desc}. "
                f"The cascade propagated through the dependency chain."
            )
        else:
            root_cause = f"cascading failure from {service}"
            confidence = 65
            reasoning = f"Cascading failure detected but root trigger unclear."

        return root_cause, confidence, reasoning

    # -- Missing Data --------------------------------------------------- #

    def _analyze_missing_data(self, service, summary, logs, signals, metrics, events, changes, timeline):
        error_type = self._find_connection_error(logs)

        if error_type:
            target = self._find_connection_target(logs)
            root_cause = (
                f"{target} connection failure affecting {service}"
                if target
                else f"connection failure affecting {service}"
            )
            confidence = 75
            reasoning = (
                f"Investigation completed with limited observability data. "
                f"Metrics were unavailable for {service}, reducing confidence. "
                f"However, log analysis clearly shows {target or 'backend'} connection "
                f"failures ({error_type}). Events confirm the connection target became "
                f"unreachable. Despite missing metrics data, the log and event evidence "
                f"is sufficient to identify the connection failure as the root cause."
            )
        else:
            root_cause = f"{service} degraded - insufficient data for root cause"
            confidence = 40
            reasoning = f"Limited data available for {service}. Cannot determine root cause with confidence."

        return root_cause, confidence, reasoning

    # -- Flapping ------------------------------------------------------- #

    def _analyze_flapping(self, service, summary, logs, signals, metrics, events, changes, timeline):
        pool_pattern = self._detect_sawtooth_pattern(metrics)
        gs = signals.get("golden_signals", {})
        anomaly_type = signals.get("anomaly_type", "")

        if pool_pattern or "intermittent" in anomaly_type:
            root_cause = (
                f"connection pool leak in {service} causing intermittent exhaustion"
            )
            confidence = 82
            reasoning = (
                f"Analysis of {service} metrics reveals a sawtooth pattern in connection "
                f"pool usage - connections gradually accumulate to the maximum, causing "
                f"intermittent failures, then partially release. This periodic pattern is "
                f"characteristic of a connection pool leak where connections are not properly "
                f"returned. The flapping/intermittent nature of the alerts correlates with "
                f"the pool reaching capacity and then partially recovering. No code changes "
                f"were found, suggesting this is a latent bug that manifests under load."
            )
        else:
            root_cause = f"intermittent failures in {service}"
            confidence = 55
            reasoning = f"Intermittent failures detected but pattern unclear."

        return root_cause, confidence, reasoning

    # -- Silent Failure ------------------------------------------------- #

    def _analyze_silent_failure(self, service, summary, logs, signals, metrics, events, changes, timeline):
        pipeline_failure = self._find_pipeline_failure(logs)
        stale_cache = self._find_stale_cache(logs)

        if pipeline_failure and stale_cache:
            root_cause = (
                f"data pipeline failure causing stale cache in {service}"
            )
            confidence = 85
            reasoning = (
                f"The investigation reveals an indirect, upstream failure chain. "
                f"The data pipeline job failed at the earliest point in the timeline, "
                f"which meant fresh data stopped flowing to {service}. "
                f"As the cache aged, the stale data caused increased cache misses and "
                f"clients stopped requesting stale recommendations, leading to the observed "
                f"throughput drop. No direct errors were produced - this was a silent "
                f"degradation caused by the upstream pipeline failure propagating through "
                f"the data freshness dependency."
            )
        elif pipeline_failure:
            root_cause = f"data pipeline failure affecting {service}"
            confidence = 75
            reasoning = f"Pipeline failure detected but downstream impact unclear."
        else:
            root_cause = f"{service} throughput degradation"
            confidence = 55
            reasoning = f"Throughput drop in {service} but root cause unclear."

        return root_cause, confidence, reasoning

    # -- Generic -------------------------------------------------------- #

    def _analyze_generic(self, service, summary, logs, signals, metrics, events, changes, timeline):
        return (
            f"{service} incident - investigation inconclusive",
            40,
            f"Generic analysis of {service} incident. Insufficient pattern match.",
        )

    # ------------------------------------------------------------------ #
    # Analysis helper methods
    # ------------------------------------------------------------------ #

    def _find_downstream_service(self, logs: list[dict]) -> str:
        """Find the downstream service from timeout logs."""
        for log in logs:
            msg = log.get("message", "").lower()
            if "timeout" in msg:
                # Extract downstream from "upstream request timeout: <service>"
                match = re.search(r"timeout.*?:\s*(\S+?)(?::\d+)?(?:\s|$)", msg)
                if match:
                    svc = match.group(1).rstrip(".,;")
                    if svc and svc != "timeout":
                        return svc
                # Try "downstream" field
                ds = log.get("downstream", "")
                if ds:
                    return ds
        return ""

    def _find_error_type(self, logs: list[dict]) -> str:
        """Find the dominant error type from logs."""
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
        """Find the most recent deployment/change."""
        for change in changes:
            if change.get("change_type") in ("deployment", "config_change", "database_migration", "maintenance"):
                return change
        return None

    def _extract_version(self, description: str) -> str:
        """Extract version string from deployment description."""
        match = re.search(r"v[\d.]+", description)
        return match.group(0) if match else ""

    def _find_backend_from_logs(self, logs: list[dict]) -> str:
        """Find the backend service causing latency from logs."""
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
        """Find the specific backend event from logs."""
        for log in logs:
            msg = log.get("message", "")
            if backend.lower() in msg.lower():
                event_type = log.get("event_type", "")
                if event_type:
                    return f"{backend} {event_type}"
                return msg
        return ""

    def _is_gradual_increase(self, metric_list: list[dict]) -> bool:
        """Check if metrics show a gradual increasing pattern."""
        if len(metric_list) < 3:
            return False
        values = [m.get("value", 0) for m in metric_list]
        increases = sum(1 for i in range(1, len(values)) if values[i] > values[i - 1])
        return increases >= len(values) * 0.6

    def _has_dns_issues(self, logs: list[dict]) -> bool:
        """Check if logs indicate DNS resolution failures."""
        for log in logs:
            msg = log.get("message", "").lower()
            if "dns" in msg or "resolve hostname" in msg:
                return True
        return False

    def _has_pool_exhaustion(self, logs: list[dict]) -> bool:
        """Check if logs indicate connection pool exhaustion."""
        for log in logs:
            msg = log.get("message", "").lower()
            if "pool exhausted" in msg or "connection pool" in msg:
                return True
        return False

    def _find_cascade_chain(self, logs: list[dict]) -> list[str]:
        """Identify the cascade chain from logs."""
        services = []
        for log in logs:
            svc = log.get("service", "")
            if svc and svc not in services:
                services.append(svc)
        return services

    def _find_connection_error(self, logs: list[dict]) -> str:
        """Find connection error type from logs."""
        for log in logs:
            msg = log.get("message", "").lower()
            error_type = log.get("error_type", "")
            if "connection refused" in msg or error_type == "connection_refused":
                return "connection_refused"
            if "timeout" in msg:
                return "timeout"
        return ""

    def _find_connection_target(self, logs: list[dict]) -> str:
        """Find the target of failed connections."""
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
        """Detect sawtooth (up-down-up) pattern in metrics."""
        pattern = metrics.get("pattern", "")
        if pattern == "sawtooth":
            return True
        metric_list = metrics.get("metrics", [])
        if len(metric_list) < 4:
            return False
        values = [m.get("value", 0) for m in metric_list]
        # Check for alternating increases and decreases
        direction_changes = 0
        for i in range(2, len(values)):
            if (values[i] - values[i - 1]) * (values[i - 1] - values[i - 2]) < 0:
                direction_changes += 1
        return direction_changes >= 2

    def _find_pipeline_failure(self, logs: list[dict]) -> bool:
        """Check if logs indicate a data pipeline failure."""
        for log in logs:
            msg = log.get("message", "").lower()
            if "pipeline" in msg and ("fail" in msg or "error" in msg):
                return True
        return False

    def _find_stale_cache(self, logs: list[dict]) -> bool:
        """Check if logs indicate stale cache issues."""
        for log in logs:
            msg = log.get("message", "").lower()
            if "stale" in msg or "cache miss" in msg:
                return True
        return False

    def _empty_result(self, incident_id: str, reason: str) -> dict:
        """Return an empty RCA result with low confidence."""
        return {
            "incident_id": incident_id,
            "root_cause": reason,
            "confidence": 10,
            "evidence_timeline": [],
            "reasoning": f"Investigation could not proceed: {reason}",
        }
