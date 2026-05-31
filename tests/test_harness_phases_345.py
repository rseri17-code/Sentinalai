"""Tests for harness Phases 3, 4, and 5.

Phase 3: Recovery outcome → PatternRegistry.update_outcome()
Phase 4: Harness post-flight registers pattern outcomes; reflection carries pattern fields
Phase 5: CoFailureIndex partners appear in blast radius output
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Phase 3 — Recovery outcome → pattern feedback
# ---------------------------------------------------------------------------

class TestPhase3PatternFeedback:
    def test_run_learning_step_calls_update_outcome_on_correct(self, tmp_path):
        """Ground-truth eval correct → PatternRegistry.update_outcome called with was_correct=True."""
        from supervisor.learning_loop import run_learning_step

        mock_eval = MagicMock()
        mock_eval.root_cause_match = "exact"
        mock_eval.actual_correct = True
        mock_eval.predicted_confidence = 80
        mock_eval.root_cause_score = 1.0
        mock_eval.confidence_error = 0.0
        mock_eval.evidence_coverage = 1.0
        mock_eval.missing_evidence = []

        result = {
            "root_cause": "db_slow",
            "_dna_fingerprint": "abc123def456abcd",
            "confidence": 80,
        }

        with patch("supervisor.learning_loop.GroundTruthEvaluator") as mock_gte, \
             patch("supervisor.learning_loop._persist_eval"), \
             patch("supervisor.learning_loop._update_calibrator"), \
             patch("supervisor.learning_loop._update_pattern_outcome") as mock_upo:
            mock_gte.from_file.return_value.has_ground_truth.return_value = True
            mock_gte.from_file.return_value.evaluate.return_value = mock_eval

            run_learning_step("INC-001", result)

        mock_upo.assert_called_once_with(
            fingerprint="abc123def456abcd",
            root_cause="db_slow",
            was_correct=True,
        )

    def test_run_learning_step_calls_update_outcome_on_incorrect(self):
        """Ground-truth eval incorrect → update_outcome called with was_correct=False."""
        from supervisor.learning_loop import run_learning_step

        mock_eval = MagicMock()
        mock_eval.actual_correct = False
        mock_eval.predicted_confidence = 60
        mock_eval.root_cause_match = "none"
        mock_eval.root_cause_score = 0.0
        mock_eval.confidence_error = 0.4
        mock_eval.evidence_coverage = 0.5
        mock_eval.missing_evidence = []

        result = {
            "root_cause": "network_flap",
            "_dna_fingerprint": "deadbeef12345678",
            "confidence": 60,
        }

        with patch("supervisor.learning_loop.GroundTruthEvaluator") as mock_gte, \
             patch("supervisor.learning_loop._persist_eval"), \
             patch("supervisor.learning_loop._update_calibrator"), \
             patch("supervisor.learning_loop._update_pattern_outcome") as mock_upo:
            mock_gte.from_file.return_value.has_ground_truth.return_value = True
            mock_gte.from_file.return_value.evaluate.return_value = mock_eval

            run_learning_step("INC-002", result)

        mock_upo.assert_called_once_with(
            fingerprint="deadbeef12345678",
            root_cause="network_flap",
            was_correct=False,
        )

    def test_run_learning_step_skips_update_when_no_fingerprint(self):
        """Missing fingerprint → update_outcome not called."""
        from supervisor.learning_loop import run_learning_step

        mock_eval = MagicMock()
        mock_eval.actual_correct = True
        mock_eval.predicted_confidence = 75
        mock_eval.root_cause_match = "exact"
        mock_eval.root_cause_score = 1.0
        mock_eval.confidence_error = 0.0
        mock_eval.evidence_coverage = 1.0
        mock_eval.missing_evidence = []

        result = {"root_cause": "oomkill", "confidence": 75}  # no _dna_fingerprint

        with patch("supervisor.learning_loop.GroundTruthEvaluator") as mock_gte, \
             patch("supervisor.learning_loop._persist_eval"), \
             patch("supervisor.learning_loop._update_calibrator"), \
             patch("supervisor.learning_loop._update_pattern_outcome") as mock_upo:
            mock_gte.from_file.return_value.has_ground_truth.return_value = True
            mock_gte.from_file.return_value.evaluate.return_value = mock_eval

            run_learning_step("INC-003", result)

        mock_upo.assert_not_called()

    def test_record_verification_outcome_updates_pattern_when_fingerprint_provided(self):
        """Verification outcome with fingerprint → PatternRegistry.update_outcome called."""
        from supervisor.learning_loop import record_verification_outcome

        with patch("supervisor.learning_loop._update_pattern_outcome") as mock_upo, \
             patch("supervisor.learning_loop.get_calibrator") as mock_cal, \
             patch("supervisor.learning_loop._calibrator_lock"):
            mock_cal.return_value.update = MagicMock()
            mock_cal.return_value.save = MagicMock()

            record_verification_outcome(
                investigation_id="inv-99",
                rca_was_correct=True,
                predicted_confidence=82.0,
                dna_fingerprint="aabbccdd11223344",
                root_cause="db_slow",
            )

        mock_upo.assert_called_once_with("aabbccdd11223344", "db_slow", True)

    def test_record_verification_outcome_skips_pattern_when_no_fingerprint(self):
        """Verification outcome without fingerprint → PatternRegistry not touched."""
        from supervisor.learning_loop import record_verification_outcome

        with patch("supervisor.learning_loop._update_pattern_outcome") as mock_upo, \
             patch("supervisor.learning_loop.get_calibrator") as mock_cal, \
             patch("supervisor.learning_loop._calibrator_lock"):
            mock_cal.return_value.update = MagicMock()
            mock_cal.return_value.save = MagicMock()

            record_verification_outcome(
                investigation_id="inv-100",
                rca_was_correct=False,
                predicted_confidence=45.0,
            )

        mock_upo.assert_not_called()

    def test_update_pattern_outcome_writes_to_registry(self, tmp_path):
        """_update_pattern_outcome delegates to PatternRegistry.update_outcome."""
        from supervisor.learning_loop import _update_pattern_outcome
        from supervisor.pattern_registry import PatternRegistry

        reg = PatternRegistry(str(tmp_path / "reg.json"))
        features = [0.5] * 16
        reg.record("fp_test", "timeout", features)

        with patch("supervisor.pattern_registry.get_registry", return_value=reg):
            _update_pattern_outcome("fp_test", "db_slow", True)
            _update_pattern_outcome("fp_test", "db_slow", True)
            _update_pattern_outcome("fp_test", "db_slow", False)

        rec = reg.get("fp_test")
        assert rec is not None
        assert rec.success_rate("db_slow") == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# Phase 4 — Harness post-flight registers pattern outcomes
# ---------------------------------------------------------------------------

class TestPhase4HarnessPatternIntegration:
    def _make_harness(self):
        from supervisor.agent_harness import InvestigationHarness
        return InvestigationHarness()

    def test_post_flight_calls_update_pattern_outcome_when_quality_above_gate(self):
        from supervisor.agent_harness import InvestigationHarness, HARNESS_QUALITY_GATE
        harness = InvestigationHarness()
        result = {
            "_dna_fingerprint": "cafebabe12345678",
            "root_cause": "db_slow",
            "_online_quality_score": HARNESS_QUALITY_GATE + 0.05,
            "_evidence_snapshot": {},
        }
        reflection = MagicMock()

        with patch("supervisor.learning_loop.run_learning_step"), \
             patch("supervisor.agent_harness._update_pattern_outcome", create=True) as mock_upo, \
             patch("supervisor.learning_loop._update_pattern_outcome") as mock_ll_upo:
            harness._post_flight_learning("INC-1", result, reflection)

        # Should call update_outcome with was_correct=True (quality >= gate)
        mock_ll_upo.assert_called_once_with(
            "cafebabe12345678", "db_slow", True
        )

    def test_post_flight_marks_incorrect_when_quality_below_gate(self):
        from supervisor.agent_harness import InvestigationHarness, HARNESS_QUALITY_GATE
        harness = InvestigationHarness()
        result = {
            "_dna_fingerprint": "deadcafe87654321",
            "root_cause": "network_flap",
            "_online_quality_score": max(0.0, HARNESS_QUALITY_GATE - 0.15),
            "_evidence_snapshot": {},
        }
        reflection = MagicMock()

        with patch("supervisor.learning_loop.run_learning_step"), \
             patch("supervisor.learning_loop._update_pattern_outcome") as mock_upo:
            harness._post_flight_learning("INC-2", result, reflection)

        mock_upo.assert_called_once_with(
            "deadcafe87654321", "network_flap", False
        )

    def test_post_flight_skips_pattern_when_no_fingerprint(self):
        from supervisor.agent_harness import InvestigationHarness
        harness = InvestigationHarness()
        result = {
            "root_cause": "oomkill",
            "_online_quality_score": 0.85,
            "_evidence_snapshot": {},
        }
        reflection = MagicMock()

        with patch("supervisor.learning_loop.run_learning_step"), \
             patch("supervisor.learning_loop._update_pattern_outcome") as mock_upo:
            harness._post_flight_learning("INC-3", result, reflection)

        mock_upo.assert_not_called()

    def test_reflection_to_dict_includes_pattern_fields(self):
        from supervisor.agent_harness import HarnessReflection
        r = HarnessReflection(investigation_id="inv-1", incident_id="INC-1")
        r.pattern_match_count = 3
        r.pattern_top_hypothesis = "db_slow"
        d = r.to_dict()
        assert d["pattern_match_count"] == 3
        assert d["pattern_top_hypothesis"] == "db_slow"

    def test_reflection_pattern_fields_default_to_empty(self):
        from supervisor.agent_harness import HarnessReflection
        r = HarnessReflection(investigation_id="inv-2", incident_id="INC-2")
        d = r.to_dict()
        assert d["pattern_match_count"] == 0
        assert d["pattern_top_hypothesis"] is None


# ---------------------------------------------------------------------------
# Phase 5 — CoFailureIndex partners augment blast radius
# ---------------------------------------------------------------------------

class TestPhase5CoFailureBlastRadius:
    def _topology(self) -> dict:
        return {
            "payment-api": {
                "tier": "P1",
                "dependencies": ["db"],
                "callers": [],
                "has_circuit_breaker": False,
            },
            "db": {
                "tier": "P2",
                "dependencies": [],
                "callers": ["payment-api"],
                "has_circuit_breaker": False,
            },
            "cache": {
                "tier": "P2",
                "dependencies": [],
                "callers": [],
                "has_circuit_breaker": True,
            },
        }

    def test_co_failure_partner_added_to_affected_services(self, tmp_path):
        from supervisor.blast_radius import compute_blast_radius
        from supervisor.co_failure_index import CoFailureIndex

        idx = CoFailureIndex(str(tmp_path / "cf.json"))
        # Record 4 co-failures in 5 incidents → rate = 0.80 (above 0.20 threshold)
        for _ in range(4):
            idx.record_investigation("payment-api", ["cache"])
        idx.record_investigation("payment-api", [])

        report = compute_blast_radius(
            target_service="payment-api",
            fix_type="restart",
            cmdb_topology=self._topology(),
            co_failure_index=idx,
        )
        names = {s.name for s in report.affected_services}
        assert "cache" in names

    def test_co_failure_partner_dependency_type_is_co_failure(self, tmp_path):
        from supervisor.blast_radius import compute_blast_radius
        from supervisor.co_failure_index import CoFailureIndex

        idx = CoFailureIndex(str(tmp_path / "cf.json"))
        for _ in range(3):
            idx.record_investigation("payment-api", ["cache"])

        report = compute_blast_radius(
            target_service="payment-api",
            fix_type="restart",
            cmdb_topology=self._topology(),
            co_failure_index=idx,
        )
        co_failure_svcs = [s for s in report.affected_services if s.dependency_type == "co_failure"]
        assert len(co_failure_svcs) == 1
        assert co_failure_svcs[0].name == "cache"

    def test_co_failure_below_min_rate_not_added(self, tmp_path):
        """Partner with rate below 0.20 threshold should be excluded."""
        from supervisor.blast_radius import compute_blast_radius
        from supervisor.co_failure_index import CoFailureIndex

        idx = CoFailureIndex(str(tmp_path / "cf.json"))
        # 1 co-failure in 10 incidents → rate = 0.10 (below 0.20)
        idx.record_investigation("payment-api", ["cache"])
        for _ in range(9):
            idx.record_investigation("payment-api", [])

        report = compute_blast_radius(
            target_service="payment-api",
            fix_type="restart",
            cmdb_topology=self._topology(),
            co_failure_index=idx,
        )
        co_failure_svcs = [s for s in report.affected_services if s.dependency_type == "co_failure"]
        assert len(co_failure_svcs) == 0

    def test_bfs_discovered_services_not_duplicated(self, tmp_path):
        """Service already in BFS results should not be added again as co_failure."""
        from supervisor.blast_radius import compute_blast_radius
        from supervisor.co_failure_index import CoFailureIndex

        idx = CoFailureIndex(str(tmp_path / "cf.json"))
        # db is already a direct_downstream of payment-api via BFS
        for _ in range(4):
            idx.record_investigation("payment-api", ["db"])

        report = compute_blast_radius(
            target_service="payment-api",
            fix_type="restart",
            cmdb_topology=self._topology(),
            co_failure_index=idx,
        )
        db_entries = [s for s in report.affected_services if s.name == "db"]
        assert len(db_entries) == 1  # not duplicated

    def test_no_co_failure_index_does_not_augment(self):
        """Passing no co_failure_index leaves blast radius unchanged (backward compat)."""
        from supervisor.blast_radius import compute_blast_radius

        report = compute_blast_radius(
            target_service="payment-api",
            fix_type="restart",
            cmdb_topology=self._topology(),
            # co_failure_index omitted
        )
        co_failure_svcs = [s for s in report.affected_services if s.dependency_type == "co_failure"]
        assert len(co_failure_svcs) == 0

    def test_co_failure_index_exception_returns_bfs_result(self, tmp_path):
        """CoFailureIndex.get_co_failures raising → BFS result returned intact."""
        from supervisor.blast_radius import compute_blast_radius

        bad_idx = MagicMock()
        bad_idx.get_co_failures.side_effect = RuntimeError("index corrupt")

        report = compute_blast_radius(
            target_service="payment-api",
            fix_type="restart",
            cmdb_topology=self._topology(),
            co_failure_index=bad_idx,
        )
        assert report is not None
        assert report.target_service == "payment-api"

    def test_co_failure_impact_scaled_by_rate(self, tmp_path):
        """Co-failure partner impact = tier_base_impact * co_failure_rate."""
        from supervisor.blast_radius import compute_blast_radius, _TIER_BASE_IMPACT
        from supervisor.co_failure_index import CoFailureIndex

        idx = CoFailureIndex(str(tmp_path / "cf.json"))
        # 2 co-failures in 4 incidents → rate = 0.5
        for _ in range(2):
            idx.record_investigation("payment-api", ["cache"])
        for _ in range(2):
            idx.record_investigation("payment-api", [])

        report = compute_blast_radius(
            target_service="payment-api",
            fix_type="restart",
            cmdb_topology=self._topology(),
            co_failure_index=idx,
        )
        co_svc = next(s for s in report.affected_services if s.dependency_type == "co_failure")
        expected = round(_TIER_BASE_IMPACT["P2"] * 0.5, 2)
        assert co_svc.estimated_impact_pct == pytest.approx(expected)
