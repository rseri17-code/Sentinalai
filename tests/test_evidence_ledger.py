"""Phase 8 — typed evidence ledger tests.

Covers:
- construction and defaults
- add / get / has / keys / values / items
- dict round-trip (lossless on keys + values)
- nested values survive verbatim
- duplicate key handling (replace semantics)
- provenance metadata (kind / source / confidence / metadata)
- snapshot immutability
- merge behavior
- shadow flag plumbing
- import-cycle guard
- supervisor.agent evidence dict behavior unchanged (no live wiring)
"""
from __future__ import annotations

import dataclasses
import json
import os

import pytest

from sentinel_core.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceLedger,
    EvidenceSnapshot,
    EvidenceSource,
    dict_to_ledger,
    infer_kind_for_key,
    infer_source_for_key,
    is_shadow_enabled,
    ledger_to_dict,
    round_trip,
    SHADOW_ENV_VAR,
)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_empty_ledger(self):
        l = EvidenceLedger()
        assert len(l) == 0
        assert l.keys() == []
        assert l.to_dict() == {}

    def test_add_single_item(self):
        l = EvidenceLedger()
        l.add("logs", [{"line": "abc"}])
        assert l.has("logs")
        assert l.get("logs") == [{"line": "abc"}]

    def test_empty_key_rejected(self):
        l = EvidenceLedger()
        with pytest.raises(ValueError):
            l.add("", "x")


# ---------------------------------------------------------------------------
# add / get / has / keys / values / items
# ---------------------------------------------------------------------------

class TestDictLikeSurface:
    def test_get_returns_raw_value(self):
        l = EvidenceLedger()
        l.add("metrics", {"cpu": 0.9})
        # get() returns the raw value, NOT the EvidenceItem
        assert l.get("metrics") == {"cpu": 0.9}
        assert not isinstance(l.get("metrics"), EvidenceItem)

    def test_get_with_default(self):
        l = EvidenceLedger()
        assert l.get("missing", "fallback") == "fallback"
        assert l.get("missing") is None

    def test_has(self):
        l = EvidenceLedger()
        l.add("logs", [])
        assert l.has("logs")
        assert not l.has("nope")

    def test_contains_operator(self):
        l = EvidenceLedger()
        l.add("x", 1)
        assert "x" in l
        assert "y" not in l

    def test_keys_preserves_insertion_order(self):
        l = EvidenceLedger()
        l.add("c", 1)
        l.add("a", 2)
        l.add("b", 3)
        assert l.keys() == ["c", "a", "b"]

    def test_values_in_order(self):
        l = EvidenceLedger()
        l.add("a", 1)
        l.add("b", 2)
        assert l.values() == [1, 2]

    def test_items_returns_key_rawvalue_tuples(self):
        l = EvidenceLedger()
        l.add("a", {"x": 1})
        l.add("b", [1, 2, 3])
        items = l.items()
        assert items == [("a", {"x": 1}), ("b", [1, 2, 3])]
        # values must be raw, NOT EvidenceItem
        for _, v in items:
            assert not isinstance(v, EvidenceItem)

    def test_iteration_yields_keys(self):
        l = EvidenceLedger()
        l.add("a", 1)
        l.add("b", 2)
        assert list(l) == ["a", "b"]

    def test_full_items_preserves_provenance(self):
        l = EvidenceLedger()
        l.add("logs", [], source=EvidenceSource.WORKER, kind=EvidenceKind.LOGS)
        full = l.full_items()
        assert full[0][0] == "logs"
        assert isinstance(full[0][1], EvidenceItem)
        assert full[0][1].source == EvidenceSource.WORKER
        assert full[0][1].kind == EvidenceKind.LOGS


# ---------------------------------------------------------------------------
# Dict round-trip (lossless)
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_simple_roundtrip(self):
        d = {"logs": [{"line": "x"}], "metrics": {"cpu": 0.5}}
        assert round_trip(d) == d

    def test_underscore_keys_preserved(self):
        """The supervisor uses _-prefixed sentinel keys (_incident_type,
        _itsm_change_correlations, etc). These must round-trip intact."""
        d = {
            "_incident_type": "error_spike",
            "_raw_diff": "diff --git ...",
            "_itsm_change_correlations": [{"id": "CHG1"}],
            "_loop_escalation": {"reason": "stuck"},
        }
        assert round_trip(d) == d

    def test_nested_dict_values_survive(self):
        d = {
            "devops_context": {
                "deployments": [{"id": "d1", "service": "x"}],
                "workflow_runs": [{"id": "r1"}],
                "nested": {"deep": {"deeper": {"x": 1}}},
            },
        }
        assert round_trip(d) == d

    def test_mixed_value_types(self):
        d = {
            "logs": [],
            "metrics": {},
            "_loop_escalation": None,
            "str_value": "hello",
            "int_value": 42,
            "float_value": 3.14,
            "bool_value": True,
            "list_value": [1, 2, 3],
            "dict_value": {"k": "v"},
        }
        assert round_trip(d) == d

    def test_empty_dict_roundtrip(self):
        assert round_trip({}) == {}

    def test_json_roundtrip_via_ledger(self):
        """Workflow checkpoints / replay artifacts must json-serialize the
        ledger's dict output and survive."""
        d = {
            "search_logs": {"logs": [{"line": "x"}], "log_count": 1},
            "_incident_type": "oom",
        }
        ledger = dict_to_ledger(d)
        json_str = json.dumps(ledger.to_dict())
        decoded = json.loads(json_str)
        assert decoded == d


# ---------------------------------------------------------------------------
# Duplicate key handling (replace semantics)
# ---------------------------------------------------------------------------

class TestDuplicateKey:
    def test_add_twice_replaces_value(self):
        l = EvidenceLedger()
        l.add("logs", [{"old": True}])
        l.add("logs", [{"new": True}])
        assert l.get("logs") == [{"new": True}]
        assert len(l) == 1

    def test_replace_updates_provenance(self):
        l = EvidenceLedger()
        l.add("metrics", {"v": 1}, source=EvidenceSource.UNKNOWN)
        l.add("metrics", {"v": 2}, source=EvidenceSource.WORKER)
        item = l.get_item("metrics")
        assert item.value == {"v": 2}
        assert item.source == EvidenceSource.WORKER

    def test_add_preserves_insertion_order_on_replace(self):
        """Replacing an existing key must NOT move it to the end."""
        l = EvidenceLedger()
        l.add("a", 1)
        l.add("b", 2)
        l.add("c", 3)
        l.add("a", 99)  # replace a
        assert l.keys() == ["a", "b", "c"]  # NOT ["b", "c", "a"]


# ---------------------------------------------------------------------------
# Provenance metadata
# ---------------------------------------------------------------------------

class TestProvenance:
    def test_default_provenance(self):
        l = EvidenceLedger()
        l.add("x", 1)
        item = l.get_item("x")
        assert item.source == EvidenceSource.UNKNOWN
        assert item.kind == EvidenceKind.OTHER
        assert item.confidence == 0.0
        assert item.metadata == {}

    def test_explicit_provenance(self):
        l = EvidenceLedger()
        l.add(
            "logs", [{"line": "x"}],
            source=EvidenceSource.WORKER,
            kind=EvidenceKind.LOGS,
            confidence=0.9,
            metadata={"worker": "log_worker", "playbook_step": "search"},
        )
        item = l.get_item("logs")
        assert item.source == EvidenceSource.WORKER
        assert item.kind == EvidenceKind.LOGS
        assert item.confidence == 0.9
        assert item.metadata["worker"] == "log_worker"

    def test_timestamp_auto_stamped(self):
        l = EvidenceLedger()
        l.add("x", 1)
        item = l.get_item("x")
        assert item.timestamp > 0

    def test_item_frozen(self):
        l = EvidenceLedger()
        l.add("x", 1)
        item = l.get_item("x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            item.value = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Snapshot immutability
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_snapshot_freezes_state(self):
        l = EvidenceLedger()
        l.add("a", 1)
        snap = l.snapshot()
        # Mutate the source ledger after snapshot
        l.add("b", 2)
        l.add("a", 99)
        # Snapshot is unaffected
        assert snap.to_dict() == {"a": 1}
        assert len(snap) == 1

    def test_snapshot_is_frozen(self):
        l = EvidenceLedger()
        l.add("a", 1)
        snap = l.snapshot()
        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.items = ()  # type: ignore[misc]

    def test_snapshot_to_full_dict_preserves_provenance(self):
        l = EvidenceLedger()
        l.add("logs", [{"x": 1}], source=EvidenceSource.WORKER, kind=EvidenceKind.LOGS)
        snap = l.snapshot()
        full = snap.to_full_dict()
        assert full["logs"]["value"] == [{"x": 1}]
        assert full["logs"]["source"] == "worker"
        assert full["logs"]["kind"] == "logs"

    def test_snapshot_keys(self):
        l = EvidenceLedger()
        l.add("a", 1)
        l.add("b", 2)
        snap = l.snapshot()
        assert snap.keys() == ["a", "b"]


# ---------------------------------------------------------------------------
# merge_dict
# ---------------------------------------------------------------------------

class TestMergeDict:
    def test_merge_adds_new_keys(self):
        l = EvidenceLedger()
        l.add("a", 1)
        l.merge_dict({"b": 2, "c": 3})
        assert l.to_dict() == {"a": 1, "b": 2, "c": 3}

    def test_merge_replaces_existing_keys(self):
        l = EvidenceLedger()
        l.add("a", 1)
        l.merge_dict({"a": 99, "b": 2})
        assert l.get("a") == 99
        assert l.get("b") == 2


# ---------------------------------------------------------------------------
# remove / clear
# ---------------------------------------------------------------------------

class TestRemoveAndClear:
    def test_remove_returns_true_when_present(self):
        l = EvidenceLedger()
        l.add("x", 1)
        assert l.remove("x") is True
        assert not l.has("x")

    def test_remove_returns_false_when_absent(self):
        l = EvidenceLedger()
        assert l.remove("nope") is False

    def test_clear_empties_ledger(self):
        l = EvidenceLedger()
        l.add("a", 1)
        l.add("b", 2)
        l.clear()
        assert len(l) == 0


# ---------------------------------------------------------------------------
# EvidenceItem serialization
# ---------------------------------------------------------------------------

class TestEvidenceItemSerialization:
    def test_to_dict_includes_all_fields(self):
        item = EvidenceItem(
            key="logs", value=[1, 2, 3],
            source=EvidenceSource.WORKER,
            kind=EvidenceKind.LOGS,
            confidence=0.8,
            metadata={"k": "v"},
            timestamp=1234.5,
        )
        d = item.to_dict()
        assert d["key"] == "logs"
        assert d["value"] == [1, 2, 3]
        assert d["source"] == "worker"
        assert d["kind"] == "logs"
        assert d["confidence"] == 0.8
        assert d["metadata"] == {"k": "v"}
        assert d["timestamp"] == 1234.5

    def test_from_dict_roundtrip(self):
        original = EvidenceItem(
            key="x", value={"a": 1},
            source=EvidenceSource.POST_PROCESSING,
            kind=EvidenceKind.PROVENANCE,
            confidence=0.5,
            metadata={"m": "n"},
            timestamp=42.0,
        )
        restored = EvidenceItem.from_dict(original.to_dict())
        assert restored == original

    def test_from_dict_with_defaults(self):
        item = EvidenceItem.from_dict({"key": "x", "value": 1})
        assert item.source == EvidenceSource.UNKNOWN
        assert item.kind == EvidenceKind.OTHER


# ---------------------------------------------------------------------------
# Full-dict round-trip (with provenance)
# ---------------------------------------------------------------------------

class TestFullDictRoundTrip:
    def test_full_roundtrip_preserves_provenance(self):
        l = EvidenceLedger()
        l.add("logs", [1, 2], source=EvidenceSource.WORKER, kind=EvidenceKind.LOGS)
        l.add("_meta", "x", source=EvidenceSource.POST_PROCESSING,
              kind=EvidenceKind.PROVENANCE, confidence=0.7)

        full = l.to_full_dict()
        restored = EvidenceLedger.from_full_dict(full)

        assert restored.to_dict() == l.to_dict()
        assert restored.get_item("logs").source == EvidenceSource.WORKER
        assert restored.get_item("logs").kind == EvidenceKind.LOGS
        assert restored.get_item("_meta").confidence == 0.7


# ---------------------------------------------------------------------------
# Adapter — kind / source inference
# ---------------------------------------------------------------------------

class TestKindInference:
    @pytest.mark.parametrize("key, expected", [
        ("logs",               EvidenceKind.LOGS),
        ("log_data",           EvidenceKind.LOGS),
        ("search_logs",        EvidenceKind.LOGS),
        ("metrics",            EvidenceKind.METRICS),
        ("query_metrics",      EvidenceKind.METRICS),
        ("get_golden_signals", EvidenceKind.GOLDEN_SIGNALS),
        ("get_events",         EvidenceKind.EVENTS),
        ("get_change_data",    EvidenceKind.CHANGES),
        ("itsm_context",       EvidenceKind.ITSM),
        ("devops_context",     EvidenceKind.DEVOPS),
        ("confluence_context", EvidenceKind.CONFLUENCE),
        ("historical_context", EvidenceKind.HISTORICAL),
        ("trace_correlation",  EvidenceKind.APM),
        ("network_evidence",   EvidenceKind.NETWORK),
        # underscore key → provenance
        ("_incident_type",     EvidenceKind.PROVENANCE),
        ("_raw_diff",          EvidenceKind.PROVENANCE),
        # unknown key → worker_result
        ("custom_step_label",  EvidenceKind.WORKER_RESULT),
    ])
    def test_infer_kind(self, key, expected):
        assert infer_kind_for_key(key) == expected


class TestSourceInference:
    def test_underscore_keys_are_post_processing(self):
        assert infer_source_for_key("_incident_type") == EvidenceSource.POST_PROCESSING
        assert infer_source_for_key("_raw_diff") == EvidenceSource.POST_PROCESSING

    def test_other_keys_default_to_worker(self):
        assert infer_source_for_key("logs") == EvidenceSource.WORKER
        assert infer_source_for_key("custom_step") == EvidenceSource.WORKER


class TestAdapterFunctions:
    def test_dict_to_ledger_infers_provenance_by_default(self):
        d = {"logs": [], "_incident_type": "oom"}
        l = dict_to_ledger(d)
        assert l.get_item("logs").kind == EvidenceKind.LOGS
        assert l.get_item("logs").source == EvidenceSource.WORKER
        assert l.get_item("_incident_type").kind == EvidenceKind.PROVENANCE
        assert l.get_item("_incident_type").source == EvidenceSource.POST_PROCESSING

    def test_dict_to_ledger_can_skip_inference(self):
        l = dict_to_ledger({"logs": []}, infer_provenance=False)
        assert l.get_item("logs").kind == EvidenceKind.OTHER
        assert l.get_item("logs").source == EvidenceSource.UNKNOWN

    def test_ledger_to_dict(self):
        l = EvidenceLedger()
        l.add("a", 1)
        l.add("b", {"nested": True})
        assert ledger_to_dict(l) == {"a": 1, "b": {"nested": True}}


# ---------------------------------------------------------------------------
# Equality
# ---------------------------------------------------------------------------

class TestEquality:
    def test_equal_ledgers_compare_equal(self):
        a = EvidenceLedger()
        a.add("x", 1)
        b = EvidenceLedger()
        b.add("x", 1, source=EvidenceSource.WORKER)  # different provenance
        # Equality is VALUE-based, ignores provenance & timestamps
        assert a == b

    def test_different_values_compare_unequal(self):
        a = EvidenceLedger()
        a.add("x", 1)
        b = EvidenceLedger()
        b.add("x", 2)
        assert a != b

    def test_ledger_not_equal_to_dict(self):
        l = EvidenceLedger()
        l.add("x", 1)
        # Not equal to a plain dict — NotImplemented falls back to False
        assert (l == {"x": 1}) is False


# ---------------------------------------------------------------------------
# Shadow flag
# ---------------------------------------------------------------------------

class TestShadowFlag:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv(SHADOW_ENV_VAR, raising=False)
        assert is_shadow_enabled() is False

    @pytest.mark.parametrize("val", ["true", "TRUE", "1", "yes", "on", "True"])
    def test_enabled_by_truthy_values(self, monkeypatch, val):
        monkeypatch.setenv(SHADOW_ENV_VAR, val)
        assert is_shadow_enabled() is True

    @pytest.mark.parametrize("val", ["false", "0", "no", "off", "", "random"])
    def test_disabled_by_falsy_values(self, monkeypatch, val):
        monkeypatch.setenv(SHADOW_ENV_VAR, val)
        assert is_shadow_enabled() is False


# ---------------------------------------------------------------------------
# Import-cycle guard
# ---------------------------------------------------------------------------

class TestImportCycleGuard:
    def test_evidence_module_has_no_supervisor_deps(self):
        for modname in (
            "sentinel_core.evidence.ledger",
            "sentinel_core.evidence.adapter",
            "sentinel_core.evidence.shadow",
        ):
            import importlib
            mod = importlib.import_module(modname)
            src = open(mod.__file__).read()
            for forbidden in (
                "from supervisor", "import supervisor",
                "from intelligence", "import intelligence",
                "from workers", "import workers",
                "from agui", "import agui",
            ):
                assert forbidden not in src, (
                    f"{modname} must not depend on {forbidden!r}"
                )


# ---------------------------------------------------------------------------
# Supervisor agent.py is NOT yet wired
# ---------------------------------------------------------------------------

class TestNoLiveWiring:
    """Phase 8 must NOT touch agent.py. Verify the file does not import the
    ledger module yet — that integration is the next phase's work."""

    def test_agent_does_not_import_evidence_ledger(self):
        import supervisor.agent as agent_mod
        src = open(agent_mod.__file__).read()
        assert "sentinel_core.evidence" not in src

    def test_existing_evidence_dict_pattern_still_works(self):
        """Sanity check: the supervisor's evidence dict pattern still works.
        A plain dict accepts the same operations the supervisor performs."""
        evidence: dict = {}
        evidence["logs"] = [{"line": "x"}]
        evidence["_incident_type"] = "oom"
        evidence["devops_context"] = {"deployments": []}
        assert evidence.get("logs") == [{"line": "x"}]
        assert evidence.get("missing", "fallback") == "fallback"
        assert "_incident_type" in evidence


# ---------------------------------------------------------------------------
# Real evidence dict shape (smoke test using realistic keys)
# ---------------------------------------------------------------------------

class TestRealisticEvidenceShape:
    def test_round_trip_realistic_evidence(self):
        """Round-trip a dict shaped like one the supervisor actually produces."""
        evidence = {
            "search_logs": {"logs": [{"line": "ERROR conn refused"}], "log_count": 1},
            "query_metrics": {"metrics": {"cpu": 0.9}, "pattern": "spike"},
            "get_golden_signals": {
                "metrics": {"errors": 99}, "anomaly_detected": True,
            },
            "get_events": [{"event": "deployment", "ts": 100}],
            "get_change_data": [{"id": "CHG1", "service": "checkout"}],
            "itsm_context": {"tickets": []},
            "devops_context": {
                "deployments": [{"id": "d1"}],
                "workflow_runs": [{"id": "r1"}],
            },
            "confluence_context": {"runbooks": []},
            "diff_analysis": {"hunks": []},
            "trace_correlation": {"traces": []},
            "_incident_type": "error_spike",
            "_suggested_root_causes": ["deploy at 10:00"],
            "_tool_recommendations": {"log_worker": 0.9},
            "_past_experiences": [{"id": "exp1"}],
            "_kg_similar_incidents": [{"id": "INC123"}],
            "_itsm_change_correlations": [{"change_id": "CHG1", "score": 0.8}],
            "_loop_escalation": {"reason": "stuck"},
            "_raw_diff": "diff --git a/x b/y",
        }
        assert round_trip(evidence) == evidence
