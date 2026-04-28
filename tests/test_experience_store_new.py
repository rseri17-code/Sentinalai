"""Tests for new functions in supervisor.experience_store:
   store_failed_experience(), get_suggested_root_causes(), get_tool_recommendations(),
   and the dual-store persistence (_load_all_raw, _save_all_raw, legacy migration).
"""
from __future__ import annotations

import os
import json

os.environ.setdefault("EXPERIENCE_STORE_ENABLED", "true")


def _make_path(tmp_path):
    return str(tmp_path / "experience_store.json")


def _seed_experience(mod, incident_type="timeout", service="svc",
                     root_cause="connection pool exhausted", quality=0.85):
    return mod.store_experience(
        f"inc-{os.urandom(4).hex()}",
        incident_type, service,
        {
            "root_cause": root_cause,
            "confidence": 80,
            "_evidence_snapshot": {"logs": ["err1"], "apm_data": {"p99": 200}},
        },
        quality,
    )


# ---------------------------------------------------------------------------
# store_failed_experience()
# ---------------------------------------------------------------------------

class TestStoreFailedExperience:

    def test_stores_below_threshold(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", _make_path(tmp_path))
        result = mod.store_failed_experience(
            "inc-001", "timeout", "svc",
            {"root_cause": "INSUFFICIENT_EVIDENCE", "confidence": 10},
            0.35, "low_confidence",
        )
        assert result is True

    def test_does_not_store_above_threshold(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", _make_path(tmp_path))
        result = mod.store_failed_experience(
            "inc-002", "timeout", "svc",
            {"root_cause": "connection pool", "confidence": 85},
            0.75, "high_quality",
        )
        assert result is False

    def test_disabled_returns_false(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_ENABLED", False)
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", _make_path(tmp_path))
        result = mod.store_failed_experience(
            "inc-003", "timeout", "svc", {}, 0.20,
        )
        assert result is False

    def test_stored_entry_has_failed_flag(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", p)
        mod.store_failed_experience("inc-004", "timeout", "svc", {}, 0.30, "low_quality")
        with open(p) as f:
            data = json.load(f)
        failed = data.get("failed", [])
        assert len(failed) == 1
        assert failed[0]["_failed"] is True
        assert failed[0]["failure_reason"] == "low_quality"

    def test_capped_at_200_failed_entries(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", _make_path(tmp_path))
        for i in range(205):
            mod.store_failed_experience(f"inc-{i}", "timeout", "svc", {}, 0.20)
        with open(_make_path(tmp_path)) as f:
            data = json.load(f)
        assert len(data["failed"]) <= 200

    def test_positive_store_unaffected(self, tmp_path, monkeypatch):
        """Storing a failed experience should not corrupt the positive experience list."""
        import supervisor.experience_store as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", p)
        # First store a good experience
        _seed_experience(mod)
        # Then store a failed one
        mod.store_failed_experience("inc-fail", "timeout", "svc", {}, 0.30)
        # Positive list should still be intact
        exps = mod.retrieve_similar("timeout", "svc")
        assert len(exps) == 1


# ---------------------------------------------------------------------------
# get_suggested_root_causes()
# ---------------------------------------------------------------------------

class TestGetSuggestedRootCauses:

    def test_returns_empty_when_no_data(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", _make_path(tmp_path))
        assert mod.get_suggested_root_causes("timeout", "svc") == []

    def test_returns_root_cause_from_similar(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", _make_path(tmp_path))
        _seed_experience(mod, root_cause="connection pool exhausted")
        causes = mod.get_suggested_root_causes("timeout", "svc")
        assert "connection pool exhausted" in causes

    def test_excludes_insufficient_evidence(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", _make_path(tmp_path))
        _seed_experience(mod, root_cause="INSUFFICIENT_EVIDENCE — need more logs")
        causes = mod.get_suggested_root_causes("timeout", "svc")
        assert causes == []

    def test_deduplicates_root_causes(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", _make_path(tmp_path))
        for _ in range(3):
            _seed_experience(mod, root_cause="same cause every time")
        causes = mod.get_suggested_root_causes("timeout", "svc")
        assert causes.count("same cause every time") == 1

    def test_respects_top_k(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", _make_path(tmp_path))
        for i in range(5):
            _seed_experience(mod, root_cause=f"cause_{i}")
        causes = mod.get_suggested_root_causes("timeout", "svc", top_k=2)
        assert len(causes) <= 2

    def test_disabled_returns_empty(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_ENABLED", False)
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", _make_path(tmp_path))
        assert mod.get_suggested_root_causes("timeout", "svc") == []


# ---------------------------------------------------------------------------
# get_tool_recommendations()
# ---------------------------------------------------------------------------

class TestGetToolRecommendations:

    def test_returns_empty_when_no_data(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", _make_path(tmp_path))
        assert mod.get_tool_recommendations("timeout", "svc") == {}

    def test_returns_evidence_keys_from_successful_experiences(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", _make_path(tmp_path))
        for _ in range(3):
            _seed_experience(mod, root_cause="pool exhausted")
        recs = mod.get_tool_recommendations("timeout", "svc")
        # "logs" and "apm_data" were in the evidence snapshot
        assert "logs" in recs or "apm_data" in recs

    def test_positive_scores_for_helpful_keys(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", _make_path(tmp_path))
        _seed_experience(mod, root_cause="pool exhausted")
        recs = mod.get_tool_recommendations("timeout", "svc")
        # All returned keys should have positive scores
        for key, score in recs.items():
            assert score > 0, f"Expected positive score for {key}, got {score}"

    def test_disabled_returns_empty(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_ENABLED", False)
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", _make_path(tmp_path))
        assert mod.get_tool_recommendations("timeout", "svc") == {}

    def test_respects_top_k(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", _make_path(tmp_path))
        for _ in range(5):
            mod.store_experience(
                f"inc-{os.urandom(4).hex()}", "timeout", "svc",
                {
                    "root_cause": "pool exhausted",
                    "confidence": 85,
                    "_evidence_snapshot": {k: f"val{k}" for k in
                                           ["a", "b", "c", "d", "e", "f", "g"]},
                },
                0.85,
            )
        recs = mod.get_tool_recommendations("timeout", "svc", top_k=3)
        assert len(recs) <= 3

    def test_no_cross_service_contamination(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", _make_path(tmp_path))
        _seed_experience(mod, service="payment-svc", root_cause="pool exhausted")
        # Query for a completely different service
        recs = mod.get_tool_recommendations("oom_kill", "other-svc")
        assert recs == {}


# ---------------------------------------------------------------------------
# Legacy format migration
# ---------------------------------------------------------------------------

class TestLegacyMigration:

    def test_legacy_list_format_migrated(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", p)
        # Write legacy list format
        legacy = [
            {
                "incident_id": "old-1",
                "incident_type": "timeout",
                "service": "svc",
                "root_cause": "pool exhausted",
                "evidence_keys": ["logs"],
                "confidence": 80,
                "online_quality_score": 0.82,
                "timestamp": "2024-01-01T00:00:00Z",
            }
        ]
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(legacy, f)

        # retrieve_similar should work on migrated data
        results = mod.retrieve_similar("timeout", "svc")
        assert len(results) == 1
        assert results[0]["root_cause"] == "pool exhausted"

    def test_legacy_format_gets_written_as_dict(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", p)
        # Write legacy list, then trigger a save
        with open(p, "w") as f:
            json.dump([], f)
        _seed_experience(mod)
        with open(p) as f:
            data = json.load(f)
        # New format should be a dict with "experiences" key
        assert isinstance(data, dict)
        assert "experiences" in data

    def test_corrupt_file_returns_empty(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", p)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("NOT VALID JSON {{{")
        results = mod.retrieve_similar("timeout", "svc")
        assert results == []

    def test_non_dict_non_list_returns_empty(self, tmp_path, monkeypatch):
        import supervisor.experience_store as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EXPERIENCE_STORE_PATH", p)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(42, f)
        all_data = mod._load_all_raw()
        assert all_data == {"experiences": [], "failed": []}
