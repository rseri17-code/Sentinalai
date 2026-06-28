"""Phase 5 — service registry tests.

Covers the registry primitives (sentinel_core.service_registry) and the
concrete wiring (supervisor.services). Verifies that the registry is purely
additive: existing direct-import singleton paths still work.
"""
from __future__ import annotations

import pytest

from sentinel_core.service_registry import (
    ServiceHealth,
    ServiceLifecycle,
    ServiceNotFoundError,
    ServiceRegistry,
    get_registry,
    reset_registry_for_tests,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def reg() -> ServiceRegistry:
    """Fresh in-memory registry, isolated from the module-level singleton."""
    return ServiceRegistry()


@pytest.fixture(autouse=True)
def _reset_global_registry():
    """Ensure the module-level singleton is fresh per test."""
    reset_registry_for_tests()
    # Also reset the wired flag on supervisor.services so each test re-wires
    import supervisor.services as svc
    svc._wired = False
    yield
    reset_registry_for_tests()
    svc._wired = False


# ---------------------------------------------------------------------------
# Registry primitives
# ---------------------------------------------------------------------------

class TestRegisterAndGet:
    def test_register_and_get_returns_factory_output(self, reg):
        reg.register("a", lambda: {"v": 1})
        assert reg.get("a") == {"v": 1}

    def test_has_reports_registration(self, reg):
        assert reg.has("missing") is False
        reg.register("present", lambda: 42)
        assert reg.has("present") is True

    def test_get_unknown_raises_service_not_found(self, reg):
        with pytest.raises(ServiceNotFoundError):
            reg.get("nope")

    def test_empty_name_rejected(self, reg):
        with pytest.raises(ValueError):
            reg.register("", lambda: 1)

    def test_duplicate_registration_without_override_rejected(self, reg):
        reg.register("a", lambda: 1)
        with pytest.raises(ValueError):
            reg.register("a", lambda: 2)


class TestSingletonLifecycle:
    def test_singleton_factory_called_once(self, reg):
        calls = []
        reg.register("once", lambda: calls.append(1) or "x")
        reg.get("once")
        reg.get("once")
        reg.get("once")
        assert calls == [1]

    def test_singleton_returns_same_instance(self, reg):
        reg.register("inst", lambda: object())
        a = reg.get("inst")
        b = reg.get("inst")
        assert a is b


class TestLazyConstruction:
    def test_factory_not_called_until_get(self, reg):
        called = []
        reg.register("lazy", lambda: called.append(1) or "v")
        assert called == []
        reg.get("lazy")
        assert called == [1]

    def test_construction_error_recorded_in_health(self, reg):
        def boom():
            raise RuntimeError("kaboom")
        reg.register("bad", boom)
        with pytest.raises(RuntimeError):
            reg.get("bad")
        h = reg.health("bad")
        assert "RuntimeError" in h.last_error
        assert "kaboom" in h.last_error
        assert h.constructed is False


class TestTransientLifecycle:
    def test_transient_factory_called_every_get(self, reg):
        calls = []
        reg.register(
            "t",
            lambda: (calls.append(1), object())[1],
            lifecycle=ServiceLifecycle.TRANSIENT,
        )
        a = reg.get("t")
        b = reg.get("t")
        assert a is not b
        assert calls == [1, 1]


class TestTestOverride:
    def test_override_replaces_factory_and_clears_instance(self, reg):
        reg.register("svc", lambda: "real")
        assert reg.get("svc") == "real"
        reg.register("svc", lambda: "fake", override=True)
        assert reg.get("svc") == "fake"

    def test_override_required_for_replacement(self, reg):
        reg.register("svc", lambda: "real")
        with pytest.raises(ValueError):
            reg.register("svc", lambda: "fake")


class TestHealthAndListing:
    def test_health_for_unbuilt_service(self, reg):
        reg.register("u", lambda: "x")
        h = reg.health("u")
        assert isinstance(h, ServiceHealth)
        assert h.constructed is False
        assert h.lifecycle == ServiceLifecycle.SINGLETON

    def test_health_after_build(self, reg):
        reg.register("b", lambda: "y")
        reg.get("b")
        assert reg.health("b").constructed is True

    def test_health_unknown_raises(self, reg):
        with pytest.raises(ServiceNotFoundError):
            reg.health("???")

    def test_list_services_returns_all_health_snapshots(self, reg):
        reg.register("a", lambda: 1)
        reg.register("b", lambda: 2)
        reg.get("a")
        names = {h.name for h in reg.list_services()}
        assert names == {"a", "b"}
        built = {h.name: h.constructed for h in reg.list_services()}
        assert built == {"a": True, "b": False}

    def test_health_to_dict_serializable(self, reg):
        reg.register("a", lambda: 1)
        reg.get("a")
        d = reg.health("a").to_dict()
        assert d["name"] == "a"
        assert d["lifecycle"] == "singleton"
        assert d["constructed"] is True


class TestResetForTests:
    def test_reset_clears_all_registrations(self, reg):
        reg.register("a", lambda: 1)
        reg.register("b", lambda: 2)
        reg.reset_for_tests()
        assert reg.has("a") is False
        assert reg.has("b") is False

    def test_clear_instances_keeps_registrations(self, reg):
        calls = []
        reg.register("a", lambda: calls.append(1) or "v")
        reg.get("a")
        reg.clear_instances()
        assert reg.has("a") is True
        reg.get("a")
        assert calls == [1, 1]


class TestModuleLevelSingleton:
    def test_get_registry_returns_same_instance(self):
        a = get_registry()
        b = get_registry()
        assert a is b

    def test_reset_drops_singleton(self):
        a = get_registry()
        reset_registry_for_tests()
        b = get_registry()
        assert a is not b


# ---------------------------------------------------------------------------
# Concrete service wiring (supervisor.services)
# ---------------------------------------------------------------------------

class TestDefaultServiceWiring:
    def test_register_default_services_registers_expected_names(self):
        from supervisor.services import register_default_services
        reg = register_default_services()
        assert reg.has("config")
        assert reg.has("workflow_engine")
        assert reg.has("confidence_calibrator")
        assert reg.has("fix_engine")

    def test_register_default_services_idempotent(self):
        from supervisor.services import register_default_services
        register_default_services()
        register_default_services()  # second call is a no-op
        reg = get_registry()
        names = {h.name for h in reg.list_services()}
        assert "config" in names

    def test_get_service_auto_wires(self):
        from supervisor.services import get_service
        cfg = get_service("config")
        # Must be an actual SentinelConfig instance
        from supervisor.sentinel_config import SentinelConfig
        assert isinstance(cfg, SentinelConfig)


class TestRegistryReturnsConfig:
    def test_config_via_registry_matches_direct_call(self):
        from supervisor.services import get_service
        from supervisor.sentinel_config import get_config
        assert get_service("config") is get_config()


class TestRegistryReturnsWorkflowEngine:
    def test_workflow_engine_via_registry_matches_direct_call(self, tmp_path):
        """Registry-returned WorkflowEngine must be the same singleton as get_engine()."""
        from supervisor.services import get_service
        from supervisor.workflow_engine import get_engine
        engine_via_registry = get_service("workflow_engine")
        engine_direct = get_engine()
        assert engine_via_registry is engine_direct


class TestRegistryReturnsCalibrator:
    def test_calibrator_via_registry_matches_direct_call(self):
        from supervisor.services import get_service
        from supervisor.confidence_calibrator import get_calibrator
        assert get_service("confidence_calibrator") is get_calibrator()


class TestRegistryReturnsFixEngine:
    def test_fix_engine_via_registry_matches_direct_call(self):
        from supervisor.services import get_service
        from supervisor.fix_engine import get_fix_engine
        assert get_service("fix_engine") is get_fix_engine()


# ---------------------------------------------------------------------------
# Backward compatibility: old direct-import paths must still work
# ---------------------------------------------------------------------------

class TestDirectImportPathsUnchanged:
    """The registry is additive — every old accessor still works untouched."""

    def test_get_config_still_works(self):
        from supervisor.sentinel_config import get_config, SentinelConfig
        assert isinstance(get_config(), SentinelConfig)

    def test_get_engine_still_works(self):
        from supervisor.workflow_engine import get_engine, WorkflowEngine
        assert isinstance(get_engine(), WorkflowEngine)

    def test_get_calibrator_still_works(self):
        from supervisor.confidence_calibrator import get_calibrator, ConfidenceCalibrator
        assert isinstance(get_calibrator(), ConfidenceCalibrator)

    def test_get_fix_engine_still_works(self):
        from supervisor.fix_engine import get_fix_engine, FixEngine
        assert isinstance(get_fix_engine(), FixEngine)


# ---------------------------------------------------------------------------
# No import cycle regression
# ---------------------------------------------------------------------------

class TestNoImportCycles:
    def test_sentinel_core_registry_has_no_supervisor_deps(self):
        """sentinel_core stays dependency-free."""
        import sentinel_core.service_registry as sr
        src = open(sr.__file__).read()
        for forbidden in ("from supervisor", "import supervisor",
                          "from intelligence", "import intelligence",
                          "from workers", "import workers"):
            assert forbidden not in src, f"sentinel_core must not depend on {forbidden!r}"

    def test_supervisor_services_imports_cleanly(self):
        import supervisor.services  # must not raise
        assert hasattr(supervisor.services, "register_default_services")
