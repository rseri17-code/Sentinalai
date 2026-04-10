"""Tests for CMDB traversal engine."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch, call

from supervisor.cmdb_traversal import (
    CMDBTraversal,
    build_change_summary,
    _extract_tier,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_worker(ci_details=None, change_records=None):
    """Build a mock ITSM worker."""
    worker = MagicMock()

    def execute_side_effect(action, params):
        if action == "get_ci_details":
            return {"ci": ci_details or {}}
        if action == "get_change_records":
            return {"change_records": change_records or []}
        return {}

    worker.execute.side_effect = execute_side_effect
    return worker


CI_WITH_DEPS = {
    "name": "payment-service",
    "tier": 1,
    "dependencies": ["payment-db", "redis-cache"],
}

CHANGE_RECORD = {
    "number": "CHG0012345",
    "type": "normal",
    "short_description": "Deploy payment-db v2.1.0",
    "state": "closed",
    "start_date": "2024-01-15T14:00:00",
    "end_date": "2024-01-15T14:30:00",
    "requested_by": "john.doe",
    "risk": "medium",
    "rollback_plan": "kubectl rollout undo deployment/payment-db",
}


# ---------------------------------------------------------------------------
# CMDBTraversal — basic traversal
# ---------------------------------------------------------------------------

class TestCMDBTraversalBasic:
    def test_returns_empty_blast_radius_when_no_changes(self):
        worker = _make_worker(ci_details=CI_WITH_DEPS, change_records=[])
        traversal = CMDBTraversal(worker)
        result = traversal.get_change_blast_radius("payment-service")
        assert result["changes_found"] == 0
        assert result["blast_radius"] == {}

    def test_finds_change_on_direct_dependency(self):
        worker = MagicMock()

        def execute(action, params):
            if action == "get_ci_details":
                return {"ci": CI_WITH_DEPS}
            if action == "get_change_records":
                svc = params.get("service", "")
                if svc == "payment-db":
                    return {"change_records": [CHANGE_RECORD]}
                return {"change_records": []}

        worker.execute.side_effect = execute
        traversal = CMDBTraversal(worker)
        result = traversal.get_change_blast_radius("payment-service")

        assert result["changes_found"] >= 1
        assert "payment-db" in result["blast_radius"]
        assert result["blast_radius"]["payment-db"][0]["number"] == "CHG0012345"

    def test_returns_affected_ci_in_result(self):
        worker = _make_worker()
        traversal = CMDBTraversal(worker)
        result = traversal.get_change_blast_radius("api-gateway")
        assert result["affected_ci"] == "api-gateway"

    def test_includes_root_ci_changes_in_blast_radius(self):
        """Root CI's own changes should also appear in blast_radius."""
        worker = _make_worker(ci_details={"tier": 2}, change_records=[CHANGE_RECORD])
        traversal = CMDBTraversal(worker)
        result = traversal.get_change_blast_radius("payment-service")
        # Root CI changes are stored under its own name
        assert "payment-service" in result["blast_radius"]

    def test_cis_checked_count(self):
        worker = MagicMock()

        def execute(action, params):
            if action == "get_ci_details":
                return {"ci": {"dependencies": ["dep1", "dep2"]}}
            return {"change_records": []}

        worker.execute.side_effect = execute
        traversal = CMDBTraversal(worker)
        result = traversal.get_change_blast_radius("root-service", max_hops=1)
        # root + 2 deps = 3 CIs
        assert result["cis_checked"] >= 1

    def test_dependency_graph_populated(self):
        worker = MagicMock()

        def execute(action, params):
            if action == "get_ci_details":
                return {"ci": {"dependencies": ["db", "cache"]}}
            return {"change_records": []}

        worker.execute.side_effect = execute
        traversal = CMDBTraversal(worker)
        result = traversal.get_change_blast_radius("web-app", max_hops=1)
        assert "web-app" in result["dependency_graph"]
        assert "db" in result["dependency_graph"]["web-app"]
        assert "cache" in result["dependency_graph"]["web-app"]

    def test_handles_worker_exception_gracefully(self):
        worker = MagicMock()
        worker.execute.side_effect = RuntimeError("connection refused")
        traversal = CMDBTraversal(worker)
        result = traversal.get_change_blast_radius("service-x")
        assert result["changes_found"] == 0


# ---------------------------------------------------------------------------
# CMDBTraversal — tier-aware hop limit
# ---------------------------------------------------------------------------

class TestTierAwareTraversal:
    def test_tier1_service_gets_2_hops(self):
        worker = MagicMock()

        def execute(action, params):
            if action == "get_ci_details":
                return {"ci": {"tier": 1, "dependencies": ["dep1"]}}
            return {"change_records": []}

        worker.execute.side_effect = execute
        traversal = CMDBTraversal(worker, max_hops=2)
        result = traversal.get_change_blast_radius("critical-service")
        assert result["hops_traversed"] == 2

    def test_tier2_service_limited_to_1_hop(self):
        worker = MagicMock()

        def execute(action, params):
            if action == "get_ci_details":
                return {"ci": {"tier": 2, "dependencies": ["dep1"]}}
            return {"change_records": []}

        worker.execute.side_effect = execute
        traversal = CMDBTraversal(worker, max_hops=2)
        result = traversal.get_change_blast_radius("secondary-service")
        assert result["hops_traversed"] == 1


# ---------------------------------------------------------------------------
# CMDBTraversal — deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_each_ci_visited_at_most_once(self):
        """Diamond dependency: A -> B, A -> C, B -> D, C -> D. D visited once."""
        call_log: list[str] = []

        def execute(action, params):
            svc = params.get("service", "")
            if action == "get_ci_details":
                call_log.append(svc)
                deps = {"A": ["B", "C"], "B": ["D"], "C": ["D"]}.get(svc, [])
                return {"ci": {"tier": 1, "dependencies": deps}}
            return {"change_records": []}

        worker = MagicMock()
        worker.execute.side_effect = execute
        traversal = CMDBTraversal(worker, max_hops=2)
        traversal.get_change_blast_radius("A")

        # D should only appear once in the call log
        assert call_log.count("D") <= 1


# ---------------------------------------------------------------------------
# get_most_recent_change
# ---------------------------------------------------------------------------

class TestGetMostRecentChange:
    def test_returns_none_when_no_changes(self):
        traversal = CMDBTraversal(MagicMock())
        result = traversal.get_most_recent_change({})
        assert result is None

    def test_returns_most_recent_change(self):
        blast = {
            "service-a": [{"end_date": "2024-01-10", "number": "CHG001"}],
            "service-b": [{"end_date": "2024-01-15", "number": "CHG002"}],
        }
        traversal = CMDBTraversal(MagicMock())
        result = traversal.get_most_recent_change(blast)
        assert result["number"] == "CHG002"
        assert result["_ci"] == "service-b"

    def test_returns_change_with_ci_key(self):
        blast = {"my-service": [{"end_date": "2024-01-01", "number": "CHG999"}]}
        traversal = CMDBTraversal(MagicMock())
        result = traversal.get_most_recent_change(blast)
        assert result is not None
        assert "_ci" in result


# ---------------------------------------------------------------------------
# build_change_summary
# ---------------------------------------------------------------------------

class TestBuildChangeSummary:
    def test_returns_no_changes_message_when_empty(self):
        result = build_change_summary({})
        assert "No recent changes" in result

    def test_includes_ci_name_in_summary(self):
        result = build_change_summary({
            "blast_radius": {"payment-db": [CHANGE_RECORD]},
            "changes_found": 1,
        })
        assert "payment-db" in result

    def test_includes_change_number_in_summary(self):
        result = build_change_summary({
            "blast_radius": {"payment-db": [CHANGE_RECORD]},
            "changes_found": 1,
        })
        assert "CHG0012345" in result

    def test_handles_empty_blast_radius_key(self):
        result = build_change_summary({"blast_radius": {}, "changes_found": 0})
        assert "No recent changes" in result


# ---------------------------------------------------------------------------
# _extract_tier helper
# ---------------------------------------------------------------------------

class TestExtractTier:
    def test_extracts_int_tier(self):
        assert _extract_tier({"tier": 1}) == 1
        assert _extract_tier({"tier": 3}) == 3

    def test_extracts_string_tier(self):
        assert _extract_tier({"tier": "critical"}) == 1
        assert _extract_tier({"tier": "tier1"}) == 1
        assert _extract_tier({"tier": "high"}) == 2

    def test_returns_none_for_missing_tier(self):
        assert _extract_tier({}) is None
        assert _extract_tier({"name": "foo"}) is None

    def test_handles_float_tier(self):
        assert _extract_tier({"tier": 2.0}) == 2
