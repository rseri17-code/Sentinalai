"""Phase 19 — Intelligence Runtime tests.

Runtime scaffold: registration, execution ordering, feature flags,
dependency resolution, failure isolation, receipt metadata, replay
compatibility, regression protection.

The runtime is disabled by default. When disabled, ``run_stage()`` returns
an empty list and no metadata is attached to phase receipts.
"""
from __future__ import annotations

import json

import pytest

from sentinel_core.runtime import (
    IntelligenceRuntime,
    IntelligenceStage,
    ModuleResult,
    ModuleSpec,
    RuntimeContext,
)
from supervisor.intelligence_runtime import (
    RUNTIME_ENV_FLAG,
    build_default_runtime,
    is_runtime_enabled,
)


# ---------------------------------------------------------------------------
# ModuleSpec / RuntimeContext / ModuleResult primitives
# ---------------------------------------------------------------------------

class TestPrimitives:
    def test_module_spec_defaults(self):
        s = ModuleSpec(name="x", stage=IntelligenceStage.POST_FETCH)
        assert s.feature_flag == ""
        assert s.priority == 100
        assert s.dependencies == ()

    def test_runtime_context_defaults(self):
        c = RuntimeContext(investigation_id="inv-1", stage=IntelligenceStage.POST_FETCH)
        assert c.fetch_out is None
        assert c.cres is None
        assert c.cout is None
        assert c.aout is None
        assert c.result is None

    def test_module_result_to_dict_json_safe(self):
        r = ModuleResult(name="x", status="success", elapsed_ms=12.3,
                         warnings=("a", "b"), metadata={"k": "v"})
        d = r.to_dict()
        # JSON round-trip
        assert json.loads(json.dumps(d)) == d
        assert d["warnings"] == ["a", "b"]
        assert d["metadata"] == {"k": "v"}

    def test_stage_enum_values(self):
        assert IntelligenceStage.POST_FETCH.value    == "post_fetch"
        assert IntelligenceStage.POST_CLASSIFY.value == "post_classify"
        assert IntelligenceStage.POST_COLLECT.value  == "post_collect"
        assert IntelligenceStage.POST_ANALYZE.value  == "post_analyze"
        assert IntelligenceStage.POST_PERSIST.value  == "post_persist"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_register_and_lookup(self):
        rt = IntelligenceRuntime(enabled=True)
        rt.register(
            ModuleSpec(name="a", stage=IntelligenceStage.POST_FETCH),
            lambda ctx: {"tag": "hello"},
        )
        assert rt.has("a")
        assert [s.name for s in rt.all_specs()] == ["a"]

    def test_duplicate_registration_rejected(self):
        rt = IntelligenceRuntime(enabled=True)
        spec = ModuleSpec(name="dup", stage=IntelligenceStage.POST_FETCH)
        rt.register(spec, lambda c: {})
        with pytest.raises(ValueError, match="already registered"):
            rt.register(spec, lambda c: {})

    def test_empty_name_rejected(self):
        rt = IntelligenceRuntime(enabled=True)
        with pytest.raises(ValueError):
            rt.register(ModuleSpec(name="", stage=IntelligenceStage.POST_FETCH),
                        lambda c: {})

    def test_non_callable_runner_rejected(self):
        rt = IntelligenceRuntime(enabled=True)
        with pytest.raises(ValueError):
            rt.register(ModuleSpec(name="x", stage=IntelligenceStage.POST_FETCH),
                        "not a callable")

    def test_unregister(self):
        rt = IntelligenceRuntime(enabled=True)
        rt.register(ModuleSpec(name="x", stage=IntelligenceStage.POST_FETCH),
                    lambda c: {})
        assert rt.unregister("x") is True
        assert rt.unregister("x") is False
        assert not rt.has("x")


# ---------------------------------------------------------------------------
# Execution ordering
# ---------------------------------------------------------------------------

class TestOrdering:
    def _stub_recorder(self, log: list) -> callable:
        def _runner(ctx: RuntimeContext, name_capture: str):
            log.append(name_capture)
            return {}
        return _runner

    def test_priority_ordering(self):
        rt = IntelligenceRuntime(enabled=True)
        log: list[str] = []
        rt.register(ModuleSpec(name="b", stage=IntelligenceStage.POST_FETCH, priority=200),
                    lambda c: log.append("b") or {})
        rt.register(ModuleSpec(name="a", stage=IntelligenceStage.POST_FETCH, priority=100),
                    lambda c: log.append("a") or {})
        rt.run_stage(IntelligenceStage.POST_FETCH,
                      RuntimeContext(investigation_id="i", stage=IntelligenceStage.POST_FETCH))
        assert log == ["a", "b"]

    def test_dependency_ordering(self):
        rt = IntelligenceRuntime(enabled=True)
        log: list[str] = []
        # register b first, but b depends on a -> a runs first
        rt.register(ModuleSpec(name="b", stage=IntelligenceStage.POST_FETCH,
                                dependencies=("a",)),
                    lambda c: log.append("b") or {})
        rt.register(ModuleSpec(name="a", stage=IntelligenceStage.POST_FETCH),
                    lambda c: log.append("a") or {})
        rt.run_stage(IntelligenceStage.POST_FETCH,
                      RuntimeContext(investigation_id="i", stage=IntelligenceStage.POST_FETCH))
        assert log == ["a", "b"]

    def test_stage_isolation(self):
        rt = IntelligenceRuntime(enabled=True)
        log: list[str] = []
        rt.register(ModuleSpec(name="f", stage=IntelligenceStage.POST_FETCH),
                    lambda c: log.append("f") or {})
        rt.register(ModuleSpec(name="a", stage=IntelligenceStage.POST_ANALYZE),
                    lambda c: log.append("a") or {})
        rt.run_stage(IntelligenceStage.POST_FETCH,
                      RuntimeContext(investigation_id="i", stage=IntelligenceStage.POST_FETCH))
        # Only POST_FETCH module ran
        assert log == ["f"]

    def test_missing_dependency_is_ignored(self):
        rt = IntelligenceRuntime(enabled=True)
        log: list[str] = []
        rt.register(ModuleSpec(name="a", stage=IntelligenceStage.POST_FETCH,
                                dependencies=("does-not-exist",)),
                    lambda c: log.append("a") or {})
        rt.run_stage(IntelligenceStage.POST_FETCH,
                      RuntimeContext(investigation_id="i", stage=IntelligenceStage.POST_FETCH))
        assert log == ["a"]

    def test_cyclic_dependencies_do_not_deadlock(self):
        rt = IntelligenceRuntime(enabled=True)
        log: list[str] = []
        rt.register(ModuleSpec(name="a", stage=IntelligenceStage.POST_FETCH,
                                dependencies=("b",)),
                    lambda c: log.append("a") or {})
        rt.register(ModuleSpec(name="b", stage=IntelligenceStage.POST_FETCH,
                                dependencies=("a",)),
                    lambda c: log.append("b") or {})
        # Must not raise; cycle broken deterministically
        rt.run_stage(IntelligenceStage.POST_FETCH,
                      RuntimeContext(investigation_id="i", stage=IntelligenceStage.POST_FETCH))
        assert set(log) == {"a", "b"}


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

class TestFeatureFlags:
    def test_master_flag_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv(RUNTIME_ENV_FLAG, raising=False)
        assert is_runtime_enabled() is False
        rt = build_default_runtime()
        assert rt.is_enabled() is False

    @pytest.mark.parametrize("val", ["true", "TRUE", "1", "yes", "on"])
    def test_master_flag_truthy_enables(self, monkeypatch, val):
        monkeypatch.setenv(RUNTIME_ENV_FLAG, val)
        assert is_runtime_enabled() is True
        assert build_default_runtime().is_enabled() is True

    @pytest.mark.parametrize("val", ["", "false", "0", "no", "off"])
    def test_master_flag_falsy_stays_off(self, monkeypatch, val):
        monkeypatch.setenv(RUNTIME_ENV_FLAG, val)
        assert is_runtime_enabled() is False

    def test_disabled_runtime_run_stage_returns_empty(self):
        rt = IntelligenceRuntime(enabled=False)
        rt.register(ModuleSpec(name="x", stage=IntelligenceStage.POST_FETCH),
                    lambda c: {"should": "not-run"})
        results = rt.run_stage(IntelligenceStage.POST_FETCH,
                                RuntimeContext(investigation_id="i",
                                               stage=IntelligenceStage.POST_FETCH))
        assert results == []

    def test_per_module_flag_off_yields_skipped(self, monkeypatch):
        monkeypatch.delenv("MY_MODULE_FLAG", raising=False)
        rt = IntelligenceRuntime(enabled=True)
        rt.register(ModuleSpec(name="gated", stage=IntelligenceStage.POST_FETCH,
                                feature_flag="MY_MODULE_FLAG"),
                    lambda c: {"ran": True})
        results = rt.run_stage(IntelligenceStage.POST_FETCH,
                                RuntimeContext(investigation_id="i",
                                               stage=IntelligenceStage.POST_FETCH))
        assert len(results) == 1
        assert results[0].status == "skipped"
        assert results[0].metadata == {}

    def test_per_module_flag_on_runs(self, monkeypatch):
        monkeypatch.setenv("MY_MODULE_FLAG", "true")
        rt = IntelligenceRuntime(enabled=True)
        rt.register(ModuleSpec(name="gated", stage=IntelligenceStage.POST_FETCH,
                                feature_flag="MY_MODULE_FLAG"),
                    lambda c: {"ran": True})
        results = rt.run_stage(IntelligenceStage.POST_FETCH,
                                RuntimeContext(investigation_id="i",
                                               stage=IntelligenceStage.POST_FETCH))
        assert results[0].status == "success"
        assert results[0].metadata == {"ran": True}


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    def test_module_exception_becomes_failed_status(self):
        rt = IntelligenceRuntime(enabled=True)
        def _boom(ctx):
            raise RuntimeError("kaboom")
        rt.register(ModuleSpec(name="fails", stage=IntelligenceStage.POST_FETCH),
                    _boom)
        results = rt.run_stage(IntelligenceStage.POST_FETCH,
                                RuntimeContext(investigation_id="i",
                                               stage=IntelligenceStage.POST_FETCH))
        assert len(results) == 1
        assert results[0].status == "failed"
        assert results[0].error_type == "RuntimeError"

    def test_failure_does_not_stop_other_modules(self):
        rt = IntelligenceRuntime(enabled=True)
        log: list[str] = []
        def _boom(ctx):
            log.append("boomed")
            raise ValueError("nope")
        rt.register(ModuleSpec(name="a", stage=IntelligenceStage.POST_FETCH, priority=100),
                    _boom)
        rt.register(ModuleSpec(name="b", stage=IntelligenceStage.POST_FETCH, priority=200),
                    lambda c: log.append("ran-b") or {"b": True})
        results = rt.run_stage(IntelligenceStage.POST_FETCH,
                                RuntimeContext(investigation_id="i",
                                               stage=IntelligenceStage.POST_FETCH))
        assert log == ["boomed", "ran-b"]
        assert results[0].status == "failed"
        assert results[1].status == "success"

    def test_run_stage_never_raises_on_arbitrary_runner_bugs(self):
        rt = IntelligenceRuntime(enabled=True)
        # Multiple runners with distinct pathological behaviors
        rt.register(ModuleSpec(name="none-ret", stage=IntelligenceStage.POST_FETCH, priority=10),
                    lambda c: None)
        rt.register(ModuleSpec(name="int-ret", stage=IntelligenceStage.POST_FETCH, priority=20),
                    lambda c: 42)  # not a dict
        rt.register(ModuleSpec(name="raises", stage=IntelligenceStage.POST_FETCH, priority=30),
                    lambda c: (_ for _ in ()).throw(TypeError("boom")))
        # Should complete without propagating
        results = rt.run_stage(IntelligenceStage.POST_FETCH,
                                RuntimeContext(investigation_id="i",
                                               stage=IntelligenceStage.POST_FETCH))
        assert len(results) == 3
        # Non-dict return is coerced to empty metadata
        assert results[1].metadata == {}


# ---------------------------------------------------------------------------
# Timing / telemetry
# ---------------------------------------------------------------------------

class TestTelemetry:
    def test_elapsed_ms_captured(self):
        import time as _time
        rt = IntelligenceRuntime(enabled=True)
        rt.register(ModuleSpec(name="sleepy", stage=IntelligenceStage.POST_FETCH),
                    lambda c: _time.sleep(0.01) or {})
        results = rt.run_stage(IntelligenceStage.POST_FETCH,
                                RuntimeContext(investigation_id="i",
                                               stage=IntelligenceStage.POST_FETCH))
        assert results[0].elapsed_ms >= 5.0  # allow for OS jitter

    def test_skipped_modules_have_zero_elapsed(self, monkeypatch):
        monkeypatch.delenv("SKIP_FLAG", raising=False)
        rt = IntelligenceRuntime(enabled=True)
        rt.register(ModuleSpec(name="s", stage=IntelligenceStage.POST_FETCH,
                                feature_flag="SKIP_FLAG"),
                    lambda c: {})
        results = rt.run_stage(IntelligenceStage.POST_FETCH,
                                RuntimeContext(investigation_id="i",
                                               stage=IntelligenceStage.POST_FETCH))
        assert results[0].status == "skipped"
        assert results[0].elapsed_ms == 0.0


# ---------------------------------------------------------------------------
# Investigate() wiring (agent.py sentinel check)
# ---------------------------------------------------------------------------

class TestAgentWiring:
    def test_agent_imports_runtime(self):
        import supervisor.agent as m
        src = open(m.__file__).read()
        assert "from sentinel_core.runtime import IntelligenceStage, RuntimeContext" in src
        assert "from supervisor.intelligence_runtime import build_default_runtime" in src

    def test_agent_has_hook_helper(self):
        import supervisor.agent as m
        src = open(m.__file__).read()
        assert "_intel_hook" in src
        # And five stage invocations, one per phase
        for stage in ("POST_FETCH", "POST_CLASSIFY", "POST_COLLECT",
                      "POST_ANALYZE", "POST_PERSIST"):
            assert f"IntelligenceStage.{stage}" in src

    def test_investigate_signature_unchanged(self):
        from supervisor.agent import SentinalAISupervisor
        import inspect
        sig = inspect.signature(SentinalAISupervisor.investigate)
        assert list(sig.parameters.keys()) == ["self", "incident_id", "replay"]


# ---------------------------------------------------------------------------
# End-to-end: registered module lifts metadata onto phase receipt
# ---------------------------------------------------------------------------

class TestReceiptIntegration:
    """Verify the receipt-metadata lift via PhaseReceiptCollector directly.
    This mirrors the block agent.py inlines around each phase call and
    avoids depending on the full pipeline surviving CollectPhase gates."""

    def test_module_result_lifted_to_receipt_metadata(self):
        from supervisor.phase_receipts import PhaseReceiptCollector
        rt = IntelligenceRuntime(enabled=True)
        rt.register(ModuleSpec(name="test-mod", stage=IntelligenceStage.POST_FETCH),
                    lambda c: {"tag": "hello"})
        col = PhaseReceiptCollector()
        with col.record("fetch") as _r:
            # Mirror the agent.py hook block
            results = rt.run_stage(
                IntelligenceStage.POST_FETCH,
                RuntimeContext(investigation_id="inv-x",
                               stage=IntelligenceStage.POST_FETCH),
            )
            if results:
                _r.metadata["intelligence"] = [r.to_dict() for r in results]
        receipt = col.to_list()[0]
        assert "intelligence" in receipt["metadata"]
        arr = receipt["metadata"]["intelligence"]
        assert len(arr) == 1
        assert arr[0]["name"] == "test-mod"
        assert arr[0]["status"] == "success"
        assert arr[0]["metadata"] == {"tag": "hello"}

    def test_disabled_runtime_leaves_receipt_clean(self):
        from supervisor.phase_receipts import PhaseReceiptCollector
        rt = IntelligenceRuntime(enabled=False)
        rt.register(ModuleSpec(name="test-mod", stage=IntelligenceStage.POST_FETCH),
                    lambda c: {"tag": "hello"})
        col = PhaseReceiptCollector()
        with col.record("fetch") as _r:
            results = rt.run_stage(
                IntelligenceStage.POST_FETCH,
                RuntimeContext(investigation_id="inv-x",
                               stage=IntelligenceStage.POST_FETCH),
            )
            if results:
                _r.metadata["intelligence"] = [r.to_dict() for r in results]
        receipt = col.to_list()[0]
        assert "intelligence" not in receipt["metadata"]


# ---------------------------------------------------------------------------
# Regression protection — no default registrations, no behavior change
# ---------------------------------------------------------------------------

class TestNoDefaultRegistrations:
    def test_default_runtime_has_no_modules(self, monkeypatch):
        monkeypatch.setenv(RUNTIME_ENV_FLAG, "true")
        rt = build_default_runtime()
        assert rt.all_specs() == []

    def test_disabled_default_runtime_is_zero_cost(self, monkeypatch):
        monkeypatch.delenv(RUNTIME_ENV_FLAG, raising=False)
        rt = build_default_runtime()
        # Even if we somehow called run_stage on it, it returns []
        assert rt.run_stage(IntelligenceStage.POST_FETCH,
                             RuntimeContext(investigation_id="i",
                                            stage=IntelligenceStage.POST_FETCH)) == []


# ---------------------------------------------------------------------------
# Replay compatibility
# ---------------------------------------------------------------------------

class TestReplayCompatibility:
    def test_replay_short_circuit_bypasses_runtime(self, tmp_path, monkeypatch):
        """Replay short-circuits before the phase receipt collector is
        constructed AND before the runtime hook helper is defined. Confirm
        the runtime hook never runs on the replay path."""
        monkeypatch.setenv(RUNTIME_ENV_FLAG, "true")

        from unittest.mock import MagicMock, Mock
        from supervisor.agent import SentinalAISupervisor
        from supervisor.replay import ReplayStore

        replay_dir = tmp_path / "replay"
        replay_dir.mkdir()
        store = ReplayStore(replay_dir=str(replay_dir))
        (replay_dir / "INC_R_20260703T100000Z.json").write_text(json.dumps({
            "case_id": "INC_R",
            "result": {"root_cause": "cached", "confidence": 88,
                        "evidence_timeline": [], "reasoning": "cached"},
            "evidence": {},
        }))

        supervisor = SentinalAISupervisor()
        supervisor._replay_store = store
        for name in supervisor.workers:
            supervisor.workers[name] = MagicMock()
            supervisor.workers[name].execute = Mock(side_effect=lambda a, p: {})
        result = supervisor.investigate("INC_R", replay=True)
        # No receipts key -> no phase receipts collected -> no runtime hook
        assert "_phase_receipts" not in result
