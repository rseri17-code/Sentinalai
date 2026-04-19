"""AG UI Synthetic Incident Generator.

Generates realistic synthetic investigation event streams for:
1. UI development and testing
2. Replay validation tests
3. Demo environments
4. Evaluation harness

Produces deterministic, realistic event sequences that:
- Cover all 7 UI panels
- Test all event types
- Simulate parallel tool execution
- Include hypothesis scoring + LLM refinement
- Include memory matches
- Optionally include control gates
- Include circuit breaker and budget warnings
"""
from __future__ import annotations

import random
import time
import uuid
from typing import Optional

from agui.schemas.events import AGUIEvent, EventType
from agui.schemas.incidents import IncidentState, InvestigationStatus, MemoryMatch, HypothesisSummary


INCIDENT_SCENARIOS = {
    "error_spike": {
        "summary": "Error rate spike on payments-service: 42% 5xx in 5 minutes",
        "service": "payments-service",
        "severity": "critical",
        "playbook": ["LogWorker", "MetricsWorker", "ApmWorker", "ItsmWorker", "DevopsWorker"],
        "root_cause": "Downstream database connection pool exhausted after failed config update",
        "confidence": 0.87,
        "hypotheses": [
            ("Database Connection Exhaustion", 0.87),
            ("Code Deployment Regression", 0.62),
            ("Traffic Surge", 0.31),
        ],
    },
    "oomkill": {
        "summary": "OOMKill on order-processor pods (3/5 killed in 10 min)",
        "service": "order-processor",
        "severity": "major",
        "playbook": ["MetricsWorker", "LogWorker", "ItsmWorker", "KnowledgeWorker"],
        "root_cause": "Memory leak in order-processor v2.4.1 introduced in last deploy",
        "confidence": 0.91,
        "hypotheses": [
            ("Memory Leak in Application Code", 0.91),
            ("Insufficient Memory Limits", 0.55),
            ("Memory Pressure from Sibling Pods", 0.28),
        ],
    },
    "latency": {
        "summary": "P99 latency on api-gateway > 3000ms (SLA: 500ms)",
        "service": "api-gateway",
        "severity": "major",
        "playbook": ["ApmWorker", "MetricsWorker", "LogWorker", "ItsmWorker"],
        "root_cause": "Cascading slow queries from misconfigured cache invalidation",
        "confidence": 0.79,
        "hypotheses": [
            ("Cache Miss Storm", 0.79),
            ("Database Slow Query", 0.65),
            ("Network Congestion", 0.41),
        ],
    },
    "timeout": {
        "summary": "Connection timeouts on user-service → auth-service",
        "service": "user-service",
        "severity": "critical",
        "playbook": ["LogWorker", "ApmWorker", "MetricsWorker", "ItsmWorker", "KnowledgeWorker"],
        "root_cause": "auth-service TLS certificate expired causing handshake failures",
        "confidence": 0.94,
        "hypotheses": [
            ("TLS Certificate Expiry", 0.94),
            ("Service Overload", 0.43),
            ("Network Partition", 0.22),
        ],
    },
}

MEMORY_MATCHES_TEMPLATE = [
    {
        "incident_id": "INC-2024-001",
        "summary": "Similar error spike on checkout-service 6 months ago",
        "service": "{service}",
        "similarity_score": 0.89,
        "root_cause": "Database connection pool exhaustion",
        "resolution": "Increased pool size from 20 to 50, added connection timeout",
        "occurred_at": "2024-09-15T14:23:00Z",
        "source": "ltm",
    },
    {
        "incident_id": "INC-2024-047",
        "summary": "Deployment regression causing 5xx errors",
        "service": "{service}",
        "similarity_score": 0.71,
        "root_cause": "Misconfigured feature flag in v2.3.0",
        "resolution": "Rolled back to v2.2.9, re-deployed with fix in v2.3.1",
        "occurred_at": "2024-11-02T09:15:00Z",
        "source": "ltm",
    },
    {
        "incident_id": "INC-2025-012",
        "summary": "High error rate during traffic spike",
        "service": "{service}",
        "similarity_score": 0.58,
        "root_cause": "Autoscaling lag during sudden load increase",
        "resolution": "Adjusted HPA target utilization from 80% to 60%",
        "occurred_at": "2025-01-18T16:45:00Z",
        "source": "knowledge_graph",
    },
]

JUDGE_SCORES_TEMPLATE = {
    "root_cause_accuracy": 0.85,
    "causal_reasoning": 0.82,
    "evidence_usage": 0.91,
    "timeline_quality": 0.78,
    "actionability": 0.88,
    "overall": 0.85,
}


class SyntheticIncidentGenerator:
    """Generates synthetic investigation event streams."""

    def __init__(self, seed: Optional[int] = None) -> None:
        self._rng = random.Random(seed)

    async def generate_investigation(
        self,
        incident_type: str = "error_spike",
        include_control_gate: bool = False,
        include_circuit_breaker: bool = False,
        include_budget_warning: bool = False,
        investigation_id: Optional[str] = None,
    ) -> tuple[str, list[AGUIEvent]]:
        """
        Generate a complete synthetic investigation event stream.

        Returns: (investigation_id, ordered_events)
        """
        scenario = INCIDENT_SCENARIOS.get(
            incident_type, INCIDENT_SCENARIOS["error_spike"]
        )
        inv_id = investigation_id or str(uuid.uuid4())
        incident_id = f"INC-{self._rng.randint(1000, 9999)}"
        trace_id = f"1-{int(time.time()):08x}-{uuid.uuid4().hex[:24]}"

        events: list[AGUIEvent] = []
        seq = 0
        base_ts = int(time.time() * 1000)

        def make_ts(offset_ms: int) -> str:
            ts_ms = base_ts + offset_ms
            return time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(ts_ms / 1000)
            )

        def next_event(event_type: EventType, payload: dict, offset_ms: int) -> AGUIEvent:
            nonlocal seq
            e = AGUIEvent(
                event_type=event_type,
                investigation_id=inv_id,
                incident_id=incident_id,
                trace_id=trace_id,
                span_id=uuid.uuid4().hex[:16],
                sequence_num=seq,
                timestamp=make_ts(offset_ms),
                timestamp_epoch_ms=base_ts + offset_ms,
                payload=payload,
            )
            seq += 1
            return e

        # === Phase 1: Investigation Start ===
        events.append(next_event(EventType.INVESTIGATION_STARTED, {
            "summary": scenario["summary"],
            "severity": scenario["severity"],
            "source": "moogsoft",
            "started_at": make_ts(0),
        }, offset_ms=0))

        # === Phase 2: Incident Fetch ===
        receipt_ops = str(uuid.uuid4())
        events.append(next_event(EventType.TOOL_CALLED, {
            "worker": "OpsWorker",
            "action": "get_incident_by_id",
            "params": {"incident_id": incident_id},
            "receipt_id": receipt_ops,
        }, offset_ms=50))
        events.append(next_event(EventType.TOOL_RESPONDED, {
            "worker": "OpsWorker",
            "action": "get_incident_by_id",
            "receipt_id": receipt_ops,
            "elapsed_ms": 312.5,
            "result_count": 1,
            "status": "success",
        }, offset_ms=412))

        # === Phase 3: Classification ===
        events.append(next_event(EventType.INCIDENT_CLASSIFIED, {
            "incident_type": incident_type,
            "confidence": 0.95,
            "keywords_matched": [incident_type, scenario["service"].split("-")[0]],
        }, offset_ms=500))

        events.append(next_event(EventType.PLAYBOOK_SELECTED, {
            "incident_type": incident_type,
            "playbook": scenario["playbook"],
        }, offset_ms=550))

        # === Phase 4: Parallel Worker Execution ===
        offset = 600
        worker_receipts = {}
        for i, worker in enumerate(scenario["playbook"]):
            action = self._worker_action(worker)
            receipt_id = str(uuid.uuid4())
            worker_receipts[worker] = receipt_id
            events.append(next_event(EventType.TOOL_CALLED, {
                "worker": worker,
                "action": action,
                "params": {"service": scenario["service"], "time_range": "30m"},
                "receipt_id": receipt_id,
            }, offset_ms=offset + i * 20))

        # Workers respond with variable latency
        for i, worker in enumerate(scenario["playbook"]):
            receipt_id = worker_receipts[worker]
            elapsed = self._rng.uniform(200, 1800)
            failed = include_circuit_breaker and i == 1  # Second worker fails
            events.append(next_event(
                EventType.TOOL_RESPONDED if not failed else EventType.TOOL_FAILED,
                {
                    "worker": worker,
                    "action": self._worker_action(worker),
                    "receipt_id": receipt_id,
                    "elapsed_ms": elapsed,
                    "result_count": 0 if failed else self._rng.randint(5, 200),
                    "status": "error" if failed else "success",
                    "error": "Circuit breaker tripped" if failed else None,
                    "output_summary": None if failed else self._worker_summary(worker, scenario),
                },
                offset_ms=offset + 500 + i * 300
            ))

        if include_circuit_breaker and len(scenario["playbook"]) > 1:
            events.append(next_event(EventType.CIRCUIT_BREAKER_TRIPPED, {
                "worker": scenario["playbook"][1],
                "failure_count": 3,
            }, offset_ms=offset + 800))

        # === Phase 5: Memory Query ===
        events.append(next_event(EventType.MEMORY_QUERIED, {
            "query": f"{incident_type} {scenario['service']} similar incidents",
            "store_type": "ltm",
        }, offset_ms=offset + 2500))

        events.append(next_event(EventType.MEMORY_RESULT, {
            "matches_count": 3,
            "top_similarity": 0.89,
        }, offset_ms=offset + 2800))

        # === Phase 6: Budget Warning (optional) ===
        if include_budget_warning:
            events.append(next_event(EventType.BUDGET_WARNING, {
                "calls_used": 16,
                "calls_max": 20,
            }, offset_ms=offset + 3000))

        # === Phase 7: Hypothesis Scoring ===
        hypotheses = [
            {"name": h[0], "score": h[1], "root_cause": scenario["root_cause"] if i == 0 else f"Possible: {h[0]}"}
            for i, h in enumerate(scenario["hypotheses"])
        ]

        events.append(next_event(EventType.HYPOTHESIS_GENERATED, {
            "hypotheses": hypotheses,
            "count": len(hypotheses),
        }, offset_ms=offset + 3200))

        events.append(next_event(EventType.HYPOTHESIS_SCORED, {
            "hypotheses": hypotheses,
            "winner": scenario["hypotheses"][0][0],
            "confidence": scenario["confidence"],
        }, offset_ms=offset + 3500))

        # === Phase 8: Optional Control Gate ===
        control_seq = seq
        if include_control_gate:
            events.append(next_event(EventType.CONTROL_REQUESTED, {
                "action": "approve",
                "reason": "High-confidence action requires approval before remediation",
                "confidence": scenario["confidence"],
            }, offset_ms=offset + 3700))
            # Simulate approval after delay
            events.append(next_event(EventType.CONTROL_APPROVED, {
                "action": "approve",
                "actor_id": "ops-approver",
                "actor_role": "approver",
                "node_id": f"ctrl_{control_seq}",
            }, offset_ms=offset + 8000))

        # === Phase 9: LLM Refinement ===
        events.append(next_event(EventType.LLM_INVOKED, {
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "purpose": "hypothesis_refinement",
            "input_tokens": 1847,
        }, offset_ms=offset + 4000))

        events.append(next_event(EventType.LLM_RESPONDED, {
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "total_tokens": 2341,
            "elapsed_ms": 2100.0,
        }, offset_ms=offset + 6100))

        # === Phase 10: Hypothesis Selected ===
        events.append(next_event(EventType.HYPOTHESIS_SELECTED, {
            "winner": scenario["hypotheses"][0][0],
            "confidence": scenario["confidence"],
            "root_cause": scenario["root_cause"],
        }, offset_ms=offset + 6200))

        # === Phase 11: RCA Generated ===
        events.append(next_event(EventType.RCA_GENERATED, {
            "root_cause": scenario["root_cause"],
            "confidence": scenario["confidence"],
            "remediation": [
                f"Rollback {scenario['service']} to previous version",
                "Scale out affected service",
                "Apply emergency patch",
            ],
            "judge_scores": JUDGE_SCORES_TEMPLATE,
        }, offset_ms=offset + 6500))

        # === Phase 12: Investigation Complete ===
        total_duration = offset + 7000
        events.append(next_event(EventType.INVESTIGATION_COMPLETED, {
            "duration_ms": total_duration,
            "tool_calls_total": len(scenario["playbook"]) + 1,
            "confidence": scenario["confidence"],
        }, offset_ms=total_duration))

        # Build state for state store
        state = self._build_state(
            inv_id, incident_id, trace_id, scenario, incident_type, events
        )

        # Store state
        try:
            from agui.state_store import get_state_store
            store = get_state_store()
            await store.put_state(state)
            for event in events:
                await store.put_event(event)
        except Exception:
            pass

        return inv_id, events

    def _worker_action(self, worker: str) -> str:
        actions = {
            "OpsWorker": "get_incident_by_id",
            "LogWorker": "search_logs",
            "MetricsWorker": "query_metrics",
            "ApmWorker": "get_golden_signals",
            "ItsmWorker": "get_ci_details",
            "DevopsWorker": "get_recent_deployments",
            "KnowledgeWorker": "search_similar",
            "ConfluenceWorker": "get_runbook",
        }
        return actions.get(worker, "execute")

    def _worker_summary(self, worker: str, scenario: dict) -> str:
        summaries = {
            "LogWorker": f"Found 1,247 ERROR logs in last 30min on {scenario['service']}",
            "MetricsWorker": "CPU 94%, Memory 87%, Connections 1,200/1,200 (100% saturated)",
            "ApmWorker": "P99 latency 3,847ms, Error rate 42.3%, Throughput -67%",
            "ItsmWorker": "2 related change records found, 1 open known error",
            "DevopsWorker": "Deploy v2.4.1 at 14:23 UTC, 847 changed files",
            "KnowledgeWorker": "3 similar incidents found, top similarity 0.89",
            "ConfluenceWorker": "Runbook: 'Database Connection Pool Troubleshooting' found",
        }
        return summaries.get(worker, f"{worker} returned data")

    def _build_state(
        self,
        inv_id: str,
        incident_id: str,
        trace_id: str,
        scenario: dict,
        incident_type: str,
        events: list[AGUIEvent],
    ) -> IncidentState:
        matches = [
            MemoryMatch(
                incident_id=m["incident_id"],
                summary=m["summary"],
                service=scenario["service"],
                similarity_score=m["similarity_score"],
                root_cause=m.get("root_cause"),
                resolution=m.get("resolution"),
                occurred_at=m.get("occurred_at"),
                source=m.get("source", "ltm"),
            )
            for m in MEMORY_MATCHES_TEMPLATE
        ]

        hypotheses = [
            HypothesisSummary(
                name=h[0],
                root_cause=scenario["root_cause"] if i == 0 else f"Possible: {h[0]}",
                score=h[1],
                is_winner=(i == 0),
                evidence_refs=[str(uuid.uuid4())[:8] for _ in range(3)],
            )
            for i, h in enumerate(scenario["hypotheses"])
        ]

        return IncidentState(
            investigation_id=inv_id,
            incident_id=incident_id,
            trace_id=trace_id,
            summary=scenario["summary"],
            affected_service=scenario["service"],
            severity=scenario["severity"],
            incident_type=incident_type,
            source="moogsoft",
            status=InvestigationStatus.COMPLETED,
            playbook=scenario["playbook"],
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            duration_ms=7000.0,
            root_cause=scenario["root_cause"],
            confidence=scenario["confidence"],
            risk_level="high" if scenario["confidence"] > 0.8 else "medium",
            hypotheses=hypotheses,
            winner_hypothesis=scenario["hypotheses"][0][0],
            tool_calls_total=len(scenario["playbook"]) + 1,
            tool_calls_success=len(scenario["playbook"]),
            tool_calls_failed=0,
            memory_matches=matches,
            judge_scores=JUDGE_SCORES_TEMPLATE,
            replay_available=True,
            event_count=len(events),
            budget_used=len(scenario["playbook"]) + 3,
            budget_max=20,
            data_freshness={w: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) for w in scenario["playbook"]},
            stale_sources=[],
        )

    async def generate_all_scenarios(self) -> list[tuple[str, list[AGUIEvent]]]:
        """Generate one investigation for each incident type."""
        results = []
        for incident_type in INCIDENT_SCENARIOS:
            result = await self.generate_investigation(incident_type)
            results.append(result)
        return results
