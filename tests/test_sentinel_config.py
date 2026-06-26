"""Tests for supervisor/sentinel_config.py.

Covers:
- Unit: from_env() reads defaults correctly
- Unit: from_env() respects env var overrides
- Unit: validate() catches bad config and passes on good config
- Unit: get_config() returns singleton; reset_config() clears it
- Unit: type coercion (_bool, _int, _float) with bad input falls back to default
- Security: TE_TOKEN and AGUI_JWT_SECRET are not stored in the config object
"""
from __future__ import annotations

import os

import pytest

from supervisor.sentinel_config import (
    SentinelConfig,
    get_config,
    reset_config,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_config():
    """Ensure singleton is reset before and after every test."""
    reset_config()
    yield
    reset_config()


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

class TestDefaults:
    """Verify that from_env() produces the documented defaults."""

    def test_from_env_returns_sentinel_config(self, monkeypatch):
        monkeypatch.delenv("AGUI_AUTH_REQUIRED", raising=False)
        monkeypatch.delenv("AGUI_JWT_SECRET", raising=False)
        cfg = SentinelConfig.from_env()
        assert isinstance(cfg, SentinelConfig)

    def test_supervisor_defaults(self, monkeypatch):
        for key in (
            "LLM_ENABLED", "AGENTIC_PLANNER", "YAML_PLAYBOOKS_ENABLED",
            "LOOP_CONTROLLER_ENABLED", "STRATEGY_EVOLVER_ENABLED",
            "ALERT_DEDUP_ENABLED", "AGUI_ENABLED",
        ):
            monkeypatch.delenv(key, raising=False)
        cfg = SentinelConfig.from_env()
        assert cfg.supervisor.llm_enabled is False
        assert cfg.supervisor.agentic_planner is False
        assert cfg.supervisor.yaml_playbooks_enabled is False
        assert cfg.supervisor.loop_controller_enabled is False
        assert cfg.supervisor.strategy_evolver_enabled is False
        assert cfg.supervisor.alert_dedup_enabled is True
        assert cfg.supervisor.agui_enabled is True

    def test_loop_defaults(self, monkeypatch):
        monkeypatch.delenv("LOOP_CONVERGENCE_THRESHOLD", raising=False)
        monkeypatch.delenv("LOOP_MAX_NUDGES", raising=False)
        cfg = SentinelConfig.from_env()
        assert cfg.supervisor.loop_convergence_threshold == 0.72
        assert cfg.supervisor.loop_max_nudges == 2

    def test_budget_defaults(self, monkeypatch):
        monkeypatch.delenv("INVESTIGATION_BUDGET_MAX_CALLS", raising=False)
        monkeypatch.delenv("MCP_CALL_TIMEOUT_SECONDS", raising=False)
        cfg = SentinelConfig.from_env()
        assert cfg.supervisor.investigation_budget_max_calls == 20
        assert cfg.supervisor.mcp_call_timeout_seconds == 30

    def test_intelligence_defaults(self, monkeypatch):
        monkeypatch.delenv("ITSM_WRITEBACK_ENABLED", raising=False)
        monkeypatch.delenv("SEMANTIC_BACKEND", raising=False)
        cfg = SentinelConfig.from_env()
        assert cfg.intelligence.itsm_writeback_enabled is False
        assert cfg.intelligence.semantic_backend == "tfidf"

    def test_agui_defaults(self, monkeypatch):
        monkeypatch.delenv("AGUI_BFF_PORT", raising=False)
        monkeypatch.delenv("AGUI_BFF_HOST", raising=False)
        monkeypatch.delenv("AGUI_AUTH_REQUIRED", raising=False)
        monkeypatch.delenv("AGUI_HONEYPOT", raising=False)
        cfg = SentinelConfig.from_env()
        assert cfg.agui.bff_port == 8081
        assert cfg.agui.bff_host == "0.0.0.0"
        assert cfg.agui.auth_required is True
        assert cfg.agui.honeypot is True

    def test_database_defaults(self, monkeypatch):
        monkeypatch.delenv("DATABASE_POOL_SIZE", raising=False)
        monkeypatch.delenv("OPS_DB_ENABLED", raising=False)
        cfg = SentinelConfig.from_env()
        assert cfg.database.pool_size == 5
        assert cfg.database.ops_db_enabled is True

    def test_workers_defaults(self, monkeypatch):
        monkeypatch.delenv("ENABLE_THOUSANDEYES_RCA", raising=False)
        monkeypatch.delenv("TE_USE_FIXTURES", raising=False)
        monkeypatch.delenv("VISUAL_EVIDENCE_ENABLED", raising=False)
        cfg = SentinelConfig.from_env()
        assert cfg.workers.enable_thousandeyes_rca is False
        assert cfg.workers.te_use_fixtures is False
        assert cfg.workers.visual_evidence_enabled is True

    def test_environment_default(self, monkeypatch):
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        cfg = SentinelConfig.from_env()
        assert cfg.environment == "development"
        assert cfg.log_level == "INFO"


# ---------------------------------------------------------------------------
# Env var overrides
# ---------------------------------------------------------------------------

class TestEnvOverrides:
    """Verify that env vars are respected over defaults."""

    def test_bool_true_variants(self, monkeypatch):
        for value in ("true", "True", "TRUE", "1", "yes", "YES"):
            monkeypatch.setenv("LLM_ENABLED", value)
            cfg = SentinelConfig.from_env()
            assert cfg.supervisor.llm_enabled is True, f"Expected true for {value!r}"

    def test_bool_false_variants(self, monkeypatch):
        for value in ("false", "False", "FALSE", "0", "no", "NO", ""):
            monkeypatch.setenv("ALERT_DEDUP_ENABLED", value)
            cfg = SentinelConfig.from_env()
            assert cfg.supervisor.alert_dedup_enabled is False, f"Expected false for {value!r}"

    def test_int_override(self, monkeypatch):
        monkeypatch.setenv("INVESTIGATION_BUDGET_MAX_CALLS", "50")
        cfg = SentinelConfig.from_env()
        assert cfg.supervisor.investigation_budget_max_calls == 50

    def test_float_override(self, monkeypatch):
        monkeypatch.setenv("LOOP_CONVERGENCE_THRESHOLD", "0.85")
        cfg = SentinelConfig.from_env()
        assert cfg.supervisor.loop_convergence_threshold == pytest.approx(0.85)

    def test_string_override(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "staging")
        cfg = SentinelConfig.from_env()
        assert cfg.environment == "staging"

    def test_itsm_writeback_remains_false_by_default(self, monkeypatch):
        monkeypatch.delenv("ITSM_WRITEBACK_ENABLED", raising=False)
        cfg = SentinelConfig.from_env()
        assert cfg.intelligence.itsm_writeback_enabled is False
        assert cfg.agui.itsm_writeback_enabled is False

    def test_agentcore_target_override(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_TARGET_SPLUNK", "MySplunkTarget")
        cfg = SentinelConfig.from_env()
        assert cfg.workers.agentcore_target_splunk == "MySplunkTarget"

    def test_slack_channel_override(self, monkeypatch):
        monkeypatch.setenv("SLACK_RCA_CHANNEL", "#custom-channel")
        cfg = SentinelConfig.from_env()
        assert cfg.integrations.slack_rca_channel == "#custom-channel"


# ---------------------------------------------------------------------------
# Type coercion — bad input falls back gracefully
# ---------------------------------------------------------------------------

class TestTypeCoercion:
    """Bad env var values should fall back to defaults, not raise."""

    def test_bad_int_uses_default(self, monkeypatch):
        monkeypatch.setenv("INVESTIGATION_BUDGET_MAX_CALLS", "not_a_number")
        cfg = SentinelConfig.from_env()
        assert cfg.supervisor.investigation_budget_max_calls == 20

    def test_bad_float_uses_default(self, monkeypatch):
        monkeypatch.setenv("LOOP_CONVERGENCE_THRESHOLD", "not_a_float")
        cfg = SentinelConfig.from_env()
        assert cfg.supervisor.loop_convergence_threshold == 0.72

    def test_bad_int_for_port_uses_default(self, monkeypatch):
        monkeypatch.setenv("AGUI_BFF_PORT", "abc")
        cfg = SentinelConfig.from_env()
        assert cfg.agui.bff_port == 8081


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    """validate() must catch bad config before the system starts."""

    def _cfg(self, monkeypatch, **overrides):
        """Build a config that passes validation with minimal env setup."""
        monkeypatch.setenv("AGUI_AUTH_REQUIRED", "false")
        monkeypatch.delenv("AGUI_JWT_SECRET", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("ENABLE_THOUSANDEYES_RCA", "false")
        for k, v in overrides.items():
            monkeypatch.setenv(k, v)
        return SentinelConfig.from_env()

    def test_valid_config_passes(self, monkeypatch):
        cfg = self._cfg(monkeypatch)
        cfg.validate()  # must not raise

    def test_invalid_convergence_threshold_zero(self, monkeypatch):
        cfg = self._cfg(monkeypatch, LOOP_CONVERGENCE_THRESHOLD="0.0")
        with pytest.raises(ValueError, match="LOOP_CONVERGENCE_THRESHOLD"):
            cfg.validate()

    def test_invalid_convergence_threshold_above_one(self, monkeypatch):
        cfg = self._cfg(monkeypatch, LOOP_CONVERGENCE_THRESHOLD="1.1")
        with pytest.raises(ValueError, match="LOOP_CONVERGENCE_THRESHOLD"):
            cfg.validate()

    def test_negative_loop_max_nudges(self, monkeypatch):
        cfg = self._cfg(monkeypatch, LOOP_MAX_NUDGES="-1")
        with pytest.raises(ValueError, match="LOOP_MAX_NUDGES"):
            cfg.validate()

    def test_zero_budget_max_calls(self, monkeypatch):
        cfg = self._cfg(monkeypatch, INVESTIGATION_BUDGET_MAX_CALLS="0")
        with pytest.raises(ValueError, match="INVESTIGATION_BUDGET_MAX_CALLS"):
            cfg.validate()

    def test_auth_required_without_jwt_secret(self, monkeypatch):
        monkeypatch.setenv("AGUI_AUTH_REQUIRED", "true")
        monkeypatch.delenv("AGUI_JWT_SECRET", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("ENABLE_THOUSANDEYES_RCA", "false")
        cfg = SentinelConfig.from_env()
        with pytest.raises(ValueError, match="AGUI_JWT_SECRET"):
            cfg.validate()

    def test_auth_required_with_jwt_secret_passes(self, monkeypatch):
        monkeypatch.setenv("AGUI_AUTH_REQUIRED", "true")
        monkeypatch.setenv("AGUI_JWT_SECRET", "supersecret")
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("ENABLE_THOUSANDEYES_RCA", "false")
        cfg = SentinelConfig.from_env()
        cfg.validate()  # must not raise

    def test_thousandeyes_enabled_without_token(self, monkeypatch):
        monkeypatch.setenv("ENABLE_THOUSANDEYES_RCA", "true")
        monkeypatch.delenv("TE_TOKEN", raising=False)
        monkeypatch.setenv("AGUI_AUTH_REQUIRED", "false")
        monkeypatch.setenv("ENVIRONMENT", "development")
        cfg = SentinelConfig.from_env()
        with pytest.raises(ValueError, match="TE_TOKEN"):
            cfg.validate()

    def test_invalid_environment_value(self, monkeypatch):
        cfg = self._cfg(monkeypatch, ENVIRONMENT="prod")
        with pytest.raises(ValueError, match="ENVIRONMENT"):
            cfg.validate()

    def test_multiple_errors_reported_together(self, monkeypatch):
        cfg = self._cfg(
            monkeypatch,
            LOOP_CONVERGENCE_THRESHOLD="0.0",
            INVESTIGATION_BUDGET_MAX_CALLS="0",
        )
        with pytest.raises(ValueError) as exc_info:
            cfg.validate()
        msg = str(exc_info.value)
        assert "LOOP_CONVERGENCE_THRESHOLD" in msg
        assert "INVESTIGATION_BUDGET_MAX_CALLS" in msg


# ---------------------------------------------------------------------------
# Singleton behaviour
# ---------------------------------------------------------------------------

class TestSingleton:
    """get_config() must return same instance; reset_config() must clear it."""

    def test_get_config_returns_same_instance(self, monkeypatch):
        monkeypatch.setenv("AGUI_AUTH_REQUIRED", "false")
        cfg1 = get_config()
        cfg2 = get_config()
        assert cfg1 is cfg2

    def test_reset_config_forces_reload(self, monkeypatch):
        monkeypatch.setenv("AGUI_AUTH_REQUIRED", "false")
        monkeypatch.setenv("ENVIRONMENT", "development")
        cfg1 = get_config()
        reset_config()
        monkeypatch.setenv("ENVIRONMENT", "staging")
        cfg2 = get_config()
        assert cfg1 is not cfg2
        assert cfg2.environment == "staging"

    def test_reset_allows_clean_state_in_tests(self, monkeypatch):
        monkeypatch.setenv("AGUI_AUTH_REQUIRED", "false")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        cfg = get_config()
        assert cfg.log_level == "DEBUG"
        reset_config()
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        cfg2 = get_config()
        assert cfg2.log_level == "WARNING"


# ---------------------------------------------------------------------------
# Security: secrets must not appear on the config object
# ---------------------------------------------------------------------------

class TestSecretsNotStored:
    """API keys, tokens, and passwords must never be stored in SentinelConfig.

    This prevents accidental logging of the config object leaking credentials.
    """

    def test_te_token_not_in_config(self, monkeypatch):
        monkeypatch.setenv("TE_TOKEN", "super-secret-token")
        monkeypatch.setenv("AGUI_AUTH_REQUIRED", "false")
        cfg = SentinelConfig.from_env()
        cfg_str = str(cfg)
        assert "super-secret-token" not in cfg_str

    def test_jwt_secret_not_in_config_fields(self, monkeypatch):
        monkeypatch.setenv("AGUI_JWT_SECRET", "my-jwt-secret-value")
        monkeypatch.setenv("AGUI_AUTH_REQUIRED", "false")
        cfg = SentinelConfig.from_env()
        # AGUI_JWT_SECRET must not be a field on cfg.agui
        assert not hasattr(cfg.agui, "jwt_secret")
        cfg_str = str(cfg)
        assert "my-jwt-secret-value" not in cfg_str

    def test_slack_bot_token_not_in_config(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
        monkeypatch.setenv("AGUI_AUTH_REQUIRED", "false")
        cfg = SentinelConfig.from_env()
        cfg_str = str(cfg)
        assert "xoxb-secret" not in cfg_str


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------

class TestImmutability:
    """SentinelConfig and its sub-sections are frozen dataclasses."""

    def test_config_is_frozen(self, monkeypatch):
        monkeypatch.setenv("AGUI_AUTH_REQUIRED", "false")
        cfg = SentinelConfig.from_env()
        with pytest.raises((TypeError, AttributeError)):
            cfg.environment = "production"  # type: ignore[misc]

    def test_supervisor_section_is_frozen(self, monkeypatch):
        monkeypatch.setenv("AGUI_AUTH_REQUIRED", "false")
        cfg = SentinelConfig.from_env()
        with pytest.raises((TypeError, AttributeError)):
            cfg.supervisor.llm_enabled = True  # type: ignore[misc]
