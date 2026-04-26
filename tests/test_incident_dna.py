"""Tests for supervisor/incident_dna.py — Incident DNA feature-vector encoding."""

from __future__ import annotations

import json
import math
import os
import tempfile

import pytest

from supervisor.incident_dna import (
    FEATURE_NAMES,
    DNAMatch,
    IncidentDNA,
    encode_incident,
    find_similar_by_dna,
    load_dna_store,
    save_dna_store,
    _cosine_similarity,
    _matching_dimensions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _full_evidence() -> dict:
    return {
        "observed_error_rate": 0.35,
        "baseline_error_rate": 0.001,
        "observed_p95": 31000,
        "baseline_p95": 200,
        "cpu_percent": 85,
        "memory_percent": 90,
        "network_errors": ["connection refused"],
        "num_affected_services": 5,
        "last_deploy_minutes_ago": 10,
        "num_evidence_sources": 5,
    }


def _sparse_evidence() -> dict:
    return {}


def _make_dna(incident_id: str, incident_type: str = "timeout", service: str = "svc",
              features: list[float] | None = None) -> IncidentDNA:
    if features is not None:
        return IncidentDNA(
            incident_id=incident_id,
            incident_type=incident_type,
            service=service,
            features=features,
            feature_names=list(FEATURE_NAMES),
            encoded_at="2024-01-01T00:00:00+00:00",
        )
    return encode_incident(
        incident_id=incident_id,
        incident_type=incident_type,
        service=service,
        evidence=_full_evidence(),
        rca_confidence=90.0,
        service_tier="P1",
        resolution_minutes=45,
    )


# ---------------------------------------------------------------------------
# Feature vector shape
# ---------------------------------------------------------------------------

class TestEncodeShape:
    def test_feature_count(self):
        dna = encode_incident("INC1", "timeout", "svc", {}, 80.0)
        assert len(dna.features) == 16

    def test_feature_names_length(self):
        dna = encode_incident("INC1", "timeout", "svc", {}, 80.0)
        assert len(dna.feature_names) == 16

    def test_feature_names_match_constants(self):
        dna = encode_incident("INC1", "timeout", "svc", {}, 80.0)
        assert dna.feature_names == list(FEATURE_NAMES)

    def test_all_features_in_unit_interval(self):
        dna = encode_incident("INC1", "error_spike", "svc", _full_evidence(), 95.0)
        for i, v in enumerate(dna.features):
            assert 0.0 <= v <= 1.0, f"Feature {i} ({FEATURE_NAMES[i]}) out of range: {v}"

    def test_sparse_evidence_no_crash(self):
        dna = encode_incident("INC1", "latency", "svc", _sparse_evidence(), 50.0)
        assert len(dna.features) == 16
        assert all(f >= 0.0 for f in dna.features)


# ---------------------------------------------------------------------------
# Individual feature encoding
# ---------------------------------------------------------------------------

class TestFeatureEncoding:
    def test_error_rate_ratio_capped_at_one(self):
        ev = {"observed_error_rate": 100.0, "baseline_error_rate": 0.001}
        dna = encode_incident("I", "error_spike", "s", ev, 50.0)
        assert dna.features[0] == 1.0

    def test_error_rate_ratio_zero_baseline(self):
        ev = {"observed_error_rate": 0.5, "baseline_error_rate": 0.0}
        dna = encode_incident("I", "error_spike", "s", ev, 50.0)
        assert dna.features[0] == 0.0

    def test_latency_ratio_normal(self):
        ev = {"observed_p95": 400, "baseline_p95": 200}
        dna = encode_incident("I", "latency", "s", ev, 50.0)
        assert dna.features[1] == pytest.approx(1.0)  # capped

    def test_cpu_high(self):
        dna = encode_incident("I", "saturation", "s", {"cpu_percent": 95}, 50.0)
        assert dna.features[2] == 1.0

    def test_cpu_medium(self):
        dna = encode_incident("I", "saturation", "s", {"cpu_percent": 65}, 50.0)
        assert dna.features[2] == 0.5

    def test_cpu_low(self):
        dna = encode_incident("I", "saturation", "s", {"cpu_percent": 30}, 50.0)
        assert dna.features[2] == 0.0

    def test_memory_high(self):
        dna = encode_incident("I", "oomkill", "s", {"memory_percent": 92}, 50.0)
        assert dna.features[3] == 1.0

    def test_memory_medium(self):
        dna = encode_incident("I", "oomkill", "s", {"memory_percent": 70}, 50.0)
        assert dna.features[3] == 0.5

    def test_memory_low(self):
        dna = encode_incident("I", "oomkill", "s", {"memory_percent": 40}, 50.0)
        assert dna.features[3] == 0.0

    def test_network_anomaly_present(self):
        dna = encode_incident("I", "network", "s", {"network_errors": ["timeout"]}, 50.0)
        assert dna.features[4] == 1.0

    def test_network_anomaly_absent(self):
        dna = encode_incident("I", "network", "s", {}, 50.0)
        assert dna.features[4] == 0.0

    def test_multi_service_impact_capped(self):
        dna = encode_incident("I", "cascading", "s", {"num_affected_services": 20}, 50.0)
        assert dna.features[5] == 1.0

    def test_multi_service_impact_partial(self):
        dna = encode_incident("I", "cascading", "s", {"num_affected_services": 5}, 50.0)
        assert dna.features[5] == pytest.approx(0.5)

    def test_change_recency_within_30min(self):
        dna = encode_incident("I", "error_spike", "s", {"last_deploy_minutes_ago": 15}, 50.0)
        assert dna.features[6] == 1.0

    def test_change_recency_within_2h(self):
        dna = encode_incident("I", "error_spike", "s", {"last_deploy_minutes_ago": 90}, 50.0)
        assert dna.features[6] == 0.7

    def test_change_recency_within_24h(self):
        dna = encode_incident("I", "error_spike", "s", {"last_deploy_minutes_ago": 720}, 50.0)
        assert dna.features[6] == 0.3

    def test_change_recency_old(self):
        dna = encode_incident("I", "error_spike", "s", {"last_deploy_minutes_ago": 2000}, 50.0)
        assert dna.features[6] == 0.0

    def test_change_recency_no_deploy(self):
        dna = encode_incident("I", "error_spike", "s", {}, 50.0)
        assert dna.features[6] == 0.0

    def test_incident_type_timeout_onehot(self):
        dna = encode_incident("I", "timeout", "s", {}, 50.0)
        assert dna.features[7] == 1.0
        assert dna.features[8] == 0.0

    def test_incident_type_oomkill_onehot(self):
        dna = encode_incident("I", "oomkill", "s", {}, 50.0)
        assert dna.features[8] == 1.0
        assert dna.features[7] == 0.0

    def test_incident_type_error_spike_onehot(self):
        dna = encode_incident("I", "error_spike", "s", {}, 50.0)
        assert dna.features[9] == 1.0

    def test_incident_type_network_onehot(self):
        dna = encode_incident("I", "network", "s", {}, 50.0)
        assert dna.features[10] == 1.0

    def test_incident_type_saturation_onehot(self):
        dna = encode_incident("I", "saturation", "s", {}, 50.0)
        assert dna.features[11] == 1.0

    def test_incident_type_unknown_all_zero(self):
        dna = encode_incident("I", "latency", "s", {}, 50.0)
        assert all(dna.features[7:12] == [0.0] * 5 for _ in [1])

    def test_service_tier_p1(self):
        dna = encode_incident("I", "timeout", "s", {}, 50.0, service_tier="P1")
        assert dna.features[12] == 1.0

    def test_service_tier_p2(self):
        dna = encode_incident("I", "timeout", "s", {}, 50.0, service_tier="P2")
        assert dna.features[12] == pytest.approx(0.67)

    def test_service_tier_p3(self):
        dna = encode_incident("I", "timeout", "s", {}, 50.0, service_tier="P3")
        assert dna.features[12] == pytest.approx(0.33)

    def test_evidence_source_count_explicit(self):
        dna = encode_incident("I", "timeout", "s", {"num_evidence_sources": 7}, 50.0)
        assert dna.features[13] == 1.0

    def test_evidence_source_count_fallback(self):
        ev = {"key1": "val1", "key2": "val2", "key3": "val3"}
        dna = encode_incident("I", "timeout", "s", ev, 50.0)
        assert dna.features[13] == pytest.approx(3 / 7)

    def test_confidence_score(self):
        dna = encode_incident("I", "timeout", "s", {}, 75.0)
        assert dna.features[14] == pytest.approx(0.75)

    def test_confidence_clamped(self):
        dna = encode_incident("I", "timeout", "s", {}, 110.0)
        assert dna.features[14] == 1.0

    def test_resolution_bucket_fast(self):
        dna = encode_incident("I", "timeout", "s", {}, 50.0, resolution_minutes=5)
        assert dna.features[15] == 0.25

    def test_resolution_bucket_medium(self):
        dna = encode_incident("I", "timeout", "s", {}, 50.0, resolution_minutes=30)
        assert dna.features[15] == 0.5

    def test_resolution_bucket_long(self):
        dna = encode_incident("I", "timeout", "s", {}, 50.0, resolution_minutes=120)
        assert dna.features[15] == 0.75

    def test_resolution_bucket_very_long(self):
        dna = encode_incident("I", "timeout", "s", {}, 50.0, resolution_minutes=300)
        assert dna.features[15] == 1.0


# ---------------------------------------------------------------------------
# Cosine similarity
# ---------------------------------------------------------------------------

class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [0.5, 0.3, 0.0, 0.8, 1.0] + [0.0] * 11
        assert _cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        a = [1.0] + [0.0] * 15
        b = [0.0, 1.0] + [0.0] * 14
        assert _cosine_similarity(a, b) == pytest.approx(0.0)

    def test_zero_vector_returns_zero(self):
        a = [0.0] * 16
        b = [0.5] * 16
        assert _cosine_similarity(a, b) == 0.0

    def test_both_zero_returns_zero(self):
        assert _cosine_similarity([0.0] * 16, [0.0] * 16) == 0.0

    def test_symmetry(self):
        a = [0.1, 0.9, 0.4, 0.0] + [0.3] * 12
        b = [0.8, 0.2, 0.5, 1.0] + [0.1] * 12
        assert _cosine_similarity(a, b) == pytest.approx(_cosine_similarity(b, a))

    def test_result_in_unit_interval(self):
        a = [0.3, 0.7, 0.0, 0.5] + [0.2] * 12
        b = [0.1, 0.9, 0.4, 0.0] + [0.6] * 12
        sim = _cosine_similarity(a, b)
        assert 0.0 <= sim <= 1.0

    def test_dna_similarity_method(self):
        dna1 = _make_dna("I1", features=[1.0] * 16)
        dna2 = _make_dna("I2", features=[1.0] * 16)
        assert dna1.similarity(dna2) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Matching dimensions
# ---------------------------------------------------------------------------

class TestMatchingDimensions:
    def test_both_above_threshold(self):
        a = [0.8] * 16
        b = [0.7] * 16
        dims = _matching_dimensions(a, b)
        assert len(dims) == 16

    def test_none_above_threshold(self):
        a = [0.3] * 16
        b = [0.3] * 16
        dims = _matching_dimensions(a, b)
        assert dims == []

    def test_partial_match(self):
        a = [0.9, 0.9, 0.1, 0.1] + [0.0] * 12
        b = [0.8, 0.1, 0.9, 0.1] + [0.0] * 12
        dims = _matching_dimensions(a, b)
        assert dims == [FEATURE_NAMES[0]]  # only index 0 both > 0.5


# ---------------------------------------------------------------------------
# find_similar_by_dna
# ---------------------------------------------------------------------------

class TestFindSimilarByDNA:
    def _make_pool(self, n: int, incident_type: str = "timeout") -> list[IncidentDNA]:
        pool = []
        for i in range(n):
            pool.append(encode_incident(
                f"PAST{i}", incident_type, "svc",
                {"cpu_percent": 85, "num_evidence_sources": 4},
                rca_confidence=80.0,
            ))
        return pool

    def test_self_excluded(self):
        query = _make_dna("INC1")
        pool = [query]
        results = find_similar_by_dna(query, pool, top_k=5, min_similarity=0.0)
        assert all(r.incident_id != "INC1" for r in results)

    def test_identical_dna_scores_highest(self):
        query = encode_incident("Q", "timeout", "svc", _full_evidence(), 90.0)
        exact_copy = IncidentDNA(
            incident_id="COPY",
            incident_type=query.incident_type,
            service=query.service,
            features=list(query.features),
            feature_names=list(query.feature_names),
            encoded_at=query.encoded_at,
        )
        other = encode_incident("OTHER", "oomkill", "other-svc", {}, 30.0)
        results = find_similar_by_dna(query, [exact_copy, other], top_k=5, min_similarity=0.0)
        assert results[0].incident_id == "COPY"
        assert results[0].similarity_score == pytest.approx(1.0)

    def test_min_similarity_filter(self):
        query = encode_incident("Q", "timeout", "svc", _full_evidence(), 90.0)
        low_sim = encode_incident("LOW", "oomkill", "other", {}, 10.0)
        results = find_similar_by_dna(query, [low_sim], top_k=5, min_similarity=0.99)
        assert results == []

    def test_top_k_limit(self):
        query = encode_incident("Q", "timeout", "svc", _full_evidence(), 90.0)
        pool = self._make_pool(10)
        results = find_similar_by_dna(query, pool, top_k=3, min_similarity=0.0)
        assert len(results) <= 3

    def test_sorted_descending(self):
        query = encode_incident("Q", "timeout", "svc", _full_evidence(), 90.0)
        pool = self._make_pool(5)
        results = find_similar_by_dna(query, pool, top_k=10, min_similarity=0.0)
        scores = [r.similarity_score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_empty_pool(self):
        query = _make_dna("Q")
        assert find_similar_by_dna(query, [], top_k=5) == []

    def test_insight_generated(self):
        query = encode_incident("Q", "timeout", "svc", _full_evidence(), 90.0)
        pool = self._make_pool(3)
        results = find_similar_by_dna(query, pool, top_k=5, min_similarity=0.0)
        for r in results:
            assert isinstance(r.insight, str)
            assert len(r.insight) > 10

    def test_cross_type_match_insight_mentions_types(self):
        query = encode_incident("Q", "timeout", "svc",
                                {"cpu_percent": 85, "memory_percent": 88}, 80.0)
        candidate = IncidentDNA(
            incident_id="C1",
            incident_type="oomkill",
            service="other-svc",
            features=list(query.features),
            feature_names=list(query.feature_names),
            encoded_at="2024-01-01T00:00:00+00:00",
        )
        results = find_similar_by_dna(query, [candidate], top_k=1, min_similarity=0.0)
        assert len(results) == 1
        assert "timeout" in results[0].insight or "oomkill" in results[0].insight


# ---------------------------------------------------------------------------
# Serialisation roundtrip
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_save_load_roundtrip(self, tmp_path):
        store_path = str(tmp_path / "dna_store.json")
        dnas = [
            encode_incident("I1", "timeout", "svc-a", _full_evidence(), 90.0),
            encode_incident("I2", "oomkill", "svc-b", {"memory_percent": 95}, 75.0),
        ]
        save_dna_store(dnas, store_path)
        loaded = load_dna_store(store_path)
        assert len(loaded) == 2
        assert loaded[0].incident_id == "I1"
        assert loaded[1].incident_id == "I2"
        assert loaded[0].features == dnas[0].features

    def test_load_missing_file_returns_empty(self, tmp_path):
        result = load_dna_store(str(tmp_path / "nonexistent.json"))
        assert result == []

    def test_load_corrupt_file_returns_empty(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json at all {{}")
        result = load_dna_store(str(p))
        assert result == []

    def test_to_dict_roundtrip(self):
        dna = encode_incident("I1", "error_spike", "svc", _full_evidence(), 88.0)
        restored = IncidentDNA.from_dict(dna.to_dict())
        assert restored.incident_id == dna.incident_id
        assert restored.features == dna.features
        assert restored.feature_names == dna.feature_names
