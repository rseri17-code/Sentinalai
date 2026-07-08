"""Wave 1 — Investigation Artifact regression suite.

Covers the fourteen mission test areas:
build, id determinism, id sensitivity, serialization round-trip,
immutability, no-evidence-values rule, admission hard reject, admission
quarantine, candidate persistence, append-only behavior, flag-off no-op,
flag-on candidate write, investigate()-output non-mutation, and (via CI)
full-regression compatibility.
"""
from __future__ import annotations

import copy
import json

import pytest

from sentinel_core.investigation_artifact import (
    AdmissionController,
    ArtifactStore,
    ArtifactStoreError,
    InvestigationArtifact,
    artifact_from_dict,
    artifact_to_dict,
    build_artifact,
    make_artifact_id,
)
from supervisor.artifact_writer import maybe_write_investigation_artifact


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _receipt(phase: str, status: str = "success", **meta) -> dict:
    return {
        "phase_name": phase,
        "status": status,
        "started_at": "2026-07-07T00:00:00Z",
        "completed_at": "2026-07-07T00:00:01Z",
        "elapsed_ms": 1000,
        "evidence_count_before": 0,
        "evidence_count_after": 3,
        "warnings": [],
        "degraded_reason": "",
        "error_type": "",
        "metadata": dict(meta),
    }


def _full_result(**overrides) -> dict:
    result = {
        "incident_id": "INC-1",
        "root_cause": "database pool exhausted at checkout",
        "confidence": 78,
        "evidence_timeline": [{"t": 1, "e": "pool saturation"}],
        "reasoning": "pool metrics exceeded limit",
        "online_quality_score": 0.85,
        "citation_coverage": 0.9,
        "_evidence_snapshot": {"logs": True, "metrics_red": True,
                                "iops": True},
        "_phase_receipts": [
            _receipt("fetch"),
            _receipt("classify"),
            _receipt("collect"),
            _receipt("analyze", intelligence=[
                {"name": "decision_intelligence",
                 "payload": {"confidence": 70,
                              "investigation_priority": "high"}},
            ]),
            _receipt("persist"),
        ],
    }
    result.update(overrides)
    return result


def _build(result=None, **kwargs):
    defaults = dict(
        incident_id="INC-1",
        investigation_id="inv-abc",
        created_at="2026-07-07T00:00:02Z",
        provenance={"producer": "wave1", "planner_mode": "playbook"},
    )
    defaults.update(kwargs)
    return build_artifact(result or _full_result(), **defaults)


# ---------------------------------------------------------------------------
# 1. Build from receipts + result
# ---------------------------------------------------------------------------

class TestBuild:
    def test_builds_from_result_and_receipts(self):
        a = _build()
        assert a.incident_id == "INC-1"
        assert a.investigation_id == "inv-abc"
        assert a.root_cause == "database pool exhausted at checkout"
        assert a.confidence == 78
        assert a.status == "completed"
        assert len(a.phase_receipts) == 5
        assert len(a.receipt_hashes) == 5
        assert a.evidence_key_summary["count"] == 3
        assert a.replay_pointer == "INC-1"
        assert a.schema_version == 1
        assert a.admission_state == "candidate"

    def test_decision_summary_lifted_from_receipt_metadata(self):
        a = _build()
        assert a.decision_summary["investigation_priority"] == "high"

    def test_worker_execution_summary_per_phase(self):
        a = _build()
        assert set(a.worker_execution_summary.keys()) == {
            "fetch", "classify", "collect", "analyze", "persist",
        }
        assert a.worker_execution_summary["fetch"]["status"] == "success"

    def test_status_meta_query(self):
        r = _full_result(root_cause="META_QUERY_NOT_INCIDENT")
        assert _build(r).status == "meta_query"

    def test_status_early_return(self):
        r = _full_result()
        r["_phase_receipts"] = r["_phase_receipts"][:2]
        assert _build(r).status == "early_return"

    def test_status_blocked(self):
        r = _full_result(root_cause="BLOCKED: hallucination gate")
        assert _build(r).status == "blocked"

    def test_status_failed(self):
        r = _full_result()
        r["_phase_receipts"][2]["status"] = "failed"
        assert _build(r).status == "failed"

    def test_secrets_redacted_in_receipts(self):
        r = _full_result()
        r["_phase_receipts"][0]["metadata"] = {
            "api_key": "sk-abcdef1234567890abcdef",
        }
        a = _build(r)
        text = json.dumps(artifact_to_dict(a))
        assert "sk-abcdef1234567890abcdef" not in text
        assert "***REDACTED***" in text


# ---------------------------------------------------------------------------
# 2-3. artifact_id determinism + sensitivity
# ---------------------------------------------------------------------------

class TestArtifactId:
    def test_deterministic_across_repeated_builds(self):
        a1 = _build()
        a2 = _build()
        assert a1.artifact_id == a2.artifact_id
        assert json.dumps(artifact_to_dict(a1), sort_keys=True) \
            == json.dumps(artifact_to_dict(a2), sort_keys=True)

    def test_changes_when_root_cause_changes(self):
        a1 = _build()
        a2 = _build(_full_result(root_cause="totally different cause"))
        assert a1.artifact_id != a2.artifact_id

    def test_changes_when_confidence_changes(self):
        a1 = _build()
        a2 = _build(_full_result(confidence=10))
        assert a1.artifact_id != a2.artifact_id

    def test_no_delimiter_collision(self):
        """RC-G: framed JSON — list framing must be unambiguous."""
        id1 = make_artifact_id({"x": ["a,b"], "y": 1})
        id2 = make_artifact_id({"x": ["a", "b"], "y": 1})
        assert id1 != id2

    def test_admission_state_excluded_from_id(self):
        a = _build()
        d = artifact_to_dict(a)
        d2 = dict(d)
        d2["admission_state"] = "admitted"
        assert make_artifact_id(d) == make_artifact_id(d2)


# ---------------------------------------------------------------------------
# 4. Serialization round trip
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_round_trip_byte_identical(self):
        a = _build()
        d = artifact_to_dict(a)
        j = json.dumps(d, sort_keys=True)
        assert d == json.loads(j)                       # RC-I contract
        a2 = artifact_from_dict(json.loads(j))
        assert json.dumps(artifact_to_dict(a2), sort_keys=True) == j

    def test_from_dict_preserves_schema_version(self):
        d = artifact_to_dict(_build())
        d["schema_version"] = 7
        assert artifact_from_dict(d).schema_version == 7   # RC-I

    def test_from_dict_tolerates_missing_fields(self):
        a = artifact_from_dict({"artifact_id": "x" * 16})
        assert a.root_cause == ""
        assert a.phase_receipts == ()
        assert a.confidence == 0


# ---------------------------------------------------------------------------
# 5. Immutability
# ---------------------------------------------------------------------------

class TestImmutability:
    def test_attributes_frozen(self):
        a = _build()
        with pytest.raises(Exception):
            a.root_cause = "mutated"

    def test_dict_fields_frozen(self):
        a = _build()
        with pytest.raises(TypeError):
            a.final_result_summary["injected"] = True
        with pytest.raises(TypeError):
            a.provenance.clear()

    def test_receipt_dicts_frozen(self):
        a = _build()
        with pytest.raises(TypeError):
            a.phase_receipts[0]["status"] = "tampered"


# ---------------------------------------------------------------------------
# 6. No full evidence values stored
# ---------------------------------------------------------------------------

class TestNoEvidenceValues:
    def test_only_keys_counts_pointers(self):
        r = _full_result()
        # a large sentinel evidence payload the artifact must NOT contain
        r["_evidence_snapshot"] = {"logs": True}
        r["logs"] = {"payload": "SENTINEL_RAW_EVIDENCE_VALUE" * 100}
        a = _build(r)
        text = json.dumps(artifact_to_dict(a))
        assert "SENTINEL_RAW_EVIDENCE_VALUE" not in text
        assert a.evidence_key_summary["keys"] == ["logs"]

    def test_result_summary_holds_key_names_only(self):
        a = _build()
        assert "keys" in a.final_result_summary
        assert "evidence_timeline" in a.final_result_summary["keys"]
        # the timeline VALUE must not be embedded
        assert "pool saturation" not in json.dumps(artifact_to_dict(a))


# ---------------------------------------------------------------------------
# 7-8. Admission classification
# ---------------------------------------------------------------------------

class TestAdmission:
    def test_hard_reject_meta_query(self):
        a = _build(_full_result(root_cause="META_QUERY_NOT_INCIDENT"))
        d = AdmissionController().classify(a)
        assert d.state == "rejected"
        assert any(r.startswith("R1") for r in d.reasons)

    def test_hard_reject_early_return(self):
        r = _full_result()
        r["_phase_receipts"] = r["_phase_receipts"][:1]
        d = AdmissionController().classify(_build(r))
        assert d.state == "rejected"
        assert any(x.startswith("R2") for x in d.reasons)

    def test_hard_reject_missing_root_cause(self):
        d = AdmissionController().classify(_build(_full_result(root_cause="")))
        assert d.state == "rejected"
        assert any(x.startswith("R5") for x in d.reasons)

    def test_hard_reject_insufficient_evidence(self):
        r = _full_result()
        r["_evidence_snapshot"] = {"logs": True}
        d = AdmissionController().classify(_build(r))
        assert d.state == "rejected"
        assert any(x.startswith("R6") for x in d.reasons)

    def test_hard_reject_gate_block(self):
        d = AdmissionController().classify(
            _build(_full_result(root_cause="BLOCKED: gate")))
        assert d.state == "rejected"
        assert any(x.startswith("R4") for x in d.reasons)

    def test_hard_reject_failed_phase(self):
        r = _full_result()
        r["_phase_receipts"][4]["status"] = "failed"
        d = AdmissionController().classify(_build(r))
        assert d.state == "rejected"
        assert any(x.startswith("R7") for x in d.reasons)

    def test_hard_reject_circuit_broken_signal(self):
        d = AdmissionController().classify(
            _build(), signals={"circuit_broken": True})
        assert d.state == "rejected"
        assert any(x.startswith("R3") for x in d.reasons)

    def test_quarantine_low_confidence(self):
        d = AdmissionController().classify(_build(_full_result(confidence=10)))
        assert d.state == "quarantined"
        assert "Q1:low_confidence" in d.reasons

    def test_quarantine_operator_rejection_signal(self):
        d = AdmissionController().classify(
            _build(), signals={"operator_rejected": True})
        assert d.state == "quarantined"
        assert "Q5:operator_rejected" in d.reasons

    def test_quarantine_replay_and_benchmark_signals(self):
        d = AdmissionController().classify(
            _build(),
            signals={"replay_regression": True,
                     "benchmark_disagreement": True})
        assert d.state == "quarantined"
        assert "Q6:replay_regression" in d.reasons
        assert "Q7:benchmark_disagreement" in d.reasons

    def test_fail_closed_missing_benchmark_pointer_quarantines(self):
        """Wave 1 artifacts have empty benchmark pointers ⇒ every clean
        artifact lands in quarantine, not admitted. Fail-closed."""
        d = AdmissionController().classify(_build())
        assert d.state == "quarantined"
        assert "Q4:missing_benchmark_pointer" in d.reasons

    def test_hard_reject_wins_over_quarantine(self):
        a = _build(_full_result(root_cause="", confidence=5))
        d = AdmissionController().classify(a)
        assert d.state == "rejected"


# ---------------------------------------------------------------------------
# 9-10. Candidate persistence + append-only
# ---------------------------------------------------------------------------

class TestStore:
    def test_candidate_persistence(self, tmp_path):
        store = ArtifactStore(tmp_path)
        a = _build()
        path = store.save_candidate(a)
        assert path.exists()
        assert store.state_of(a.artifact_id) == "candidate"
        loaded = store.load(a.artifact_id)
        assert loaded.artifact_id == a.artifact_id
        assert json.dumps(artifact_to_dict(loaded), sort_keys=True) \
            == json.dumps(artifact_to_dict(a), sort_keys=True)

    def test_save_idempotent_no_overwrite(self, tmp_path):
        store = ArtifactStore(tmp_path)
        a = _build()
        p1 = store.save_candidate(a)
        mtime = p1.stat().st_mtime_ns
        p2 = store.save_candidate(a)
        assert p1 == p2
        assert p1.stat().st_mtime_ns == mtime      # untouched

    def test_transition_moves_file_and_appends_audit(self, tmp_path):
        store = ArtifactStore(tmp_path)
        a = _build()
        store.save_candidate(a)
        store.transition(a.artifact_id, "quarantined",
                          reasons=("Q1:low_confidence",), at="t1")
        assert store.state_of(a.artifact_id) == "quarantined"
        assert not (tmp_path / "candidate" / f"{a.artifact_id}.json").exists()
        events = list(store.audit_events())
        assert events[-1]["to"] == "quarantined"
        assert events[-1]["reasons"] == ["Q1:low_confidence"]

    def test_validated_is_event_only(self, tmp_path):
        store = ArtifactStore(tmp_path)
        a = _build()
        store.save_candidate(a)
        store.transition(a.artifact_id, "admitted", at="t1")
        store.transition(a.artifact_id, "validated", at="t2")
        # file still lives in admitted/ — bytes untouched
        assert (tmp_path / "admitted" / f"{a.artifact_id}.json").exists()
        assert store.state_of(a.artifact_id) == "validated"

    def test_validated_requires_admitted(self, tmp_path):
        store = ArtifactStore(tmp_path)
        a = _build()
        store.save_candidate(a)
        with pytest.raises(ArtifactStoreError):
            store.transition(a.artifact_id, "validated")

    def test_record_decision_does_not_move(self, tmp_path):
        store = ArtifactStore(tmp_path)
        a = _build()
        store.save_candidate(a)
        store.record_decision(a.artifact_id, "admitted",
                               reasons=(), at="t1")
        assert store.state_of(a.artifact_id) == "candidate"   # no move
        events = list(store.audit_events())
        assert events[-1]["to"] == "decision:admitted"

    def test_rejects_path_traversal_ids(self, tmp_path):
        store = ArtifactStore(tmp_path)
        with pytest.raises(ArtifactStoreError):
            store.load("../escape")


# ---------------------------------------------------------------------------
# 11-13. Feature flag + runtime non-mutation
# ---------------------------------------------------------------------------

class TestRuntimeHook:
    def test_flag_off_no_artifact_no_mutation(self, tmp_path, monkeypatch):
        monkeypatch.delenv("INVESTIGATION_ARTIFACT_ENABLED", raising=False)
        monkeypatch.setenv("ARTIFACT_STORE_PATH", str(tmp_path))
        result = _full_result()
        before = copy.deepcopy(result)
        maybe_write_investigation_artifact(result, "INC-1", "inv-abc")
        assert result == before                       # untouched
        assert not any(tmp_path.iterdir()) if tmp_path.exists() else True

    def test_flag_on_persists_candidate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INVESTIGATION_ARTIFACT_ENABLED", "true")
        monkeypatch.setenv("ARTIFACT_STORE_PATH", str(tmp_path))
        result = _full_result()
        before = copy.deepcopy(result)
        maybe_write_investigation_artifact(result, "INC-1", "inv-abc")
        assert result == before                       # read-only contract
        store = ArtifactStore(tmp_path)
        ids = store.list_ids("candidate")
        assert len(ids) == 1
        assert store.load(ids[0]).incident_id == "INC-1"

    def test_flag_on_with_admission_records_decision_only(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("INVESTIGATION_ARTIFACT_ENABLED", "true")
        monkeypatch.setenv("ADMISSION_CONTROL_ENABLED", "true")
        monkeypatch.setenv("ARTIFACT_STORE_PATH", str(tmp_path))
        maybe_write_investigation_artifact(_full_result(), "INC-1", "inv-abc")
        store = ArtifactStore(tmp_path)
        ids = store.list_ids("candidate")
        assert len(ids) == 1                          # still candidate
        events = list(store.audit_events())
        assert len(events) == 1
        assert events[0]["to"].startswith("decision:")

    def test_never_raises_on_bad_input(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INVESTIGATION_ARTIFACT_ENABLED", "true")
        # point the store root at a FILE — mkdir will fail inside the writer
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("occupied")
        monkeypatch.setenv("ARTIFACT_STORE_PATH", str(blocker))
        # must swallow, log, and return — never raise
        maybe_write_investigation_artifact(_full_result(), "INC-1")
        maybe_write_investigation_artifact(None, "INC-1")
        maybe_write_investigation_artifact("not-a-dict", "INC-1")  # type: ignore[arg-type]
