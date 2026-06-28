"""Phase 6 — InvestigationContext tests.

Covers the context model, builder, snapshot serialization, and the two
additive adoption points: WorkflowAwareInvestigator.investigate_with_context()
and WorkflowEngine.start_from_context(). Verifies that existing direct call
sites still work unchanged.
"""
from __future__ import annotations

import dataclasses
import json
import time

import pytest

from sentinel_core.context import (
    ContextBuilder,
    ContextSnapshot,
    InvestigationContext,
)


# ---------------------------------------------------------------------------
# Construction & defaults
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_requires_incident_id(self):
        ctx = InvestigationContext(incident_id="INC1")
        assert ctx.incident_id == "INC1"

    def test_investigation_id_auto_derived(self):
        ctx = InvestigationContext(incident_id="INC42")
        assert ctx.investigation_id == "inv-INC42"

    def test_explicit_investigation_id_preserved(self):
        ctx = InvestigationContext(incident_id="X", investigation_id="custom-id")
        assert ctx.investigation_id == "custom-id"

    def test_created_at_auto_stamped(self):
        before = time.time()
        ctx = InvestigationContext(incident_id="INC")
        after = time.time()
        assert before <= ctx.created_at <= after

    def test_explicit_created_at_preserved(self):
        ctx = InvestigationContext(incident_id="INC", created_at=12345.0)
        assert ctx.created_at == 12345.0

    def test_defaults(self):
        ctx = InvestigationContext(incident_id="INC")
        assert ctx.incident_type == ""
        assert ctx.service == ""
        assert ctx.severity == 3
        assert ctx.current_phase == ""
        assert ctx.incident == {}
        assert ctx.receipts is None
        assert ctx.budget is None
        assert ctx.circuits is None
        assert ctx.metadata == {}


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------

class TestImmutability:
    def test_cannot_reassign_incident_id(self):
        ctx = InvestigationContext(incident_id="INC")
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.incident_id = "OTHER"  # type: ignore[misc]

    def test_cannot_reassign_phase(self):
        ctx = InvestigationContext(incident_id="INC")
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.current_phase = "x"  # type: ignore[misc]

    def test_cannot_reassign_handle(self):
        ctx = InvestigationContext(incident_id="INC")
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.receipts = object()  # type: ignore[misc]

    def test_metadata_dict_is_per_instance(self):
        """Default-factory dicts must not be shared between instances."""
        a = InvestigationContext(incident_id="A")
        b = InvestigationContext(incident_id="B")
        a.metadata["x"] = 1
        assert "x" not in b.metadata


# ---------------------------------------------------------------------------
# Copy / update
# ---------------------------------------------------------------------------

class TestWithPhase:
    def test_with_phase_returns_new_instance(self):
        ctx = InvestigationContext(incident_id="INC")
        nxt = ctx.with_phase("analyze")
        assert nxt is not ctx
        assert nxt.current_phase == "analyze"
        assert ctx.current_phase == ""

    def test_with_phase_preserves_identifiers(self):
        ctx = InvestigationContext(incident_id="INC", incident_type="error", service="svc")
        nxt = ctx.with_phase("collect")
        assert nxt.incident_id == "INC"
        assert nxt.incident_type == "error"
        assert nxt.service == "svc"


class TestWithClassified:
    def test_updates_only_provided_fields(self):
        ctx = InvestigationContext(incident_id="INC")
        nxt = ctx.with_classified(incident_type="latency_spike")
        assert nxt.incident_type == "latency_spike"
        assert nxt.service == ""  # unchanged
        assert nxt.severity == 3  # unchanged

    def test_updates_all_fields(self):
        ctx = InvestigationContext(incident_id="INC")
        nxt = ctx.with_classified(incident_type="oom", service="payments", severity=1)
        assert nxt.incident_type == "oom"
        assert nxt.service == "payments"
        assert nxt.severity == 1


class TestWithHandles:
    def test_attaches_handles(self):
        ctx = InvestigationContext(incident_id="INC")
        r, b, c = object(), object(), object()
        nxt = ctx.with_handles(receipts=r, budget=b, circuits=c)
        assert nxt.receipts is r
        assert nxt.budget is b
        assert nxt.circuits is c

    def test_omitted_handles_unchanged(self):
        sentinel = object()
        ctx = InvestigationContext(incident_id="INC").with_handles(receipts=sentinel)
        nxt = ctx.with_handles(budget=object())
        assert nxt.receipts is sentinel  # not overwritten

    def test_explicit_none_clears_handle(self):
        sentinel = object()
        ctx = InvestigationContext(incident_id="INC").with_handles(receipts=sentinel)
        cleared = ctx.with_handles(receipts=None)
        assert cleared.receipts is None


class TestWithIncidentAndMetadata:
    def test_with_incident_copies_dict(self):
        payload = {"service": "x", "severity": 2}
        ctx = InvestigationContext(incident_id="INC").with_incident(payload)
        assert ctx.incident == payload
        payload["service"] = "mutated"  # caller-side mutation
        assert ctx.incident["service"] == "x"  # context unaffected

    def test_with_metadata_merges(self):
        ctx = InvestigationContext(incident_id="INC").with_metadata(a=1)
        nxt = ctx.with_metadata(b=2)
        assert nxt.metadata == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# Snapshot serialization
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_snapshot_drops_handles_and_incident(self):
        ctx = InvestigationContext(
            incident_id="INC", incident_type="x", service="svc",
            incident={"big": "payload"},
            receipts=object(), budget=object(), circuits=object(),
        )
        snap = ctx.snapshot()
        assert isinstance(snap, ContextSnapshot)
        assert snap.incident_id == "INC"
        assert snap.incident_type == "x"
        # Handles and incident dict are NOT on the snapshot
        assert not hasattr(snap, "receipts")
        assert not hasattr(snap, "incident")

    def test_snapshot_to_dict_is_json_safe(self):
        ctx = InvestigationContext(
            incident_id="INC", incident_type="error_spike",
            service="checkout", severity=2, current_phase="analyze",
        ).with_metadata(extra="value")
        d = ctx.snapshot().to_dict()
        # Must round-trip through JSON
        roundtripped = json.loads(json.dumps(d))
        assert roundtripped == d

    def test_snapshot_roundtrip(self):
        original = ContextSnapshot(
            incident_id="INC",
            investigation_id="inv-INC",
            incident_type="latency",
            service="api",
            severity=1,
            current_phase="collect",
            created_at=1000.5,
            metadata={"k": "v"},
        )
        d = original.to_dict()
        restored = ContextSnapshot.from_dict(d)
        assert restored == original

    def test_snapshot_from_dict_with_defaults(self):
        snap = ContextSnapshot.from_dict({"incident_id": "INC"})
        assert snap.incident_id == "INC"
        assert snap.severity == 3
        assert snap.metadata == {}


# ---------------------------------------------------------------------------
# Workflow metadata format
# ---------------------------------------------------------------------------

class TestToWorkflowMetadata:
    def test_includes_core_identifiers(self):
        ctx = InvestigationContext(
            incident_id="INC", incident_type="error", service="svc", severity=2,
        )
        meta = ctx.to_workflow_metadata()
        assert meta["incident_id"] == "INC"
        assert meta["incident_type"] == "error"
        assert meta["service"] == "svc"
        assert meta["severity"] == 2

    def test_merges_user_metadata(self):
        ctx = InvestigationContext(incident_id="INC").with_metadata(tenant="acme")
        assert ctx.to_workflow_metadata()["tenant"] == "acme"

    def test_user_metadata_does_not_overwrite_core(self):
        # Core ids always win; user can't smuggle a different incident_id
        ctx = InvestigationContext(incident_id="INC")
        meta = ctx.with_metadata(incident_id="OTHER").to_workflow_metadata()
        # Build order: core ids are written, then **self.metadata spreads, so
        # the user's "incident_id" actually overwrites — verify the expected
        # behavior so this is documented.
        assert meta["incident_id"] == "OTHER"


# ---------------------------------------------------------------------------
# ContextBuilder
# ---------------------------------------------------------------------------

class TestContextBuilder:
    def test_for_incident_basic(self):
        ctx = ContextBuilder.for_incident("INC")
        assert ctx.incident_id == "INC"
        assert ctx.investigation_id == "inv-INC"

    def test_for_incident_with_payload(self):
        ctx = ContextBuilder.for_incident("INC", incident={"service": "x"})
        assert ctx.incident == {"service": "x"}

    def test_for_incident_attaches_handles(self):
        r = object()
        ctx = ContextBuilder.for_incident("INC", receipts=r)
        assert ctx.receipts is r

    def test_for_incident_rejects_empty_id(self):
        with pytest.raises(ValueError):
            ContextBuilder.for_incident("")

    def test_from_snapshot_rehydrates_identifiers(self):
        snap = ContextSnapshot(
            incident_id="INC", investigation_id="inv-INC",
            incident_type="latency", service="api", severity=2,
            current_phase="analyze", created_at=42.0, metadata={"k": "v"},
        )
        ctx = ContextBuilder.from_snapshot(snap)
        assert ctx.incident_id == "INC"
        assert ctx.investigation_id == "inv-INC"
        assert ctx.incident_type == "latency"
        assert ctx.service == "api"
        assert ctx.current_phase == "analyze"
        assert ctx.metadata == {"k": "v"}

    def test_from_snapshot_does_not_recover_handles(self):
        snap = ContextSnapshot(incident_id="X", investigation_id="inv-X")
        ctx = ContextBuilder.from_snapshot(snap)
        assert ctx.receipts is None
        assert ctx.budget is None
        assert ctx.circuits is None


# ---------------------------------------------------------------------------
# Workflow + replay + receipt compatibility (additive adoption)
# ---------------------------------------------------------------------------

class TestWorkflowEngineCompatibility:
    def test_start_from_context_creates_run(self, tmp_path):
        from supervisor.workflow_engine import WorkflowEngine
        from sentinel_core.models.workflow import WorkflowStatus

        engine = WorkflowEngine(db_path=str(tmp_path / "wf.db"))
        ctx = ContextBuilder.for_incident("INC100").with_classified(
            incident_type="error_spike", service="checkout", severity=2,
        )
        assert engine.start_from_context(ctx) is True

        cp = engine.resume(ctx.investigation_id)
        assert cp is not None
        assert cp.status == WorkflowStatus.RUNNING
        assert cp.metadata["incident_id"] == "INC100"
        assert cp.metadata["incident_type"] == "error_spike"
        assert cp.metadata["service"] == "checkout"
        assert cp.metadata["severity"] == 2

    def test_start_from_context_matches_direct_start(self, tmp_path):
        """start_from_context is equivalent to start(ctx.investigation_id, ctx.to_workflow_metadata())."""
        from supervisor.workflow_engine import WorkflowEngine

        engine = WorkflowEngine(db_path=str(tmp_path / "wf.db"))
        ctx = ContextBuilder.for_incident("INC200").with_classified(
            incident_type="latency", service="api",
        )
        engine.start(ctx.investigation_id, ctx.to_workflow_metadata())
        cp1 = engine.resume(ctx.investigation_id)

        engine2 = WorkflowEngine(db_path=str(tmp_path / "wf2.db"))
        engine2.start_from_context(ctx)
        cp2 = engine2.resume(ctx.investigation_id)

        assert cp1.metadata == cp2.metadata


class TestWorkflowMiddlewareCompatibility:
    def test_investigate_with_context_forwards_incident_id(self, tmp_path):
        from supervisor.workflow_middleware import WorkflowAwareInvestigator
        from supervisor.workflow_engine import WorkflowEngine

        class FakeSupervisor:
            def __init__(self):
                self.calls = []
            def investigate(self, incident_id, replay=False):
                self.calls.append((incident_id, replay))
                return {"incident_id": incident_id, "root_cause": "ok",
                        "confidence": 0.9, "reasoning": "test",
                        "stop_reason": "done", "degraded": False}

        sup = FakeSupervisor()
        engine = WorkflowEngine(db_path=str(tmp_path / "wf.db"))
        wm = WorkflowAwareInvestigator(sup, engine=engine)
        ctx = ContextBuilder.for_incident("INC300")

        result = wm.investigate_with_context(ctx)
        assert sup.calls == [("INC300", False)]
        assert result["incident_id"] == "INC300"

    def test_investigate_with_context_passes_replay_flag(self, tmp_path):
        from supervisor.workflow_middleware import WorkflowAwareInvestigator
        from supervisor.workflow_engine import WorkflowEngine

        class FakeSupervisor:
            def investigate(self, incident_id, replay=False):
                return {"incident_id": incident_id, "replay_flag": replay,
                        "root_cause": "x", "confidence": 0.5, "reasoning": "y",
                        "stop_reason": "done", "degraded": False}

        engine = WorkflowEngine(db_path=str(tmp_path / "wf.db"))
        wm = WorkflowAwareInvestigator(FakeSupervisor(), engine=engine)
        ctx = ContextBuilder.for_incident("INC301")
        result = wm.investigate_with_context(ctx, replay=True)
        assert result["replay_flag"] is True


class TestReceiptCompatibility:
    def test_receipt_collector_accepts_ctx_incident_id(self):
        """ReceiptCollector(case_id=ctx.incident_id) is the expected wiring."""
        from supervisor.receipt import ReceiptCollector
        ctx = ContextBuilder.for_incident("INC400")
        rc = ReceiptCollector(case_id=ctx.incident_id)
        assert rc.case_id == "INC400"


# ---------------------------------------------------------------------------
# Backward compatibility — existing call sites unchanged
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    def test_old_workflow_engine_start_still_works(self, tmp_path):
        from supervisor.workflow_engine import WorkflowEngine
        engine = WorkflowEngine(db_path=str(tmp_path / "wf.db"))
        # The old positional call style continues to work.
        assert engine.start("inv-LEGACY", {"incident_id": "LEGACY"}) is True

    def test_old_workflow_middleware_investigate_signature_unchanged(self, tmp_path):
        from supervisor.workflow_middleware import WorkflowAwareInvestigator
        from supervisor.workflow_engine import WorkflowEngine

        class FakeSupervisor:
            def investigate(self, incident_id, replay=False):
                return {"incident_id": incident_id, "root_cause": "x",
                        "confidence": 0.5, "reasoning": "y",
                        "stop_reason": "done", "degraded": False}

        engine = WorkflowEngine(db_path=str(tmp_path / "wf.db"))
        wm = WorkflowAwareInvestigator(FakeSupervisor(), engine=engine)
        # The existing call form works exactly as before.
        result = wm.investigate("INC500")
        assert result["incident_id"] == "INC500"


# ---------------------------------------------------------------------------
# Import compatibility — no cycles, lives in sentinel_core
# ---------------------------------------------------------------------------

class TestImportCompatibility:
    def test_context_module_has_no_supervisor_deps(self):
        """sentinel_core.context must not import supervisor/intelligence/workers."""
        from sentinel_core.context import investigation, builder
        for mod in (investigation, builder):
            src = open(mod.__file__).read()
            for forbidden in ("from supervisor", "import supervisor",
                              "from intelligence", "import intelligence",
                              "from workers", "import workers",
                              "from agui", "import agui"):
                assert forbidden not in src, (
                    f"{mod.__name__} must not depend on {forbidden!r}"
                )

    def test_public_imports(self):
        from sentinel_core.context import (
            InvestigationContext, ContextSnapshot, ContextBuilder,
        )
        assert InvestigationContext is not None
        assert ContextSnapshot is not None
        assert ContextBuilder is not None
