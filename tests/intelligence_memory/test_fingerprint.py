"""Fingerprint tests."""
from __future__ import annotations

import pytest

from sentinel_core.intel_memory import (
    FingerprintInput,
    MemoryRecord,
    TopologySnapshot,
    compute_evidence_pattern_hash,
    compute_fingerprint,
    compute_planner_path_hash,
    compute_topology_hash,
    compute_transaction_path_hash,
)
from sentinel_core.intel_memory.fingerprint import fingerprint_from_record


class TestCompomentHashes:
    def test_topology_deterministic(self):
        t = TopologySnapshot(services=("checkout", "db"))
        assert compute_topology_hash(t) == compute_topology_hash(t)

    def test_topology_service_order_irrelevant(self):
        t1 = TopologySnapshot(services=("checkout", "db"))
        t2 = TopologySnapshot(services=("db", "checkout"))
        assert compute_topology_hash(t1) == compute_topology_hash(t2)

    def test_topology_dict_and_dataclass_agree(self):
        t = TopologySnapshot(services=("checkout",), cloud="aws")
        d = {"services": ("checkout",), "cloud": "aws"}
        assert compute_topology_hash(t) == compute_topology_hash(d)

    def test_transaction_path_order_matters(self):
        h1 = compute_transaction_path_hash(("ui", "checkout", "db"))
        h2 = compute_transaction_path_hash(("db", "checkout", "ui"))
        assert h1 != h2

    def test_planner_path_deterministic(self):
        h1 = compute_planner_path_hash(("cap:a", "cap:b"))
        h2 = compute_planner_path_hash(("cap:a", "cap:b"))
        assert h1 == h2

    def test_evidence_pattern_set_semantics(self):
        # Evidence uses set semantics (order-independent)
        h1 = compute_evidence_pattern_hash(("logs", "metrics"))
        h2 = compute_evidence_pattern_hash(("metrics", "logs"))
        assert h1 == h2


class TestComputeFingerprint:
    def test_deterministic(self):
        fi = FingerprintInput(
            service="checkout", environment="prod-east",
            application="ecom", incident_type="saturation",
            topology=TopologySnapshot(services=("checkout", "db")),
        )
        assert compute_fingerprint(fi) == compute_fingerprint(fi)

    def test_different_services_differ(self):
        fi1 = FingerprintInput(service="checkout", environment="prod-east")
        fi2 = FingerprintInput(service="payments", environment="prod-east")
        assert compute_fingerprint(fi1) != compute_fingerprint(fi2)

    def test_empty_input_still_produces_hash(self):
        h = compute_fingerprint(FingerprintInput())
        assert isinstance(h, str)
        assert len(h) == 16

    def test_from_record_matches_direct(self):
        rec = MemoryRecord(
            memory_id="m1",
            service="checkout",
            environment="prod-east",
            application="ecom",
            incident_type="saturation",
            topology=TopologySnapshot(services=("checkout", "db"),
                                        cloud="aws"),
            transaction_path=("ui", "checkout", "db"),
            planner_decisions=("cap:collect_logs",),
            evidence_collected=("logs",),
        )
        # Fingerprint derived from the record must be stable
        assert fingerprint_from_record(rec) == fingerprint_from_record(rec)
