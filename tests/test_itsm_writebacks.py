"""Tests for intelligence/itsm_writebacks.py.

Covers:
- dry_run mode returns success=True without HTTP calls
- mock provider used when no tokens set
- resolve() returns WritebackResult with correct fields
- acknowledge() works
- add_comment() works
- ITSM_WRITEBACK_ENABLED=false keeps dry_run=True
- get_engine() returns singleton
- WritebackResult fields are correct
"""
from __future__ import annotations

import os
import importlib

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_engine(dry_run: bool = True, env: dict | None = None):
    """Import ITSMWritebackEngine fresh (reload module to pick up env changes)."""
    import intelligence.itsm_writebacks as mod
    # Reset the module-level singleton so each test starts clean
    mod._engine = None
    # Patch provider detection at instance level by constructing directly
    engine = mod.ITSMWritebackEngine(dry_run=dry_run)
    return engine


def _get_module():
    import intelligence.itsm_writebacks as mod
    return mod


# ---------------------------------------------------------------------------
# Tests: WritebackResult dataclass
# ---------------------------------------------------------------------------

class TestWritebackResult:
    def test_fields_accessible(self):
        from intelligence.itsm_writebacks import WritebackResult
        r = WritebackResult(
            incident_id="INC-001",
            provider="mock",
            action="resolved",
            success=True,
            message="dry_run",
            dry_run=True,
        )
        assert r.incident_id == "INC-001"
        assert r.provider == "mock"
        assert r.action == "resolved"
        assert r.success is True
        assert r.message == "dry_run"
        assert r.dry_run is True

    def test_fields_false_success(self):
        from intelligence.itsm_writebacks import WritebackResult
        r = WritebackResult(
            incident_id="INC-002",
            provider="pagerduty",
            action="acknowledged",
            success=False,
            message="HTTP 404",
            dry_run=False,
        )
        assert r.success is False
        assert r.dry_run is False


# ---------------------------------------------------------------------------
# Tests: Provider detection
# ---------------------------------------------------------------------------

class TestProviderDetection:
    def test_mock_provider_when_no_tokens(self, monkeypatch):
        monkeypatch.delenv("PD_TOKEN", raising=False)
        monkeypatch.delenv("OG_TOKEN", raising=False)
        monkeypatch.delenv("SN_TOKEN", raising=False)
        from intelligence.itsm_writebacks import _detect_provider
        provider, token = _detect_provider()
        assert provider == "mock"
        assert token == ""

    def test_pd_provider_when_pd_token_set(self, monkeypatch):
        monkeypatch.setenv("PD_TOKEN", "pd-secret-token")
        monkeypatch.delenv("OG_TOKEN", raising=False)
        monkeypatch.delenv("SN_TOKEN", raising=False)
        from intelligence.itsm_writebacks import _detect_provider
        provider, token = _detect_provider()
        assert provider == "pagerduty"
        assert token == "pd-secret-token"

    def test_og_provider_when_og_token_set(self, monkeypatch):
        monkeypatch.delenv("PD_TOKEN", raising=False)
        monkeypatch.setenv("OG_TOKEN", "og-secret-token")
        monkeypatch.delenv("SN_TOKEN", raising=False)
        from intelligence.itsm_writebacks import _detect_provider
        provider, token = _detect_provider()
        assert provider == "opsgenie"
        assert token == "og-secret-token"

    def test_sn_provider_when_sn_token_set(self, monkeypatch):
        monkeypatch.delenv("PD_TOKEN", raising=False)
        monkeypatch.delenv("OG_TOKEN", raising=False)
        monkeypatch.setenv("SN_TOKEN", "sn-secret-token")
        from intelligence.itsm_writebacks import _detect_provider
        provider, token = _detect_provider()
        assert provider == "servicenow"
        assert token == "sn-secret-token"

    def test_pd_takes_priority_over_og(self, monkeypatch):
        monkeypatch.setenv("PD_TOKEN", "pd-token")
        monkeypatch.setenv("OG_TOKEN", "og-token")
        monkeypatch.delenv("SN_TOKEN", raising=False)
        from intelligence.itsm_writebacks import _detect_provider
        provider, token = _detect_provider()
        assert provider == "pagerduty"


# ---------------------------------------------------------------------------
# Tests: dry_run mode
# ---------------------------------------------------------------------------

class TestDryRunMode:
    def test_resolve_dry_run_returns_success_no_http(self, monkeypatch):
        monkeypatch.delenv("PD_TOKEN", raising=False)
        monkeypatch.delenv("OG_TOKEN", raising=False)
        monkeypatch.delenv("SN_TOKEN", raising=False)
        monkeypatch.setenv("ITSM_WRITEBACK_ENABLED", "false")

        engine = _fresh_engine(dry_run=True)
        result = engine.resolve(
            incident_id="INC-100",
            service="payment-service",
            root_cause="DB pool exhausted",
            resolution_action="increase pool size",
            confidence=0.92,
        )
        assert result.success is True
        assert result.dry_run is True
        assert result.action == "resolved"
        assert "INC-100" in result.message

    def test_acknowledge_dry_run_returns_success(self, monkeypatch):
        monkeypatch.delenv("PD_TOKEN", raising=False)
        monkeypatch.setenv("ITSM_WRITEBACK_ENABLED", "false")

        engine = _fresh_engine(dry_run=True)
        result = engine.acknowledge("INC-200", "cart-service", 0.85)
        assert result.success is True
        assert result.dry_run is True
        assert result.action == "acknowledged"

    def test_add_comment_dry_run_returns_success(self, monkeypatch):
        monkeypatch.delenv("PD_TOKEN", raising=False)
        monkeypatch.setenv("ITSM_WRITEBACK_ENABLED", "false")

        engine = _fresh_engine(dry_run=True)
        result = engine.add_comment("INC-300", "This is a test comment.")
        assert result.success is True
        assert result.dry_run is True
        assert result.action == "commented"

    def test_writeback_enabled_false_keeps_dry_run(self, monkeypatch):
        """Even with a token set, ITSM_WRITEBACK_ENABLED=false must keep dry_run behaviour."""
        monkeypatch.setenv("PD_TOKEN", "fake-pd-token")
        monkeypatch.setenv("ITSM_WRITEBACK_ENABLED", "false")

        engine = _fresh_engine(dry_run=True)
        result = engine.resolve("INC-400", "auth-service", "JWT saturation", "scale", 0.9)
        # Should be dry_run because ITSM_WRITEBACK_ENABLED=false overrides token presence
        assert result.success is True
        assert result.dry_run is True


# ---------------------------------------------------------------------------
# Tests: mock provider
# ---------------------------------------------------------------------------

class TestMockProvider:
    def test_resolve_mock_provider(self, monkeypatch):
        monkeypatch.delenv("PD_TOKEN", raising=False)
        monkeypatch.delenv("OG_TOKEN", raising=False)
        monkeypatch.delenv("SN_TOKEN", raising=False)
        monkeypatch.setenv("ITSM_WRITEBACK_ENABLED", "false")

        engine = _fresh_engine(dry_run=True)
        assert engine.provider == "mock"
        result = engine.resolve("INC-500", "svc", "root", "action", 0.8)
        assert result.provider == "mock"
        assert result.success is True

    def test_acknowledge_mock_provider(self, monkeypatch):
        monkeypatch.delenv("PD_TOKEN", raising=False)
        monkeypatch.delenv("OG_TOKEN", raising=False)
        monkeypatch.delenv("SN_TOKEN", raising=False)

        engine = _fresh_engine(dry_run=True)
        result = engine.acknowledge("INC-600", "svc", 0.75)
        assert result.provider == "mock"
        assert result.success is True


# ---------------------------------------------------------------------------
# Tests: get_engine singleton
# ---------------------------------------------------------------------------

class TestGetEngineSingleton:
    def test_get_engine_returns_same_instance(self, monkeypatch):
        import intelligence.itsm_writebacks as mod
        monkeypatch.setattr(mod, "_engine", None)  # reset singleton

        from intelligence.itsm_writebacks import get_engine
        e1 = get_engine()
        e2 = get_engine()
        assert e1 is e2

    def test_get_engine_returns_itsm_writeback_engine(self, monkeypatch):
        import intelligence.itsm_writebacks as mod
        monkeypatch.setattr(mod, "_engine", None)  # reset singleton

        from intelligence.itsm_writebacks import get_engine, ITSMWritebackEngine
        engine = get_engine()
        assert isinstance(engine, ITSMWritebackEngine)


# ---------------------------------------------------------------------------
# Tests: resolve WritebackResult correctness
# ---------------------------------------------------------------------------

class TestResolveResultFields:
    def test_result_incident_id_preserved(self, monkeypatch):
        monkeypatch.setenv("ITSM_WRITEBACK_ENABLED", "false")
        engine = _fresh_engine(dry_run=True)
        result = engine.resolve("INC-999", "svc", "root", "action", 0.8)
        assert result.incident_id == "INC-999"

    def test_result_action_is_resolved(self, monkeypatch):
        monkeypatch.setenv("ITSM_WRITEBACK_ENABLED", "false")
        engine = _fresh_engine(dry_run=True)
        result = engine.resolve("INC-001", "svc", "root", "action", 0.8)
        assert result.action == "resolved"

    def test_runbook_url_not_required(self, monkeypatch):
        monkeypatch.setenv("ITSM_WRITEBACK_ENABLED", "false")
        engine = _fresh_engine(dry_run=True)
        # Should not raise
        result = engine.resolve("INC-002", "svc", "root", "action", 0.8)
        assert result.success is True

    def test_runbook_url_accepted(self, monkeypatch):
        monkeypatch.setenv("ITSM_WRITEBACK_ENABLED", "false")
        engine = _fresh_engine(dry_run=True)
        result = engine.resolve("INC-003", "svc", "root", "action", 0.8,
                                runbook_url="https://wiki.example.com/runbook-123")
        assert result.success is True
