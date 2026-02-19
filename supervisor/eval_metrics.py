"""Deep eval metrics for SentinalAI agent investigations.

Emits OTEL metrics (counters, histograms, gauges) to the collector
which routes them to Splunk HEC for dashboard population.

When the OTEL SDK is not configured, all record_* calls are no-ops.

Metric naming follows OpenTelemetry semantic conventions:
  sentinalai.investigations.*   — decision quality
  sentinalai.hypotheses.*       — hypothesis evaluation
  sentinalai.worker_calls.*     — evidence gathering
  sentinalai.evidence.*         — data completeness
  sentinalai.budget.*           — execution budget
  sentinalai.circuit_breaker.*  — resilience
  sentinalai.confidence.*       — calibration
  sentinalai.investigation.*    — latency / phase timing
"""

from __future__ import annotations

import logging
from typing import Any

from supervisor.observability import get_meter

logger = logging.getLogger("sentinalai.eval")


# =========================================================================
# Lazy metric instrument creation (only when meter is available)
# =========================================================================

_instruments: dict[str, Any] = {}


def _get_or_create(name: str, factory, **kwargs):
    """Get a cached instrument or create one from the meter."""
    if name in _instruments:
        return _instruments[name]
    meter = get_meter()
    if meter is None:
        return None
    inst = factory(name, **kwargs)
    _instruments[name] = inst
    return inst


def _counter(name: str, **kwargs):
    meter = get_meter()
    if meter is None:
        return None
    return _get_or_create(name, meter.create_counter, **kwargs)


def _histogram(name: str, **kwargs):
    meter = get_meter()
    if meter is None:
        return None
    return _get_or_create(name, meter.create_histogram, **kwargs)


def _up_down_counter(name: str, **kwargs):
    meter = get_meter()
    if meter is None:
        return None
    return _get_or_create(name, meter.create_up_down_counter, **kwargs)


# =========================================================================
# Confidence bracket helper
# =========================================================================

def _confidence_bracket(confidence: int) -> str:
    if confidence <= 25:
        return "very_low"
    if confidence <= 50:
        return "low"
    if confidence <= 75:
        return "medium"
    if confidence <= 90:
        return "high"
    return "very_high"


# =========================================================================
# Public API — record_* functions called from agent.py and guardrails
# =========================================================================

def record_investigation(
    incident_id: str,
    incident_type: str,
    service: str,
    confidence: int,
    root_cause: str,
    tool_calls: int,
    evidence_sources: int,
    hypothesis_count: int,
    winner_hypothesis: str,
    elapsed_ms: float,
    status: str = "success",
    root_cause_keywords_matched: int = 0,
) -> None:
    """Record a completed investigation with all eval dimensions.

    This is the primary eval metric emitter — called once per investigation.
    """
    attrs = {
        "incident_type": incident_type,
        "service": service,
        "status": status,
        "confidence_bracket": _confidence_bracket(confidence),
    }

    # 1. Investigation counter (core eval metric)
    c = _counter(
        "sentinalai.investigations.total",
        description="Total investigations completed",
        unit="1",
    )
    if c is not None:
        c.add(1, attrs)

    # 2. Confidence distribution histogram
    h = _histogram(
        "sentinalai.confidence.distribution",
        description="Confidence score distribution across investigations",
        unit="1",
    )
    if h is not None:
        h.record(confidence, {"incident_type": incident_type, "service": service})

    # 3. Investigation duration histogram
    h = _histogram(
        "sentinalai.investigation.duration_ms",
        description="End-to-end investigation duration",
        unit="ms",
    )
    if h is not None:
        h.record(elapsed_ms, {"incident_type": incident_type, "service": service})

    # 4. Tool calls per investigation histogram
    h = _histogram(
        "sentinalai.investigation.tool_calls",
        description="Number of tool calls per investigation",
        unit="1",
    )
    if h is not None:
        h.record(tool_calls, {"incident_type": incident_type})

    # 5. Evidence sources per investigation histogram
    h = _histogram(
        "sentinalai.evidence.sources_count",
        description="Number of evidence sources available per investigation",
        unit="1",
    )
    if h is not None:
        h.record(evidence_sources, {"incident_type": incident_type})

    # 6. Hypothesis count per investigation
    h = _histogram(
        "sentinalai.hypotheses.count",
        description="Number of hypotheses generated per investigation",
        unit="1",
    )
    if h is not None:
        h.record(hypothesis_count, {"incident_type": incident_type})

    # 7. Winner hypothesis counter
    c = _counter(
        "sentinalai.hypotheses.winners",
        description="Count of investigations won by each hypothesis type",
        unit="1",
    )
    if c is not None:
        c.add(1, {
            "incident_type": incident_type,
            "winner_hypothesis": winner_hypothesis,
        })

    # 8. Keywords matched (accuracy proxy)
    if root_cause_keywords_matched > 0:
        h = _histogram(
            "sentinalai.root_cause.keywords_matched",
            description="Number of expected keywords found in root cause",
            unit="1",
        )
        if h is not None:
            h.record(root_cause_keywords_matched, {"incident_type": incident_type})

    logger.debug(
        "eval: investigation recorded",
        extra={
            "incident_id": incident_id,
            "incident_type": incident_type,
            "confidence": confidence,
            "tool_calls": tool_calls,
            "hypothesis_count": hypothesis_count,
            "winner": winner_hypothesis,
            "elapsed_ms": round(elapsed_ms, 1),
        },
    )


def record_worker_call(
    worker_name: str,
    action: str,
    status: str,
    elapsed_ms: float,
    incident_type: str = "",
) -> None:
    """Record a single worker/tool call with timing and status."""
    attrs = {
        "worker_name": worker_name,
        "action": action,
        "status": status,
    }
    if incident_type:
        attrs["incident_type"] = incident_type

    # Worker call counter
    c = _counter(
        "sentinalai.worker_calls.total",
        description="Total worker calls by worker, action, and status",
        unit="1",
    )
    if c is not None:
        c.add(1, attrs)

    # Worker call latency histogram
    h = _histogram(
        "sentinalai.worker_call.duration_ms",
        description="Worker call latency distribution",
        unit="ms",
    )
    if h is not None:
        h.record(elapsed_ms, {
            "worker_name": worker_name,
            "action": action,
            "status": status,
        })


def record_circuit_breaker_trip(
    worker_name: str,
    transition: str,
) -> None:
    """Record a circuit breaker state change.

    transition: "closed_to_open" | "open_to_half_open" | "half_open_to_closed"
    """
    c = _counter(
        "sentinalai.circuit_breaker.trips",
        description="Circuit breaker state transitions",
        unit="1",
    )
    if c is not None:
        c.add(1, {
            "worker_name": worker_name,
            "state_transition": transition,
        })


def record_budget_exhausted(
    incident_type: str,
    service: str,
    calls_made: int,
    max_calls: int,
) -> None:
    """Record when an investigation exhausts its execution budget."""
    c = _counter(
        "sentinalai.budget.exhausted",
        description="Investigations that exhausted their tool call budget",
        unit="1",
    )
    if c is not None:
        c.add(1, {"incident_type": incident_type, "service": service})

    h = _histogram(
        "sentinalai.budget.calls_at_exhaustion",
        description="Number of calls made when budget was exhausted",
        unit="1",
    )
    if h is not None:
        h.record(calls_made, {"incident_type": incident_type})


def record_evidence_completeness(
    incident_type: str,
    logs_available: bool,
    signals_available: bool,
    metrics_available: bool,
    events_available: bool,
    changes_available: bool,
) -> None:
    """Record which evidence sources were available for an investigation."""
    sources = {
        "logs": logs_available,
        "signals": signals_available,
        "metrics": metrics_available,
        "events": events_available,
        "changes": changes_available,
    }

    total_available = sum(1 for v in sources.values() if v)

    c = _counter(
        "sentinalai.evidence.completeness",
        description="Evidence source availability per investigation",
        unit="1",
    )
    if c is not None:
        for source_name, available in sources.items():
            c.add(1, {
                "incident_type": incident_type,
                "source": source_name,
                "available": str(available).lower(),
            })

    h = _histogram(
        "sentinalai.evidence.total_sources",
        description="Total evidence sources available per investigation",
        unit="1",
    )
    if h is not None:
        h.record(total_available, {"incident_type": incident_type})


def record_receipt_summary(
    incident_type: str,
    total_calls: int,
    succeeded: int,
    failed: int,
    total_elapsed_ms: float,
) -> None:
    """Record aggregated receipt statistics for an investigation."""
    h = _histogram(
        "sentinalai.receipts.total_calls",
        description="Total receipt-tracked calls per investigation",
        unit="1",
    )
    if h is not None:
        h.record(total_calls, {"incident_type": incident_type})

    if failed > 0:
        c = _counter(
            "sentinalai.receipts.failures",
            description="Total failed worker calls tracked by receipts",
            unit="1",
        )
        if c is not None:
            c.add(failed, {"incident_type": incident_type})

    h = _histogram(
        "sentinalai.receipts.total_elapsed_ms",
        description="Total time spent in worker calls per investigation",
        unit="ms",
    )
    if h is not None:
        h.record(total_elapsed_ms, {"incident_type": incident_type})


def record_eval_score(
    incident_id: str,
    incident_type: str,
    dimension: str,
    score: float,
) -> None:
    """Record an individual eval quality score (0.0-1.0).

    dimension examples: "root_cause_accuracy", "reasoning_quality",
                        "timeline_correctness", "causality_explained"
    """
    h = _histogram(
        "sentinalai.eval.score",
        description="Agent eval quality scores by dimension",
        unit="1",
    )
    if h is not None:
        h.record(score, {
            "incident_type": incident_type,
            "eval_dimension": dimension,
        })
