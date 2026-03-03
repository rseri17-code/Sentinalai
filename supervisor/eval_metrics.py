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

GenAI semantic conventions (https://opentelemetry.io/docs/specs/semconv/gen-ai/):
  gen_ai.client.token.usage     — input/output token counts per LLM call
  gen_ai.client.operation.duration — LLM call latency
  sentinalai.llm.*              — LLM-specific operational metrics
  sentinalai.judge.*            — LLM-as-judge eval scores
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from supervisor.observability import get_meter

logger = logging.getLogger("sentinalai.eval")


# =========================================================================
# Lazy metric instrument creation (only when meter is available)
# =========================================================================

_instruments: dict[str, Any] = {}
_instruments_lock = threading.Lock()


def _get_or_create(name: str, factory, **kwargs):
    """Get a cached instrument or create one from the meter (thread-safe)."""
    if name in _instruments:
        return _instruments[name]
    with _instruments_lock:
        # Double-check after acquiring lock
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


# =========================================================================
# GenAI Semantic Convention Metrics
# https://opentelemetry.io/docs/specs/semconv/gen-ai/
# =========================================================================

def record_llm_usage(
    operation: str,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: float,
    incident_type: str = "",
    status: str = "success",
) -> None:
    """Record LLM call metrics following GenAI semantic conventions.

    operation: "refine_hypothesis" | "generate_reasoning" | "judge"
    """
    attrs = {
        "gen_ai.system": "aws.bedrock",
        "gen_ai.request.model": model_id,
        "gen_ai.operation.name": operation,
        "status": status,
    }
    if incident_type:
        attrs["incident_type"] = incident_type

    # gen_ai.client.token.usage — input tokens
    h = _histogram(
        "gen_ai.client.token.usage",
        description="GenAI token usage per LLM call",
        unit="{token}",
    )
    if h is not None:
        h.record(input_tokens, {**attrs, "gen_ai.token.type": "input"})
        h.record(output_tokens, {**attrs, "gen_ai.token.type": "output"})

    # gen_ai.client.operation.duration — latency in seconds
    h = _histogram(
        "gen_ai.client.operation.duration",
        description="GenAI LLM call duration",
        unit="s",
    )
    if h is not None:
        h.record(latency_ms / 1000.0, attrs)

    # sentinalai.llm.calls — counter for total LLM calls
    c = _counter(
        "sentinalai.llm.calls.total",
        description="Total LLM calls by operation and model",
        unit="1",
    )
    if c is not None:
        c.add(1, attrs)

    # sentinalai.llm.tokens — cumulative token counter
    c = _counter(
        "sentinalai.llm.tokens.total",
        description="Cumulative LLM tokens consumed",
        unit="{token}",
    )
    if c is not None:
        c.add(input_tokens, {**attrs, "gen_ai.token.type": "input"})
        c.add(output_tokens, {**attrs, "gen_ai.token.type": "output"})

    # G7.2: Cost estimation metric based on token counts and model pricing
    estimated_cost = estimate_llm_cost(model_id, input_tokens, output_tokens)
    if estimated_cost > 0:
        h = _histogram(
            "sentinalai.investigation.estimated_cost",
            description="Estimated cost per LLM call in USD",
            unit="USD",
        )
        if h is not None:
            h.record(estimated_cost, attrs)

    logger.debug(
        "genai: op=%s model=%s in=%d out=%d latency=%.1fms cost=$%.6f",
        operation, model_id, input_tokens, output_tokens, latency_ms, estimated_cost,
    )


# =========================================================================
# G7.2: Cost estimation for FinOps dashboards
# =========================================================================

# Pricing per 1K tokens (approximate, in USD)
_MODEL_PRICING: dict[str, dict[str, float]] = {
    "anthropic.claude-3-5-sonnet": {"input": 0.003, "output": 0.015},
    "anthropic.claude-3-haiku": {"input": 0.00025, "output": 0.00125},
    "anthropic.claude-3-sonnet": {"input": 0.003, "output": 0.015},
    "anthropic.claude-3-opus": {"input": 0.015, "output": 0.075},
    "amazon.titan-text-premier": {"input": 0.0005, "output": 0.0015},
    "amazon.titan-text-lite": {"input": 0.00015, "output": 0.0002},
}

# Default pricing for unknown models
_DEFAULT_PRICING = {"input": 0.003, "output": 0.015}


def estimate_llm_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD for an LLM call based on token counts and model pricing."""
    pricing = _DEFAULT_PRICING
    for model_prefix, model_pricing in _MODEL_PRICING.items():
        if model_prefix in model_id:
            pricing = model_pricing
            break
    input_cost = (input_tokens / 1000.0) * pricing["input"]
    output_cost = (output_tokens / 1000.0) * pricing["output"]
    return round(input_cost + output_cost, 8)


def record_judge_scores(
    incident_id: str,
    incident_type: str,
    scores: dict[str, float],
    source: str = "llm_judge",
) -> None:
    """Record LLM-as-judge dimension scores as OTEL metrics."""
    for dimension, score in scores.items():
        h = _histogram(
            "sentinalai.judge.score",
            description="LLM-as-judge quality scores by dimension",
            unit="1",
        )
        if h is not None:
            h.record(score, {
                "incident_type": incident_type,
                "judge_dimension": dimension,
                "judge_source": source,
            })

    # Overall score as a separate metric for dashboards
    overall = scores.get("overall", 0)
    h = _histogram(
        "sentinalai.judge.overall",
        description="LLM-as-judge overall quality score",
        unit="1",
    )
    if h is not None:
        h.record(overall, {
            "incident_type": incident_type,
            "judge_source": source,
        })
