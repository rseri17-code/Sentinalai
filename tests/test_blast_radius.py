"""Tests for supervisor/blast_radius.py.

Covers:
  - All four risk tiers (LOW / MEDIUM / HIGH / CRITICAL)
  - Empty topology (isolated service)
  - P1 dependency detection and its effect on tier and precautions
  - Precaution generation (drain traffic, notify team, CB warning)
  - safe_to_auto_apply logic (True only for LOW + no P1 deps)
  - requires_human_approval logic (True for MEDIUM / HIGH / CRITICAL)
  - KG edge augmentation of topology
  - Traffic-percentage-based impact
  - Circuit-breaker impact reduction

All tests are standalone — no external I/O, no fixtures from disk.
"""
from __future__ import annotations

import pytest

from supervisor.blast_radius import (
    AffectedService,
    BlastRadiusReport,
    RiskTier,
    compute_blast_radius,
)


# ---------------------------------------------------------------------------
# Shared topology fixtures
# ---------------------------------------------------------------------------

def _isolated_topology() -> dict:
    """A single service with no callers and no dependencies."""
    return {
        "payment-service": {
            "tier": "P2",
            "dependencies": [],
            "callers": [],
            "has_circuit_breaker": False,
            "team": "payments-sre",
            "traffic_pct": 0.0,
        }
    }


def _simple_chain_topology() -> dict:
    """api-gateway → payment-service → payment-db (linear chain)."""
    return {
        "payment-db": {
            "tier": "P1",
            "dependencies": [],
            "callers": ["payment-service"],
            "has_circuit_breaker": False,
            "team": "db-sre",
            "traffic_pct": 0.0,
        },
        "payment-service": {
            "tier": "P2",
            "dependencies": ["payment-db"],
            "callers": ["api-gateway"],
            "has_circuit_breaker": False,
            "team": "payments-sre",
            "traffic_pct": 0.0,
        },
        "api-gateway": {
            "tier": "P1",
            "dependencies": ["payment-service"],
            "callers": [],
            "has_circuit_breaker": True,
            "team": "platform-sre",
            "traffic_pct": 0.0,
        },
    }


def _large_fanout_topology() -> dict:
    """payment-service is called by many P1 services — high impact."""
    base = {
        "payment-service": {
            "tier": "P2",
            "dependencies": [],
            "callers": [f"svc-{i}" for i in range(6)],
            "has_circuit_breaker": False,
            "team": "payments-sre",
            "traffic_pct": 0.0,
        }
    }
    for i in range(6):
        base[f"svc-{i}"] = {
            "tier": "P1",
            "dependencies": ["payment-service"],
            "callers": [],
            "has_circuit_breaker": False,
            "team": f"team-{i}",
            "traffic_pct": 0.0,
        }
    return base


def _circuit_breaker_topology() -> dict:
    """Downstream services all have circuit breakers."""
    return {
        "payment-service": {
            "tier": "P2",
            "dependencies": [],
            "callers": ["checkout", "orders"],
            "has_circuit_breaker": False,
            "team": "payments",
            "traffic_pct": 0.0,
        },
        "checkout": {
            "tier": "P2",
            "dependencies": ["payment-service"],
            "callers": [],
            "has_circuit_breaker": True,
            "team": "checkout-team",
            "traffic_pct": 0.0,
        },
        "orders": {
            "tier": "P2",
            "dependencies": ["payment-service"],
            "callers": [],
            "has_circuit_breaker": True,
            "team": "orders-team",
            "traffic_pct": 0.0,
        },
    }


# ---------------------------------------------------------------------------
# Risk tier tests
# ---------------------------------------------------------------------------

class TestRiskTiers:
    def test_low_risk_isolated_service(self):
        """An isolated P3 service with no callers → LOW risk."""
        topology = {
            "log-collector": {
                "tier": "P3",
                "dependencies": [],
                "callers": [],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            }
        }
        report = compute_blast_radius("log-collector", "restart", topology)
        assert report.risk_tier == RiskTier.LOW
        assert report.total_estimated_user_impact_pct < 5.0

    def test_medium_risk_single_p1_caller(self):
        """One P1 caller → at least MEDIUM risk."""
        topology = {
            "payment-service": {
                "tier": "P2",
                "dependencies": [],
                "callers": ["api-gateway"],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            },
            "api-gateway": {
                "tier": "P1",
                "dependencies": ["payment-service"],
                "callers": [],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            },
        }
        report = compute_blast_radius("payment-service", "restart", topology)
        assert report.risk_tier in (RiskTier.MEDIUM, RiskTier.HIGH, RiskTier.CRITICAL)

    def test_high_risk_two_p1_callers(self):
        """Two P1 callers → HIGH or CRITICAL."""
        topology = {
            "shared-cache": {
                "tier": "P2",
                "dependencies": [],
                "callers": ["svc-a", "svc-b"],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            },
            "svc-a": {
                "tier": "P1",
                "dependencies": ["shared-cache"],
                "callers": [],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            },
            "svc-b": {
                "tier": "P1",
                "dependencies": ["shared-cache"],
                "callers": [],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            },
        }
        report = compute_blast_radius("shared-cache", "restart", topology)
        assert report.risk_tier in (RiskTier.HIGH, RiskTier.CRITICAL)

    def test_critical_risk_large_fanout(self):
        """Six P1 callers → CRITICAL risk."""
        topology = _large_fanout_topology()
        report = compute_blast_radius("payment-service", "restart", topology)
        assert report.risk_tier == RiskTier.CRITICAL

    def test_critical_risk_via_high_traffic_pct(self):
        """A service with >50% traffic_pct should be CRITICAL even with no callers."""
        topology = {
            "main-api": {
                "tier": "P1",
                "dependencies": [],
                "callers": [],
                "has_circuit_breaker": False,
                "traffic_pct": 75.0,
            }
        }
        report = compute_blast_radius("main-api", "restart", topology)
        assert report.risk_tier == RiskTier.CRITICAL
        assert report.total_estimated_user_impact_pct > 50.0

    def test_medium_risk_from_traffic_pct(self):
        """A service with 10% traffic_pct → MEDIUM risk."""
        topology = {
            "user-service": {
                "tier": "P2",
                "dependencies": [],
                "callers": [],
                "has_circuit_breaker": False,
                "traffic_pct": 10.0,
            }
        }
        report = compute_blast_radius("user-service", "config_change", topology)
        assert report.risk_tier in (RiskTier.MEDIUM, RiskTier.HIGH)


# ---------------------------------------------------------------------------
# Empty / edge-case topology tests
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_topology(self):
        """Empty topology dict → LOW risk, no affected services."""
        report = compute_blast_radius("ghost-service", "restart", {})
        assert report.risk_tier == RiskTier.LOW
        assert report.affected_services == []
        assert report.total_estimated_user_impact_pct == 0.0
        assert "No downstream services" in report.reasoning or \
               report.total_estimated_user_impact_pct == 0.0

    def test_target_service_missing_from_topology(self):
        """Target not in topology → treat as isolated → LOW risk."""
        topology = {
            "other-svc": {
                "tier": "P1",
                "dependencies": [],
                "callers": [],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            }
        }
        report = compute_blast_radius("unknown-service", "restart", topology)
        assert report.risk_tier == RiskTier.LOW
        assert report.affected_services == []

    def test_isolated_service_with_dependencies_not_callers(self):
        """Service with dependencies but no callers → no downstream disruption."""
        topology = {
            "worker": {
                "tier": "P2",
                "dependencies": ["database"],
                "callers": [],   # nothing calls worker
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            },
            "database": {
                "tier": "P1",
                "dependencies": [],
                "callers": ["worker"],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            },
        }
        report = compute_blast_radius("worker", "restart", topology)
        # No callers of worker → no direct_downstream services affected
        direct = [s for s in report.affected_services if s.dependency_type == "direct_downstream"]
        assert direct == []


# ---------------------------------------------------------------------------
# P1 dependency detection tests
# ---------------------------------------------------------------------------

class TestP1Detection:
    def test_p1_caller_detected(self):
        """P1 caller should appear in affected_services with tier=P1."""
        topology = _simple_chain_topology()
        report = compute_blast_radius("payment-service", "restart", topology)
        p1_affected = [s for s in report.affected_services if s.tier == "P1"]
        assert len(p1_affected) >= 1
        p1_names = {s.name for s in p1_affected}
        assert "api-gateway" in p1_names

    def test_p1_caller_triggers_non_low_tier(self):
        """Any P1 caller means risk tier cannot be LOW."""
        topology = _simple_chain_topology()
        report = compute_blast_radius("payment-service", "restart", topology)
        assert report.risk_tier != RiskTier.LOW

    def test_no_p1_deps_allows_low_tier(self):
        """When all callers are P3, risk can remain LOW."""
        topology = {
            "batch-job": {
                "tier": "P3",
                "dependencies": [],
                "callers": ["reporting"],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            },
            "reporting": {
                "tier": "P3",
                "dependencies": ["batch-job"],
                "callers": [],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            },
        }
        report = compute_blast_radius("batch-job", "restart", topology)
        assert report.risk_tier == RiskTier.LOW
        p1_affected = [s for s in report.affected_services if s.tier == "P1"]
        assert p1_affected == []


# ---------------------------------------------------------------------------
# Precaution generation tests
# ---------------------------------------------------------------------------

class TestPrecautionGeneration:
    def test_restart_generates_drain_traffic_precaution(self):
        report = compute_blast_radius("payment-service", "restart", _simple_chain_topology())
        drain_precautions = [p for p in report.recommended_precautions if "Drain" in p]
        assert len(drain_precautions) >= 1
        assert "payment-service" in drain_precautions[0]

    def test_rollback_generates_drain_traffic_precaution(self):
        topology = _isolated_topology()
        report = compute_blast_radius("payment-service", "rollback", topology)
        assert any("Drain" in p for p in report.recommended_precautions)

    def test_traffic_shift_generates_maintenance_mode_precaution(self):
        topology = _isolated_topology()
        report = compute_blast_radius("payment-service", "traffic_shift", topology)
        assert any("maintenance mode" in p.lower() for p in report.recommended_precautions)

    def test_p1_dep_generates_notify_precaution(self):
        topology = _simple_chain_topology()
        report = compute_blast_radius("payment-service", "restart", topology)
        notify_precautions = [p for p in report.recommended_precautions if "P1" in p or "Notify" in p]
        assert len(notify_precautions) >= 1

    def test_team_notification_included(self):
        topology = _simple_chain_topology()
        report = compute_blast_radius("payment-service", "restart", topology)
        team_precautions = [p for p in report.recommended_precautions if "team" in p.lower() or "Team" in p]
        assert len(team_precautions) >= 1

    def test_high_risk_generates_change_ticket_precaution(self):
        topology = _large_fanout_topology()
        report = compute_blast_radius("payment-service", "restart", topology)
        assert report.risk_tier in (RiskTier.HIGH, RiskTier.CRITICAL)
        assert any("change" in p.lower() or "ticket" in p.lower() or "approval" in p.lower()
                   for p in report.recommended_precautions)

    def test_critical_risk_generates_maintenance_window_precaution(self):
        topology = _large_fanout_topology()
        report = compute_blast_radius("payment-service", "restart", topology)
        assert report.risk_tier == RiskTier.CRITICAL
        assert any("maintenance window" in p.lower() or "communication" in p.lower()
                   for p in report.recommended_precautions)

    def test_no_circuit_breaker_generates_cb_warning(self):
        """P1/P2 service without CB should trigger a warning precaution."""
        topology = {
            "payment-service": {
                "tier": "P2",
                "dependencies": [],
                "callers": ["checkout"],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            },
            "checkout": {
                "tier": "P1",
                "dependencies": ["payment-service"],
                "callers": [],
                "has_circuit_breaker": False,   # no circuit breaker
                "team": "checkout-team",
                "traffic_pct": 0.0,
            },
        }
        report = compute_blast_radius("payment-service", "restart", topology)
        cb_warnings = [p for p in report.recommended_precautions
                       if "circuit breaker" in p.lower() or "fallback" in p.lower()]
        assert len(cb_warnings) >= 1

    def test_scale_up_generates_capacity_precaution(self):
        topology = _isolated_topology()
        report = compute_blast_radius("payment-service", "scale_up", topology)
        assert any("capacity" in p.lower() or "node" in p.lower()
                   for p in report.recommended_precautions)

    def test_config_change_generates_canary_precaution(self):
        topology = _isolated_topology()
        report = compute_blast_radius("payment-service", "config_change", topology)
        assert any("canary" in p.lower() for p in report.recommended_precautions)

    def test_precautions_are_deduplicated(self):
        """Calling compute_blast_radius twice should not produce duplicate precautions."""
        topology = _simple_chain_topology()
        report = compute_blast_radius("payment-service", "restart", topology)
        assert len(report.recommended_precautions) == len(set(report.recommended_precautions))


# ---------------------------------------------------------------------------
# safe_to_auto_apply tests
# ---------------------------------------------------------------------------

class TestSafeToAutoApply:
    def test_low_risk_no_p1_is_safe(self):
        """LOW risk + no P1 deps → safe_to_auto_apply = True."""
        topology = {
            "log-collector": {
                "tier": "P3",
                "dependencies": [],
                "callers": [],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            }
        }
        report = compute_blast_radius("log-collector", "restart", topology)
        assert report.risk_tier == RiskTier.LOW
        assert report.safe_to_auto_apply is True

    def test_medium_risk_not_safe(self):
        """MEDIUM risk → safe_to_auto_apply = False."""
        topology = {
            "payment-service": {
                "tier": "P2",
                "dependencies": [],
                "callers": ["api"],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            },
            "api": {
                "tier": "P1",
                "dependencies": ["payment-service"],
                "callers": [],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            },
        }
        report = compute_blast_radius("payment-service", "restart", topology)
        assert report.safe_to_auto_apply is False

    def test_high_risk_not_safe(self):
        topology = _large_fanout_topology()
        report = compute_blast_radius("payment-service", "restart", topology)
        assert report.safe_to_auto_apply is False

    def test_critical_risk_not_safe(self):
        topology = {
            "main-api": {
                "tier": "P1",
                "dependencies": [],
                "callers": [],
                "has_circuit_breaker": False,
                "traffic_pct": 75.0,
            }
        }
        report = compute_blast_radius("main-api", "restart", topology)
        assert report.safe_to_auto_apply is False

    def test_p1_caller_means_not_safe(self):
        """Even with very low impact %, a P1 dep makes it unsafe to auto-apply."""
        topology = {
            "payment-service": {
                "tier": "P2",
                "dependencies": [],
                "callers": ["tiny-p1"],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            },
            "tiny-p1": {
                "tier": "P1",
                "dependencies": ["payment-service"],
                "callers": [],
                "has_circuit_breaker": True,   # CB reduces impact a lot
                "traffic_pct": 0.0,
            },
        }
        report = compute_blast_radius("payment-service", "restart", topology)
        assert report.safe_to_auto_apply is False


# ---------------------------------------------------------------------------
# requires_human_approval tests
# ---------------------------------------------------------------------------

class TestRequiresHumanApproval:
    def test_low_risk_no_approval_needed(self):
        topology = {
            "log-svc": {
                "tier": "P3",
                "dependencies": [],
                "callers": [],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            }
        }
        report = compute_blast_radius("log-svc", "restart", topology)
        assert report.requires_human_approval is False

    def test_medium_risk_requires_approval(self):
        topology = {
            "user-service": {
                "tier": "P2",
                "dependencies": [],
                "callers": [],
                "has_circuit_breaker": False,
                "traffic_pct": 10.0,   # 10% → MEDIUM
            }
        }
        report = compute_blast_radius("user-service", "restart", topology)
        assert report.requires_human_approval is True

    def test_high_risk_requires_approval(self):
        topology = _large_fanout_topology()
        report = compute_blast_radius("payment-service", "restart", topology)
        assert report.requires_human_approval is True

    def test_critical_risk_requires_approval(self):
        topology = {
            "main-api": {
                "tier": "P1",
                "dependencies": [],
                "callers": [],
                "has_circuit_breaker": False,
                "traffic_pct": 75.0,
            }
        }
        report = compute_blast_radius("main-api", "restart", topology)
        assert report.requires_human_approval is True


# ---------------------------------------------------------------------------
# Circuit-breaker impact reduction tests
# ---------------------------------------------------------------------------

class TestCircuitBreakerImpact:
    def test_circuit_breaker_reduces_impact(self):
        """Services with CBs should have lower estimated impact than those without."""
        report_no_cb = compute_blast_radius(
            "payment-service", "restart", _simple_chain_topology()
        )
        report_with_cb = compute_blast_radius(
            "payment-service", "restart", _circuit_breaker_topology()
        )
        # circuit_breaker_topology has all CBs → should be lower or equal total impact
        assert report_with_cb.total_estimated_user_impact_pct <= \
               report_no_cb.total_estimated_user_impact_pct

    def test_cb_service_marked_can_degrade_gracefully(self):
        topology = _circuit_breaker_topology()
        report = compute_blast_radius("payment-service", "restart", topology)
        cb_services = [s for s in report.affected_services if s.can_degrade_gracefully]
        assert len(cb_services) >= 1


# ---------------------------------------------------------------------------
# KG edge augmentation tests
# ---------------------------------------------------------------------------

class TestKGEdgeAugmentation:
    def test_kg_depends_on_edge_adds_caller(self):
        """A DEPENDS_ON KG edge (a→b) should cause a to appear as a caller of b."""
        topology = {
            "payment-service": {
                "tier": "P2",
                "dependencies": [],
                "callers": [],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            },
            "frontend": {
                "tier": "P1",
                "dependencies": [],
                "callers": [],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            },
        }
        kg_edges = [{"src": "frontend", "dst": "payment-service", "rel": "DEPENDS_ON"}]
        report = compute_blast_radius("payment-service", "restart", topology, kg_edges=kg_edges)
        affected_names = {s.name for s in report.affected_services}
        assert "frontend" in affected_names

    def test_no_kg_edges_behaves_same_as_empty_list(self):
        topology = _simple_chain_topology()
        report_none = compute_blast_radius("payment-service", "restart", topology, kg_edges=None)
        report_empty = compute_blast_radius("payment-service", "restart", topology, kg_edges=[])
        assert report_none.risk_tier == report_empty.risk_tier
        assert report_none.total_estimated_user_impact_pct == \
               report_empty.total_estimated_user_impact_pct


# ---------------------------------------------------------------------------
# Report structure / dataclass tests
# ---------------------------------------------------------------------------

class TestReportStructure:
    def test_report_is_blast_radius_report(self):
        report = compute_blast_radius("payment-service", "restart", _simple_chain_topology())
        assert isinstance(report, BlastRadiusReport)

    def test_affected_services_are_affected_service_instances(self):
        report = compute_blast_radius("payment-service", "restart", _simple_chain_topology())
        for svc in report.affected_services:
            assert isinstance(svc, AffectedService)

    def test_dependency_path_includes_target_and_affected(self):
        topology = _simple_chain_topology()
        report = compute_blast_radius("payment-service", "restart", topology)
        for svc in report.affected_services:
            assert len(svc.dependency_path) >= 2
            assert "payment-service" in svc.dependency_path

    def test_fix_type_stored_in_report(self):
        topology = _isolated_topology()
        report = compute_blast_radius("payment-service", "rollback", topology)
        assert report.fix_type == "rollback"

    def test_target_service_stored_in_report(self):
        topology = _isolated_topology()
        report = compute_blast_radius("payment-service", "scale_up", topology)
        assert report.target_service == "payment-service"

    def test_impact_pct_is_non_negative(self):
        topology = _simple_chain_topology()
        report = compute_blast_radius("payment-service", "restart", topology)
        assert report.total_estimated_user_impact_pct >= 0.0

    def test_impact_pct_capped_at_100(self):
        """Total impact should never exceed 100%."""
        topology = _large_fanout_topology()
        # Add even more callers to push impact high
        for i in range(6, 20):
            topology["payment-service"]["callers"].append(f"extra-svc-{i}")
            topology[f"extra-svc-{i}"] = {
                "tier": "P1",
                "dependencies": ["payment-service"],
                "callers": [],
                "has_circuit_breaker": False,
                "traffic_pct": 0.0,
            }
        report = compute_blast_radius("payment-service", "restart", topology)
        assert report.total_estimated_user_impact_pct <= 100.0

    def test_reasoning_is_non_empty_string(self):
        topology = _simple_chain_topology()
        report = compute_blast_radius("payment-service", "restart", topology)
        assert isinstance(report.reasoning, str)
        assert len(report.reasoning) > 0

    def test_risk_tier_is_risk_tier_enum(self):
        topology = _isolated_topology()
        report = compute_blast_radius("payment-service", "restart", topology)
        assert isinstance(report.risk_tier, RiskTier)

    def test_precautions_is_list(self):
        topology = _isolated_topology()
        report = compute_blast_radius("payment-service", "restart", topology)
        assert isinstance(report.recommended_precautions, list)
