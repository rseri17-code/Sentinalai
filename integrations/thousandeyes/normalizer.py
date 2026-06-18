"""ThousandEyes evidence normalizer.

Converts raw ThousandEyes MCP responses into NetworkEvidence instances
with deterministic confidence scoring and owner inference.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


@dataclass
class NetworkEvidence:
    """Normalized evidence from one ThousandEyes test result (per agent/round)."""

    # Source identity
    source: str = "thousandeyes"
    test_id: str = ""
    test_name: str = ""
    test_type: str = ""
    target: str = ""

    # Agent identity
    agent_id: str = ""
    agent_location: str = ""
    agent_type: str = "Cloud"    # Cloud | Enterprise | Endpoint
    region: str = ""
    asn: str = ""
    provider: str = ""

    # Time window
    window_start: str = ""
    window_end: str = ""

    # Core metrics (None = not measured for this test type)
    availability: float | None = None      # 0–100 %
    packet_loss: float | None = None       # 0–100 %
    latency_ms: float | None = None
    jitter_ms: float | None = None
    dns_time_ms: float | None = None
    connect_time_ms: float | None = None
    ssl_time_ms: float | None = None
    response_time_ms: float | None = None
    response_code: int | None = None

    # Error classification
    error_type: str | None = None          # CONNECT | TIMEOUT | HTTP_ERROR | DNS_ERROR | etc.
    error_details: str | None = None

    # Path visualization
    path_hops: int | None = None
    changed_hops: int | None = None

    # BGP
    bgp_route_changed: bool = False

    # Derived
    affected_scope: str = "unknown"        # global | regional | local | unknown
    confidence: float = 0.0               # 0.0–1.0 how strongly this points to network root cause
    recommended_owner: str = "unknown"    # network | isp | dns | saas | cdn | endpoint | app | unknown

    evidence_id: str = field(default_factory=lambda: f"te-{uuid.uuid4().hex[:12]}")
    recorded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Error types that strongly indicate network/external failure (not app failure)
_HIGH_CONFIDENCE_ERRORS = frozenset({"CONNECT", "TIMEOUT", "DNS_ERROR", "SERVER_ERROR"})


def compute_network_confidence(ev: NetworkEvidence) -> float:
    """Deterministic confidence score: 0.0–1.0 indicating network root cause likelihood.

    Higher score = more evidence this is a network problem, not an app problem.
    No LLM involved — pure observable fields.
    """
    score = 0.0

    if ev.availability is not None and ev.availability == 0.0:
        score += 0.40
    elif ev.availability is not None and ev.availability < 50.0:
        score += 0.20

    if ev.packet_loss is not None and ev.packet_loss > 20:
        score += 0.20
    elif ev.packet_loss is not None and ev.packet_loss > 5:
        score += 0.10

    if ev.error_type in _HIGH_CONFIDENCE_ERRORS:
        score += 0.15

    if ev.connect_time_ms is not None and ev.connect_time_ms > 500:
        score += 0.10

    if ev.changed_hops is not None and ev.changed_hops > 0:
        score += 0.10

    if ev.bgp_route_changed:
        score += 0.15

    if ev.dns_time_ms is not None and ev.dns_time_ms > 500:
        score += 0.10

    # Scope multiplier: global failures are stronger network signal
    if ev.affected_scope == "global":
        score *= 1.2
    elif ev.affected_scope == "regional":
        score *= 1.1

    return min(score, 1.0)


def infer_owner(ev: NetworkEvidence) -> str:
    """Deterministically infer the likely responsible party from evidence."""
    if ev.error_type in ("DNS_ERROR", "SERVER_ERROR") and ev.test_type == "dns-server":
        return "dns"

    if ev.response_code == 503 and ev.affected_scope == "global" and ev.agent_type == "Cloud":
        return "saas"

    if ev.agent_type in ("Enterprise", "Endpoint") and ev.availability is not None and ev.availability < 50:
        return "endpoint"

    if ev.changed_hops and ev.changed_hops > 0:
        return "isp"

    if ev.packet_loss is not None and ev.packet_loss > 5:
        return "isp"

    if ev.connect_time_ms is not None and ev.connect_time_ms > 500:
        return "network"

    if ev.bgp_route_changed:
        return "carrier"

    return "unknown"


def normalize_alert(raw_alert: dict) -> NetworkEvidence:
    """Normalize a single ThousandEyes alert dict → NetworkEvidence."""
    ev = NetworkEvidence(
        test_id=str(raw_alert.get("testId", "")),
        test_name=raw_alert.get("testName", ""),
        test_type=raw_alert.get("type", ""),
        window_start=raw_alert.get("dateStart", ""),
        window_end=raw_alert.get("dateEnd", "") or "",
        error_type="TE_ALERT",
        error_details=(
            f"{raw_alert.get('alertRule', {}).get('alertRuleName', '')} "
            f"[{raw_alert.get('severity', '')}]"
        ).strip(),
        availability=_agent_avg(raw_alert.get("agents", []), "availability"),
    )
    ev.affected_scope = _infer_scope_from_agent_count(len(raw_alert.get("agents", [])))
    ev.confidence = compute_network_confidence(ev)
    ev.recommended_owner = infer_owner(ev)
    return ev


def normalize_test_result(raw_result: dict, test_type: str = "") -> NetworkEvidence:
    """Normalize a single per-agent test result → NetworkEvidence."""
    ev = NetworkEvidence(
        test_id=str(raw_result.get("testId", "")),
        test_name=raw_result.get("testName", ""),
        test_type=test_type or raw_result.get("type", ""),
        agent_id=str(raw_result.get("agentId", "")),
        agent_location=raw_result.get("agentName", ""),
        agent_type=raw_result.get("agentType", "Cloud"),
        availability=_float(raw_result.get("availability")),
        packet_loss=_float(raw_result.get("loss")),
        latency_ms=_float(raw_result.get("latency") or raw_result.get("responseTime")),
        jitter_ms=_float(raw_result.get("jitter")),
        dns_time_ms=_float(
            raw_result["resolutionTime"] if raw_result.get("resolutionTime") is not None
            else raw_result.get("dnsTime")
        ),
        connect_time_ms=_float(raw_result.get("connectTime")),
        ssl_time_ms=_float(raw_result.get("sslTime")),
        response_time_ms=_float(raw_result.get("responseTime")),
        response_code=raw_result.get("responseCode"),
        error_type=raw_result.get("errorType"),
        error_details=raw_result.get("errorDetails"),
    )
    ev.affected_scope = "local"   # single-agent result; caller aggregates scope
    ev.confidence = compute_network_confidence(ev)
    ev.recommended_owner = infer_owner(ev)
    return ev


def aggregate_scope(results: list[NetworkEvidence]) -> str:
    """Infer affected scope from a list of per-agent results."""
    if not results:
        return "unknown"
    degraded = [
        r for r in results
        if (r.availability is not None and r.availability < 80)
        or (r.packet_loss is not None and r.packet_loss > 5)
    ]
    cloud_degraded = [r for r in degraded if r.agent_type == "Cloud"]
    if len(cloud_degraded) >= 3:
        return "global"
    if len(cloud_degraded) >= 2:
        return "regional"
    if degraded:
        return "local"
    return "unknown"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _float(val: Any) -> float | None:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _agent_avg(agents: list[dict], metric: str) -> float | None:
    vals = [a[metric] for a in agents if metric in a and a[metric] is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _infer_scope_from_agent_count(count: int) -> str:
    if count >= 3:
        return "global"
    if count == 2:
        return "regional"
    return "local"
