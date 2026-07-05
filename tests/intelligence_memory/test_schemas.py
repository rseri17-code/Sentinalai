"""Schema tests for Incident Intelligence Memory."""
from __future__ import annotations

import json

import pytest

from sentinel_core.intel_memory import (
    MEMORY_SCHEMA_VERSION,
    BlastRadiusSnapshot,
    MemoryRecord,
    RecurringPattern,
    RecurringPatternKind,
    SimilarityScore,
    TopologySnapshot,
)


class TestMemoryRecord:
    def test_defaults_construct(self):
        r = MemoryRecord(memory_id="m1")
        assert r.memory_id == "m1"
        assert r.confidence == 0
        assert r.schema_version == MEMORY_SCHEMA_VERSION

    def test_frozen(self):
        r = MemoryRecord(memory_id="m1")
        with pytest.raises(Exception):
            r.memory_id = "m2"

    def test_to_dict_json_safe(self):
        r = MemoryRecord(
            memory_id="m1",
            evidence_collected=("logs", "metrics"),
            planner_decisions=("cap:collect_logs",),
        )
        d = r.to_dict()
        # tuples are lists in the dict output
        assert d["evidence_collected"] == ["logs", "metrics"]
        assert d["planner_decisions"] == ["cap:collect_logs"]
        json.dumps(d)

    def test_from_dict_roundtrip(self):
        original = MemoryRecord(
            memory_id="m1",
            incident_id="INC1",
            service="checkout",
            evidence_collected=("logs",),
            topology=TopologySnapshot(services=("checkout", "db")),
        )
        d = original.to_dict()
        restored = MemoryRecord.from_dict(d)
        assert restored.memory_id == original.memory_id
        assert restored.evidence_collected == original.evidence_collected
        assert restored.topology.services == ("checkout", "db")


class TestTopologySnapshot:
    def test_defaults(self):
        t = TopologySnapshot()
        assert t.services == ()
        assert t.cloud == ""

    def test_frozen(self):
        t = TopologySnapshot()
        with pytest.raises(Exception):
            t.cloud = "aws"


class TestBlastRadiusSnapshot:
    def test_defaults(self):
        b = BlastRadiusSnapshot()
        assert b.severity == "low"
        assert b.total_affected == 0


class TestSimilarityScore:
    def test_to_dict_sorts_breakdown(self):
        s = SimilarityScore(memory_id="m",
                              overall=0.5,
                              breakdown={"b": 0.1, "a": 0.9})
        d = s.to_dict()
        # Sorted keys
        assert list(d["breakdown"].keys()) == ["a", "b"]


class TestRecurringPattern:
    def test_kind_enum_values(self):
        for kind in RecurringPatternKind:
            assert isinstance(kind.value, str)

    def test_frozen(self):
        p = RecurringPattern(kind="x", signature="s", count=1)
        with pytest.raises(Exception):
            p.count = 2
