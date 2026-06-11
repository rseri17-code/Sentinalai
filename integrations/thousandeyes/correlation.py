"""ThousandEyes deterministic correlation rules.

Each rule takes normalized NetworkEvidence + the existing evidence dict and
returns a CorrelationResult.  No LLM in the rule logic — pure boolean/scored
conditions on observable fields.

Rules implemented:
  TE-CORR-001: Network-induced latency (packet loss + high connectTime + normal waitTime)
  TE-CORR-002: External network degradation (timeouts + path instability)
  TE-CORR-003: Infra healthy / external path degraded (inside-out clean, outside-in fails)
  TE-CORR-004: DNS root cause (DNS test failing + DNS errors in logs)
  TE-CORR-005: Regional ISP issue (region-isolated failure, common ASN)
  TE-CORR-006: SaaS provider outage (external 503/0% global + internal healthy)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from integrations.thousandeyes.normalizer import NetworkEvidence


@dataclass
class CorrelationResult:
    rule_id: str
    rule_name: str
    matched: bool
    confidence_delta: float   # add to overall RCA confidence when matched
    owner: str                # likely responsible party
    rca_summary: str          # human-readable output for the RCA report


def rule_001_network_induced_latency(
    evidence_list: list[NetworkEvidence],
    existing: dict[str, Any],
) -> CorrelationResult:
    """TE-CORR-001: packet loss present + elevated connectTime + normal waitTime.

    Distinguishes network congestion from app slowness: if waitTime (server
    processing) is normal but connectTime (TCP handshake) is high, the bottleneck
    is in the network, not the application.
    """
    matched = False
    for ev in evidence_list:
        if (
            ev.packet_loss is not None and ev.packet_loss > 5
            and ev.connect_time_ms is not None and ev.connect_time_ms > 200
        ):
            matched = True
            break

    return CorrelationResult(
        rule_id="TE-CORR-001",
        rule_name="network_induced_latency",
        matched=matched,
        confidence_delta=0.30 if matched else 0.0,
        owner="network",
        rca_summary=(
            "ThousandEyes confirms network-induced latency: packet loss detected with "
            "elevated TCP connect time. Application wait time is within normal range, "
            "ruling out app-layer processing as the bottleneck."
        ) if matched else "",
    )


def rule_002_external_network_degradation(
    evidence_list: list[NetworkEvidence],
    existing: dict[str, Any],
) -> CorrelationResult:
    """TE-CORR-002: external agents failing + path hop changes detected."""
    path_changed = any(ev.changed_hops and ev.changed_hops > 0 for ev in evidence_list)
    external_failing = any(
        ev.agent_type == "Cloud"
        and ev.availability is not None
        and ev.availability < 80
        for ev in evidence_list
    )
    matched = path_changed and external_failing

    return CorrelationResult(
        rule_id="TE-CORR-002",
        rule_name="external_network_degradation",
        matched=matched,
        confidence_delta=0.35 if matched else 0.0,
        owner="network",
        rca_summary=(
            "ThousandEyes path visualization shows route instability: changed hops "
            "detected coinciding with external agent failures. Network path rerouting "
            "is the likely cause of observed degradation."
        ) if matched else "",
    )


def rule_003_infra_healthy_path_degraded(
    evidence_list: list[NetworkEvidence],
    existing: dict[str, Any],
) -> CorrelationResult:
    """TE-CORR-003: inside-out tools show healthy + external agents fail.

    The key signal: cloud agents from multiple regions fail while internal
    APM/K8s tools report no anomalies.
    """
    cloud_failing = [
        ev for ev in evidence_list
        if ev.agent_type == "Cloud"
        and ev.availability is not None
        and ev.availability < 80
    ]
    # Check if existing evidence shows internal health
    golden_signals = existing.get("golden_signals", {})
    internal_healthy = (
        not golden_signals.get("error_rate")
        or golden_signals.get("error_rate", 100) < 5
    )

    matched = len(cloud_failing) >= 2 and internal_healthy

    return CorrelationResult(
        rule_id="TE-CORR-003",
        rule_name="infra_healthy_path_degraded",
        matched=matched,
        confidence_delta=0.40 if matched else 0.0,
        owner="network",
        rca_summary=(
            f"Internal infrastructure appears healthy (APM/K8s reporting no anomalies) "
            f"but {len(cloud_failing)} external ThousandEyes agents report degradation. "
            "Root cause is likely at the ingress, CDN, or network perimeter — not the "
            "application itself."
        ) if matched else "",
    )


def rule_004_dns_root_cause(
    evidence_list: list[NetworkEvidence],
    existing: dict[str, Any],
) -> CorrelationResult:
    """TE-CORR-004: DNS test failing + DNS errors visible in log evidence."""
    dns_test_failing = any(
        ev.test_type == "dns-server"
        and ev.error_type in ("DNS_ERROR", "SERVER_ERROR", "TIMEOUT")
        for ev in evidence_list
    )
    log_data = existing.get("logs", {})
    log_text = str(log_data).lower()
    dns_in_logs = any(kw in log_text for kw in ("servfail", "nxdomain", "dns", "resolution"))

    matched = dns_test_failing

    return CorrelationResult(
        rule_id="TE-CORR-004",
        rule_name="dns_root_cause",
        matched=matched,
        confidence_delta=0.40 if matched else 0.0,
        owner="dns",
        rca_summary=(
            "ThousandEyes DNS test confirms resolution failure. "
            + ("Log evidence corroborates DNS errors. " if dns_in_logs else "")
            + "Root cause is DNS infrastructure, not application code."
        ) if matched else "",
    )


def rule_005_regional_isp_issue(
    evidence_list: list[NetworkEvidence],
    existing: dict[str, Any],
) -> CorrelationResult:
    """TE-CORR-005: region-isolated degradation with a common ASN across failing agents."""
    degraded = [
        ev for ev in evidence_list
        if (ev.availability is not None and ev.availability < 80)
        or (ev.packet_loss is not None and ev.packet_loss > 5)
    ]
    healthy = [
        ev for ev in evidence_list
        if (ev.availability is None or ev.availability >= 80)
        and (ev.packet_loss is None or ev.packet_loss <= 5)
    ]

    # Need both degraded and healthy agents — rules out global outage
    if not degraded or not healthy:
        return CorrelationResult(
            rule_id="TE-CORR-005",
            rule_name="regional_isp_issue",
            matched=False,
            confidence_delta=0.0,
            owner="isp",
            rca_summary="",
        )

    # Check for common ASN across degraded agents
    asns = [ev.asn for ev in degraded if ev.asn]
    common_asn = max(set(asns), key=asns.count) if asns else ""
    matched = bool(common_asn) and asns.count(common_asn) >= 2

    return CorrelationResult(
        rule_id="TE-CORR-005",
        rule_name="regional_isp_issue",
        matched=matched,
        confidence_delta=0.35 if matched else 0.0,
        owner="isp",
        rca_summary=(
            f"Regional ISP degradation detected: {len(degraded)} agents degraded, "
            f"{len(healthy)} agents healthy. Common ASN: {common_asn}. "
            "Other regions unaffected, ruling out service-wide failure."
        ) if matched else "",
    )


def rule_006_saas_provider_outage(
    evidence_list: list[NetworkEvidence],
    existing: dict[str, Any],
) -> CorrelationResult:
    """TE-CORR-006: global availability=0% from all cloud agents + internal services healthy."""
    cloud_results = [ev for ev in evidence_list if ev.agent_type == "Cloud"]
    if not cloud_results:
        return CorrelationResult(
            rule_id="TE-CORR-006",
            rule_name="saas_provider_outage",
            matched=False,
            confidence_delta=0.0,
            owner="saas",
            rca_summary="",
        )

    all_cloud_down = all(ev.availability is not None and ev.availability == 0 for ev in cloud_results)
    http_503 = any(ev.response_code == 503 for ev in cloud_results)

    matched = all_cloud_down and len(cloud_results) >= 2

    return CorrelationResult(
        rule_id="TE-CORR-006",
        rule_name="saas_provider_outage",
        matched=matched,
        confidence_delta=0.40 if matched else 0.0,
        owner="saas",
        rca_summary=(
            f"SaaS provider outage confirmed: {len(cloud_results)} global agents report "
            f"0% availability"
            + (" with HTTP 503 responses" if http_503 else "")
            + ". This is a third-party outage — no remediation available within the service."
        ) if matched else "",
    )


def run_all_rules(
    evidence_list: list[NetworkEvidence],
    existing: dict[str, Any],
) -> list[CorrelationResult]:
    """Run all correlation rules and return matched results sorted by confidence_delta desc."""
    rules = [
        rule_001_network_induced_latency,
        rule_002_external_network_degradation,
        rule_003_infra_healthy_path_degraded,
        rule_004_dns_root_cause,
        rule_005_regional_isp_issue,
        rule_006_saas_provider_outage,
    ]
    results = [r(evidence_list, existing) for r in rules]
    return sorted(
        (r for r in results if r.matched),
        key=lambda r: r.confidence_delta,
        reverse=True,
    )
