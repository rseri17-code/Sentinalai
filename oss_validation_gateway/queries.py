"""Translate worker query hints into LogQL / PromQL.

Workers pass Splunk-ish strings (``timeout payment-service``) and metric
hints (``response_time_ms``). The shim maps those onto the demo seed
metric names and a conservative Loki selector.
"""

from __future__ import annotations

import re

# Seed exporter metric names (see deploy/oss-validation/seed/seed.py).
METRIC_PROMQL: dict[str, str] = {
    "response_time_ms": 'demo_latency_p95_ms{{service="{service}"}}',
    "memory_usage_bytes": 'process_resident_memory_bytes{{service="{service}"}}',
    "cpu_usage_percent": 'demo_saturation_pct{{service="{service}"}}',
    "error_rate": 'demo_error_rate{{service="{service}"}}',
    "request_rate": 'demo_request_rate{{service="{service}"}}',
}

GOLDEN_SIGNAL_PROMQL: dict[str, str] = {
    "latency_p95": 'demo_latency_p95_ms{{service="{service}"}}',
    "latency_baseline_p95": 'demo_latency_baseline_p95_ms{{service="{service}"}}',
    "latency_p50": 'demo_latency_p50_ms{{service="{service}"}}',
    "latency_p99": 'demo_latency_p99_ms{{service="{service}"}}',
    "error_rate": 'demo_error_rate{{service="{service}"}}',
    "request_rate": 'demo_request_rate{{service="{service}"}}',
    "saturation": 'demo_saturation_pct{{service="{service}"}}',
}

_IDENT = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _safe_service(service: str | None) -> str:
    value = (service or "payment-service").strip() or "payment-service"
    if not _IDENT.match(value):
        return "payment-service"
    return value


def _quote_logql(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def splunk_query_to_logql(query: str, service: str | None = None) -> str:
    """Map a playbook ``query_hint`` to a Loki LogQL selector.

    ``timeout {service}`` / ``error payment-service`` become
    ``{service="payment-service"} |= "timeout"``. Unknown punctuation is
    stripped; an empty query lists the service stream.
    """
    svc = _safe_service(service)
    raw = (query or "").strip()
    # Drop the service token if the playbook already interpolated it.
    tokens = [t for t in re.split(r"\s+", raw) if t and t != svc]
    # Keep alphanumerics; join remaining keywords for a contains filter.
    keywords: list[str] = []
    for token in tokens:
        cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "", token)
        if cleaned and cleaned.lower() not in {"or", "and", "not"}:
            keywords.append(cleaned)
    selector = '{service="%s"}' % _quote_logql(svc)
    if not keywords:
        return selector
    # First keyword is the strongest signal (timeout, error, OOMKilled, …).
    return '%s |= "%s"' % (selector, _quote_logql(keywords[0]))


def metric_hint_to_promql(metric: str | None, service: str | None = None) -> str:
    svc = _safe_service(service)
    key = (metric or "").strip() or "request_rate"
    template = METRIC_PROMQL.get(key, METRIC_PROMQL["request_rate"])
    return template.format(service=svc)


def golden_signal_promql(signal: str, service: str | None = None) -> str:
    svc = _safe_service(service)
    template = GOLDEN_SIGNAL_PROMQL.get(signal)
    if template is None:
        raise KeyError(signal)
    return template.format(service=svc)
