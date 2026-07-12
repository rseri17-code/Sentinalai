"""Final Loop Engineering Convergence — regression suite.

R1 admission executor, R2 benchmark matcher, R3 nightly pipeline,
P2 usefulness, P3 corpus health, P4 effectiveness — plus the standing
invariants: append-only, deterministic, wave3 disabled.
"""
from __future__ import annotations

import dataclasses
import json

from sentinel_core.intel_memory import MemoryRecord, MemoryStore
from sentinel_core.investigation_artifact import (
    AdmissionController,
    ArtifactStore,
    build_artifact,
)
from sentinel_core.investigation_value import (
    corpus_health_report,
    corpus_usefulness_report,
    learning_effectiveness_report,
    match_scenario,
    run_admission_review,
    run_benchmark_matching,
    run_nightly_learning,
)

from tests.investigation_value.test_wave3_readiness_program import (
    _enriched_result,
)
from tests.synthetic.runner import load_all_scenarios


def _artifact(i: int = 0, *, root_cause=None, created=None, **result_over):
    r = _enriched_result(**result_over)
    if root_cause is not None:
        r["root_cause"] = root_cause
    r["incident_id"] = f"INC-{i}"
    return build_artifact(
        r, incident_id=f"INC-{i}", investigation_id=f"inv-{i}",
        created_at=created or f"2026-07-0{(i % 8) + 1}T00:00:00Z",
        provenance={"producer": "wave1"},
    )


def _k8s_artifact(i: int = 0):
    """Artifact matching the real k8s_pod_crashloop bench scenario."""
    return _artifact(
        i,
        root_cause="checkout container OOMKilled by kubelet — memory "
                     "limit 8Gi is below live working set 8.1Gi",
    )


# ---------------------------------------------------------------------------
# R2 — Benchmark matcher
# ---------------------------------------------------------------------------

class TestBenchmarkMatcher:
    def test_matches_real_scenario(self):
        scenarios = load_all_scenarios()
        m = match_scenario(_k8s_artifact(), scenarios)
        assert m is not None
        assert m["scenario_id"] == "k8s_pod_crashloop"
        assert m["match_score"] >= 0.3

    def test_no_forced_match_below_threshold(self):
        scenarios = load_all_scenarios()
        a = _artifact(root_cause="qq zz xx completely unrelated words")
        assert match_scenario(a, scenarios) is None

    def test_deterministic(self):
        scenarios = load_all_scenarios()
        a = _k8s_artifact()
        assert match_scenario(a, scenarios) == match_scenario(a, scenarios)

    def test_signals_carry_agreement_and_pointer(self):
        scenarios = load_all_scenarios()
        a = _k8s_artifact()
        signals = run_benchmark_matching({a.artifact_id: a}, scenarios)
        sig = signals[a.artifact_id]
        assert sig["benchmark_pointer"] == "bench:k8s_pod_crashloop"
        assert 0.0 <= sig["benchmark_agreement"] <= 1.0
        assert isinstance(sig["benchmark_disagreement"], bool)

    def test_benchmark_signal_satisfies_q4(self):
        a = _k8s_artifact()
        d_without = AdmissionController().classify(a)
        assert "Q4:missing_benchmark_pointer" in d_without.reasons
        d_with = AdmissionController().classify(
            a, signals={"benchmark_pointer": "bench:k8s_pod_crashloop"})
        assert "Q4:missing_benchmark_pointer" not in d_with.reasons


# ---------------------------------------------------------------------------
# R1 — Admission executor
# ---------------------------------------------------------------------------

class TestAdmissionExecutor:
    def _seed(self, tmp_path, artifacts):
        astore = ArtifactStore(tmp_path / "a")
        for a in artifacts:
            astore.save_candidate(a)
        return astore

    def test_admits_and_projects_to_active_memory(self, tmp_path):
        a = _k8s_artifact()
        self._seed(tmp_path, [a])
        report = run_admission_review(
            tmp_path / "a", tmp_path / "m", at="t1",
            signals_by_artifact={a.artifact_id: {
                "benchmark_pointer": "bench:k8s_pod_crashloop"}},
        )
        assert report["admitted"] == [a.artifact_id]
        astore = ArtifactStore(tmp_path / "a")
        assert astore.state_of(a.artifact_id) == "admitted"
        mstore = MemoryStore(tmp_path / "m")
        assert list(mstore.list_ids()) == [a.artifact_id]

    def test_rejects_stay_out_of_memory(self, tmp_path):
        bad = _artifact(1, root_cause="")            # R5 hard reject
        self._seed(tmp_path, [bad])
        report = run_admission_review(tmp_path / "a", tmp_path / "m",
                                        at="t1")
        assert report["rejected"] == [bad.artifact_id]
        assert list(MemoryStore(tmp_path / "m").list_ids()) == []

    def test_quarantine_without_benchmark_signal(self, tmp_path):
        a = _k8s_artifact()
        self._seed(tmp_path, [a])
        report = run_admission_review(tmp_path / "a", tmp_path / "m",
                                        at="t1")
        assert report["quarantined"] == [a.artifact_id]   # fail-closed Q4

    def test_retroactive_demotion_archives_memory(self, tmp_path):
        a = _k8s_artifact()
        self._seed(tmp_path, [a])
        sig = {a.artifact_id: {"benchmark_pointer": "bench:x"}}
        run_admission_review(tmp_path / "a", tmp_path / "m", at="t1",
                              signals_by_artifact=sig)
        # operator rejects later
        sig[a.artifact_id]["operator_rejected"] = True
        report = run_admission_review(tmp_path / "a", tmp_path / "m",
                                        at="t2", signals_by_artifact=sig)
        assert report["demoted"] == [a.artifact_id]
        astore = ArtifactStore(tmp_path / "a")
        assert astore.state_of(a.artifact_id) == "quarantined"
        mstore = MemoryStore(tmp_path / "m")
        assert list(mstore.list_ids()) == []               # out of active
        assert list(mstore.list_deleted())                 # archived, RC-E

    def test_validation_promotion_event_only(self, tmp_path):
        a = _k8s_artifact()
        self._seed(tmp_path, [a])
        sig = {a.artifact_id: {"benchmark_pointer": "bench:x"}}
        run_admission_review(tmp_path / "a", tmp_path / "m", at="t1",
                              signals_by_artifact=sig)
        sig[a.artifact_id]["validated"] = True
        report = run_admission_review(tmp_path / "a", tmp_path / "m",
                                        at="t2", signals_by_artifact=sig)
        assert report["validated"] == [a.artifact_id]
        astore = ArtifactStore(tmp_path / "a")
        assert astore.state_of(a.artifact_id) == "validated"
        # bytes untouched — file still in admitted/
        assert (tmp_path / "a" / "admitted"
                 / f"{a.artifact_id}.json").exists()

    def test_deterministic_and_replayable(self, tmp_path):
        arts = [_k8s_artifact(i) for i in range(3)]
        self._seed(tmp_path, arts)
        sig = {a.artifact_id: {"benchmark_pointer": "bench:x"}
                for a in arts}
        r1 = run_admission_review(tmp_path / "a", tmp_path / "m",
                                    at="t1", signals_by_artifact=sig)
        # second run: everything already transitioned — idempotent
        r2 = run_admission_review(tmp_path / "a", tmp_path / "m",
                                    at="t2", signals_by_artifact=sig)
        assert len(r1["admitted"]) == 3
        assert r2["admitted"] == []                        # no re-admission
        assert r2["errors"] == []
        # audit trail replays the full history
        events = list(ArtifactStore(tmp_path / "a").audit_events())
        assert len([e for e in events if e["to"] == "admitted"]) == 3


# ---------------------------------------------------------------------------
# P2 — Usefulness
# ---------------------------------------------------------------------------

class TestUsefulness:
    def test_components_and_report(self, tmp_path):
        arts = [_k8s_artifact(i) for i in range(3)]
        records = [MemoryRecord.from_artifact(a) for a in arts]
        report = corpus_usefulness_report(records)
        assert report["record_count"] == 3
        top = report["records"][0]
        # recurring cause across 2 other records ⇒ high root_cause
        assert top["components"]["root_cause"] > 0.5
        assert top["components"]["remediation"] == 1.0     # B2 resolution
        assert top["components"]["false_lead"] == 1.0      # B2 gaps
        # planner path empty on these fixtures ⇒ demonstrably useless
        assert "planner" in report["never_influences_retrieval"]

    def test_deterministic(self):
        records = [MemoryRecord.from_artifact(_k8s_artifact(i))
                   for i in range(2)]
        assert json.dumps(corpus_usefulness_report(records),
                           sort_keys=True) \
            == json.dumps(corpus_usefulness_report(records),
                           sort_keys=True)


# ---------------------------------------------------------------------------
# P3 — Corpus health
# ---------------------------------------------------------------------------

class TestCorpusHealth:
    def test_duplicates_conflicts_stale_obsolete(self):
        a1 = MemoryRecord.from_artifact(_k8s_artifact(0))
        # same fingerprint, contradictory cause ⇒ conflict
        a2 = dataclasses.replace(
            a1, memory_id="m2",
            detected_root_cause="totally unrelated dns nxdomain issue")
        stale = dataclasses.replace(
            a1, memory_id="m3", timestamp="2020-01-01T00:00:00Z",
            service="legacy-svc")
        report = corpus_health_report([a1, a2, stale],
                                        as_of="2026-07-11T00:00:00Z")
        assert report["duplicates"]["count"] == 1
        assert len(report["conflicts"]) >= 1
        assert "m3" in report["stale"]["memory_ids"]
        assert "legacy-svc" in report["obsolete_services"]
        assert report["schema_versions"] == {"1": 3}

    def test_deterministic(self):
        recs = [MemoryRecord.from_artifact(_k8s_artifact(i))
                for i in range(2)]
        r1 = corpus_health_report(recs, as_of="2026-07-11")
        r2 = corpus_health_report(recs, as_of="2026-07-11")
        assert json.dumps(r1, sort_keys=True) == json.dumps(r2,
                                                              sort_keys=True)


# ---------------------------------------------------------------------------
# P4 — Learning effectiveness
# ---------------------------------------------------------------------------

class TestEffectiveness:
    def _record(self, day, mtti, score):
        base = MemoryRecord.from_artifact(_k8s_artifact(0))
        return dataclasses.replace(
            base, memory_id=f"m-{day}-{mtti}",
            timestamp=f"2026-07-{day:02d}T00:00:00Z",
            mtti_ms=mtti, investigation_score=score)

    def test_improving_mtti_detected(self):
        records = [self._record(d, mtti, 0.5 + d * 0.05)
                   for d, mtti in ((1, 90000), (2, 60000), (3, 30000))]
        report = learning_effectiveness_report(records)
        assert report["trends"]["mtti_ms"]["verdict"] == "improving"
        assert report["trends"]["rca_quality"]["verdict"] == "improving"
        assert "mtti_ms" in report["improving"]

    def test_degrading_detected_and_blocks_is_learning(self):
        records = [self._record(d, mtti, 0.9 - d * 0.2)
                   for d, mtti in ((1, 30000), (2, 60000), (3, 90000))]
        report = learning_effectiveness_report(records)
        assert report["trends"]["mtti_ms"]["verdict"] == "degrading"
        assert report["is_learning"] is False

    def test_deterministic(self):
        records = [self._record(1, 1000, 0.5)]
        assert json.dumps(learning_effectiveness_report(records),
                           sort_keys=True) \
            == json.dumps(learning_effectiveness_report(records),
                           sort_keys=True)


# ---------------------------------------------------------------------------
# R3 — Nightly pipeline end-to-end
# ---------------------------------------------------------------------------

class TestNightlyPipeline:
    def test_full_pass_admits_and_reports(self, tmp_path):
        astore = ArtifactStore(tmp_path / "a")
        for i in range(3):
            astore.save_candidate(_k8s_artifact(i))
        scenarios = load_all_scenarios()

        summary = run_nightly_learning(
            tmp_path / "a", tmp_path / "m", tmp_path / "out",
            generated_at="2026-07-11T00:00:00Z", scenarios=scenarios,
        )
        # matcher satisfied Q4 ⇒ executor admits all three
        assert len(summary["admission"]["admitted"]) == 3
        assert summary["memory"]["active_records"] == 3
        assert summary["readiness"]["wave3_enabled"] is False
        # all report files written
        for name in ("nightly_summary.json", "memory_usefulness.json",
                      "corpus_health.json", "learning_effectiveness.json",
                      "wave3_readiness.json", "readiness_history.jsonl"):
            assert (tmp_path / "out" / name).exists(), name
        # G4 inputs came from the matcher (bench evidence, not authority)
        readiness = json.loads(
            (tmp_path / "out" / "wave3_readiness.json").read_text())
        g4 = [g for g in readiness["readiness"]["gates"]
              if g["gate"] == "G4"][0]
        assert g4["value"]["mean"] is not None

    def test_no_scenarios_recorded_as_skip_and_fail_closed(self, tmp_path):
        astore = ArtifactStore(tmp_path / "a")
        astore.save_candidate(_k8s_artifact(0))
        summary = run_nightly_learning(
            tmp_path / "a", tmp_path / "m", tmp_path / "out",
            generated_at="2026-07-11T00:00:00Z", scenarios=None,
        )
        assert summary["benchmark_matching"].startswith("skipped")
        # no bench signal ⇒ Q4 quarantine ⇒ nothing admitted (fail-closed)
        assert summary["admission"]["admitted"] == []
        assert summary["admission"]["quarantined"] != []

    def test_deterministic_given_same_inputs(self, tmp_path):
        for sub in ("s1", "s2"):
            astore = ArtifactStore(tmp_path / sub / "a")
            for i in range(2):
                astore.save_candidate(_k8s_artifact(i))
        scenarios = load_all_scenarios()
        outs = []
        for sub in ("s1", "s2"):
            outs.append(run_nightly_learning(
                tmp_path / sub / "a", tmp_path / sub / "m",
                tmp_path / sub / "out",
                generated_at="2026-07-11T00:00:00Z", scenarios=scenarios,
            ))
        assert json.dumps(outs[0], sort_keys=True) \
            == json.dumps(outs[1], sort_keys=True)

    def test_wave3_disabled_even_on_perfect_corpus(self, tmp_path):
        """The structural invariant survives the full loop."""
        astore = ArtifactStore(tmp_path / "a")
        astore.save_candidate(_k8s_artifact(0))
        summary = run_nightly_learning(
            tmp_path / "a", tmp_path / "m", tmp_path / "out",
            generated_at="2026-07-11T00:00:00Z",
            scenarios=load_all_scenarios(),
        )
        assert summary["readiness"]["wave3_enabled"] is False
