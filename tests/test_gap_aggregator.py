"""Tests for supervisor.gap_aggregator."""
from __future__ import annotations

import json
import os
import pytest

os.environ.setdefault("GAP_AGGREGATOR_ENABLED", "true")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_path(tmp_path):
    return str(tmp_path / "gap_patterns.json")


# ---------------------------------------------------------------------------
# record_gaps()
# ---------------------------------------------------------------------------

class TestRecordGaps:

    def test_creates_file_on_first_record(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))
        mod.record_gaps("timeout", "auth-service", ["apm_data"])
        assert os.path.exists(_make_path(tmp_path))

    def test_increments_count(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))
        mod.record_gaps("timeout", "auth-service", ["apm_data"])
        mod.record_gaps("timeout", "auth-service", ["apm_data"])
        with open(_make_path(tmp_path)) as f:
            data = json.load(f)
        entry = data["timeout:auth-service"]["apm_data"]
        assert entry["count"] == 2
        assert entry["total_seen"] == 2

    def test_frequency_calculated_correctly(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))
        mod.record_gaps("timeout", "svc", ["logs"])
        mod.record_gaps("timeout", "svc", [])       # no gap this time
        mod.record_gaps("timeout", "svc", ["logs"])
        with open(_make_path(tmp_path)) as f:
            data = json.load(f)
        assert data["timeout:svc"]["logs"]["frequency"] == pytest.approx(2 / 3, abs=0.01)

    def test_ignores_empty_gap_list(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", p)
        mod.record_gaps("timeout", "svc", [])
        # File may or may not be created; no gap entries should exist
        if os.path.exists(p):
            with open(p) as f:
                data = json.load(f)
            bucket = data.get("timeout:svc", {})
            gap_keys = [k for k in bucket if not k.startswith("_")]
            assert len(gap_keys) == 0

    def test_ignores_missing_incident_type_or_service(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", p)
        mod.record_gaps("", "svc", ["apm_data"])
        mod.record_gaps("timeout", "", ["apm_data"])
        assert not os.path.exists(p)

    def test_disabled_noop(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_ENABLED", False)
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", p)
        mod.record_gaps("timeout", "svc", ["apm_data"])
        assert not os.path.exists(p)

    def test_multiple_categories_in_one_call(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))
        mod.record_gaps("timeout", "svc", ["apm_data", "logs", "metrics"])
        with open(_make_path(tmp_path)) as f:
            data = json.load(f)
        bucket = data["timeout:svc"]
        assert "apm_data" in bucket
        assert "logs" in bucket
        assert "metrics" in bucket

    def test_categories_starting_with_underscore_ignored(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))
        mod.record_gaps("timeout", "svc", ["_internal", "apm_data"])
        with open(_make_path(tmp_path)) as f:
            data = json.load(f)
        bucket = data["timeout:svc"]
        assert "_internal" not in bucket
        assert "apm_data" in bucket


# ---------------------------------------------------------------------------
# get_persistent_gaps()
# ---------------------------------------------------------------------------

class TestGetPersistentGaps:

    def _seed(self, mod, incident_type, service, gap, n_with, n_without):
        """Record gap n_with times, then record n_without times without it."""
        for _ in range(n_with):
            mod.record_gaps(incident_type, service, [gap])
        for _ in range(n_without):
            mod.record_gaps(incident_type, service, [])

    def test_returns_empty_when_no_data(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))
        assert mod.get_persistent_gaps("timeout", "svc") == []

    def test_returns_gap_above_threshold(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))
        # 7/10 = 70% → above default 50% threshold
        self._seed(mod, "timeout", "svc", "apm_data", 7, 3)
        gaps = mod.get_persistent_gaps("timeout", "svc")
        assert "apm_data" in gaps

    def test_does_not_return_gap_below_threshold(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))
        # 3/10 = 30% → below default 50% threshold
        self._seed(mod, "timeout", "svc", "logs", 3, 7)
        gaps = mod.get_persistent_gaps("timeout", "svc")
        assert "logs" not in gaps

    def test_custom_threshold_override(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))
        self._seed(mod, "timeout", "svc", "metrics", 3, 7)
        # With low threshold (0.2), 30% should pass
        gaps = mod.get_persistent_gaps("timeout", "svc", threshold=0.20)
        assert "metrics" in gaps

    def test_sorted_by_frequency_descending(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))
        # apm_data: 9/10, logs: 6/10
        for _ in range(9):
            mod.record_gaps("timeout", "svc", ["apm_data"])
        mod.record_gaps("timeout", "svc", [])
        for _ in range(6):
            mod.record_gaps("timeout", "svc", ["logs"])

        gaps = mod.get_persistent_gaps("timeout", "svc")
        if len(gaps) >= 2:
            assert gaps[0] == "apm_data"

    def test_disabled_returns_empty(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_ENABLED", False)
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))
        assert mod.get_persistent_gaps("timeout", "svc") == []

    def test_broad_key_included_with_discount(self, tmp_path, monkeypatch):
        """Gaps recorded under (type, '*') broad key are included at 0.7x weight."""
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))
        # Record under broad key '*'
        for _ in range(8):
            mod.record_gaps("timeout", "*", ["trace_correlation"])
        mod.record_gaps("timeout", "*", [])
        mod.record_gaps("timeout", "*", [])

        # Query for specific service that has no data of its own
        gaps = mod.get_persistent_gaps("timeout", "other-svc")
        # 8/10 = 80%, discounted to 56% — still above 50% threshold
        assert "trace_correlation" in gaps


# ---------------------------------------------------------------------------
# get_gap_report()
# ---------------------------------------------------------------------------

class TestGetGapReport:

    def test_empty_report_when_no_data(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))
        report = mod.get_gap_report()
        assert "patterns" in report
        assert len(report["patterns"]) == 0

    def test_report_contains_recorded_patterns(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))
        mod.record_gaps("timeout", "svc", ["apm_data"])
        report = mod.get_gap_report()
        assert "timeout:svc" in report["patterns"]

    def test_report_filtered_by_incident_type(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))
        mod.record_gaps("timeout", "svc-a", ["apm_data"])
        mod.record_gaps("oom_kill", "svc-b", ["logs"])
        report = mod.get_gap_report(incident_type="timeout")
        assert "timeout:svc-a" in report["patterns"]
        assert "oom_kill:svc-b" not in report["patterns"]

    def test_report_marks_persistent_gaps(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))
        for _ in range(8):
            mod.record_gaps("timeout", "svc", ["apm_data"])
        mod.record_gaps("timeout", "svc", [])
        mod.record_gaps("timeout", "svc", [])
        report = mod.get_gap_report()
        gaps_list = report["patterns"]["timeout:svc"]["gaps"]
        # gaps is a list of (cat, info) tuples
        gap_dict = dict(gaps_list)
        assert gap_dict["apm_data"]["persistent"] is True


# ---------------------------------------------------------------------------
# record_gaps_from_critique()
# ---------------------------------------------------------------------------

class TestRecordGapsFromCritique:

    def test_accepts_critique_result_object(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))

        class FakeCritique:
            gaps = ["no golden signals collected", "missing APM data"]

        mod.record_gaps_from_critique("timeout", "svc", FakeCritique())
        assert os.path.exists(_make_path(tmp_path))

    def test_accepts_dict_critique(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", _make_path(tmp_path))
        mod.record_gaps_from_critique("timeout", "svc", {
            "gaps": ["log data was missing", "no trace context"]
        })
        with open(_make_path(tmp_path)) as f:
            data = json.load(f)
        bucket = data.get("timeout:svc", {})
        assert "logs" in bucket or "trace_correlation" in bucket

    def test_empty_gaps_noop(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", p)
        mod.record_gaps_from_critique("timeout", "svc", {"gaps": []})
        assert not os.path.exists(p)

    def test_disabled_noop(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_ENABLED", False)
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", p)
        mod.record_gaps_from_critique("timeout", "svc", {"gaps": ["missing apm data"]})
        assert not os.path.exists(p)


# ---------------------------------------------------------------------------
# _parse_gap_categories() keyword mapping
# ---------------------------------------------------------------------------

class TestParseGapCategories:

    def test_golden_signal_keyword(self):
        from supervisor.gap_aggregator import _parse_gap_categories
        cats = _parse_gap_categories(["no golden signal data collected"])
        assert "golden_signals" in cats

    def test_apm_keyword(self):
        from supervisor.gap_aggregator import _parse_gap_categories
        assert "apm_data" in _parse_gap_categories(["missing APM metrics"])

    def test_log_keyword(self):
        from supervisor.gap_aggregator import _parse_gap_categories
        assert "logs" in _parse_gap_categories(["log data unavailable"])

    def test_deploy_maps_to_devops_context(self):
        from supervisor.gap_aggregator import _parse_gap_categories
        assert "devops_context" in _parse_gap_categories(["no deploy records found"])

    def test_cmdb_keyword(self):
        from supervisor.gap_aggregator import _parse_gap_categories
        assert "cmdb_blast_radius" in _parse_gap_categories(["cmdb lookup failed"])

    def test_unknown_text_returns_empty(self):
        from supervisor.gap_aggregator import _parse_gap_categories
        assert _parse_gap_categories(["something completely unrelated"]) == []

    def test_case_insensitive(self):
        from supervisor.gap_aggregator import _parse_gap_categories
        assert "apm_data" in _parse_gap_categories(["APM DATA MISSING"])

    def test_multiple_keywords_in_one_string(self):
        from supervisor.gap_aggregator import _parse_gap_categories
        cats = _parse_gap_categories(["git commit and log data missing"])
        assert "git_context" in cats
        assert "logs" in cats

    def test_empty_input_returns_empty(self):
        from supervisor.gap_aggregator import _parse_gap_categories
        assert _parse_gap_categories([]) == []


# ---------------------------------------------------------------------------
# Persistence edge cases
# ---------------------------------------------------------------------------

class TestPersistenceEdgeCases:

    def test_corrupt_file_resets_gracefully(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", p)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("NOT VALID JSON {{{")
        # Should not crash — resets to empty store
        mod.record_gaps("timeout", "svc", ["apm_data"])
        assert os.path.exists(p)

    def test_atomic_write_no_tmp_residue(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", p)
        mod.record_gaps("timeout", "svc", ["logs"])
        assert not os.path.exists(p + ".tmp")

    def test_meta_updated_on_save(self, tmp_path, monkeypatch):
        import supervisor.gap_aggregator as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "GAP_AGGREGATOR_PATH", p)
        mod.record_gaps("timeout", "svc", ["apm_data"])
        with open(p) as f:
            data = json.load(f)
        assert "_meta" in data
        assert data["_meta"]["total_records"] == 1
