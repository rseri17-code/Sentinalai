"""Tests for mandatory webhook authentication (REQUIRE_WEBHOOK_AUTH).

Covers:
- _check_sig with REQUIRE_WEBHOOK_AUTH=true and missing secret → 401
- _check_sig with REQUIRE_WEBHOOK_AUTH=true and valid signature → passes
- _check_sig with REQUIRE_WEBHOOK_AUTH=true and bad signature → 401
- _check_sig with REQUIRE_WEBHOOK_AUTH=false and no secret → skipped (legacy)
- validate_webhook_secrets_at_startup raises when required secrets missing
- validate_webhook_secrets_at_startup passes when all secrets present
"""
from __future__ import annotations

import hashlib
import hmac
import os

import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signed_body(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _make_request(body: bytes, sig_header: str, header_name: str) -> MagicMock:
    req = MagicMock()
    req.body = AsyncMock(return_value=body)
    req.headers = {header_name: sig_header}
    return req


# ---------------------------------------------------------------------------
# _check_sig
# ---------------------------------------------------------------------------

class TestCheckSig:
    """Unit tests for the _check_sig async helper."""

    @pytest.mark.asyncio
    async def test_require_auth_no_secret_raises_401(self):
        """REQUIRE_WEBHOOK_AUTH=true + no secret → 401 misconfiguration."""
        with patch.dict(os.environ, {"REQUIRE_WEBHOOK_AUTH": "true"}):
            import importlib
            import agui.api.intake as intake_mod
            importlib.reload(intake_mod)
            req = MagicMock()
            with pytest.raises(HTTPException) as exc_info:
                await intake_mod._check_sig(req, secret="", header_name="X-Test-Sig")
            assert exc_info.value.status_code == 401
            assert "REQUIRE_WEBHOOK_AUTH" in exc_info.value.detail
        importlib.reload(intake_mod)  # restore

    @pytest.mark.asyncio
    async def test_require_auth_valid_signature_passes(self):
        """REQUIRE_WEBHOOK_AUTH=true + valid secret + valid sig → no exception."""
        secret = "my-prod-secret"
        body = b'{"event": "test"}'
        sig = _make_signed_body(secret, body)
        req = _make_request(body, sig, "X-Test-Sig")

        with patch.dict(os.environ, {"REQUIRE_WEBHOOK_AUTH": "true"}):
            import importlib
            import agui.api.intake as intake_mod
            importlib.reload(intake_mod)
            await intake_mod._check_sig(req, secret=secret, header_name="X-Test-Sig")
        importlib.reload(intake_mod)

    @pytest.mark.asyncio
    async def test_require_auth_bad_signature_raises_401(self):
        """REQUIRE_WEBHOOK_AUTH=true + valid secret + wrong sig → 401."""
        secret = "my-prod-secret"
        body = b'{"event": "test"}'
        req = _make_request(body, "sha256=deadbeef", "X-Test-Sig")

        with patch.dict(os.environ, {"REQUIRE_WEBHOOK_AUTH": "true"}):
            import importlib
            import agui.api.intake as intake_mod
            importlib.reload(intake_mod)
            with pytest.raises(HTTPException) as exc_info:
                await intake_mod._check_sig(req, secret=secret, header_name="X-Test-Sig")
            assert exc_info.value.status_code == 401
        importlib.reload(intake_mod)

    @pytest.mark.asyncio
    async def test_require_auth_missing_header_raises_401(self):
        """REQUIRE_WEBHOOK_AUTH=true + valid secret + missing header → 401."""
        secret = "my-prod-secret"
        req = MagicMock()
        req.headers = {}  # no sig header
        req.body = AsyncMock(return_value=b"{}")

        with patch.dict(os.environ, {"REQUIRE_WEBHOOK_AUTH": "true"}):
            import importlib
            import agui.api.intake as intake_mod
            importlib.reload(intake_mod)
            with pytest.raises(HTTPException) as exc_info:
                await intake_mod._check_sig(req, secret=secret, header_name="X-Test-Sig")
            assert exc_info.value.status_code == 401
        importlib.reload(intake_mod)

    @pytest.mark.asyncio
    async def test_no_require_auth_no_secret_skips(self):
        """REQUIRE_WEBHOOK_AUTH=false (default) + no secret → skip validation (legacy)."""
        with patch.dict(os.environ, {"REQUIRE_WEBHOOK_AUTH": "false"}):
            import importlib
            import agui.api.intake as intake_mod
            importlib.reload(intake_mod)
            req = MagicMock()
            # Should not raise
            await intake_mod._check_sig(req, secret="", header_name="X-Test-Sig")
        importlib.reload(intake_mod)


# ---------------------------------------------------------------------------
# validate_webhook_secrets_at_startup
# ---------------------------------------------------------------------------

class TestValidateWebhookSecretsAtStartup:
    """Tests for the startup validator."""

    def test_raises_when_require_auth_and_secrets_missing(self):
        """RuntimeError raised if REQUIRE_WEBHOOK_AUTH=true and any secret missing."""
        env_overrides = {
            "REQUIRE_WEBHOOK_AUTH": "true",
            "MOOGSOFT_WEBHOOK_SECRET": "",
            "PAGERDUTY_WEBHOOK_SECRET": "",
            "SNOW_WEBHOOK_SECRET": "",
            "OPSGENIE_WEBHOOK_SECRET": "",
            "GRAFANA_WEBHOOK_SECRET": "",
            "CLOUDWATCH_WEBHOOK_SECRET": "",
        }
        with patch.dict(os.environ, env_overrides):
            import importlib
            import agui.api.intake as intake_mod
            importlib.reload(intake_mod)
            with pytest.raises(RuntimeError) as exc_info:
                intake_mod.validate_webhook_secrets_at_startup()
            assert "REQUIRE_WEBHOOK_AUTH" in str(exc_info.value)
        importlib.reload(intake_mod)

    def test_passes_when_require_auth_and_all_secrets_set(self):
        """No error if REQUIRE_WEBHOOK_AUTH=true and all secrets are set."""
        env_overrides = {
            "REQUIRE_WEBHOOK_AUTH": "true",
            "MOOGSOFT_WEBHOOK_SECRET": "s1",
            "PAGERDUTY_WEBHOOK_SECRET": "s2",
            "SNOW_WEBHOOK_SECRET": "s3",
            "OPSGENIE_WEBHOOK_SECRET": "s4",
            "GRAFANA_WEBHOOK_SECRET": "s5",
            "CLOUDWATCH_WEBHOOK_SECRET": "s6",
        }
        with patch.dict(os.environ, env_overrides):
            import importlib
            import agui.api.intake as intake_mod
            importlib.reload(intake_mod)
            intake_mod.validate_webhook_secrets_at_startup()  # must not raise
        importlib.reload(intake_mod)

    def test_skips_validation_when_require_auth_false(self):
        """No error if REQUIRE_WEBHOOK_AUTH=false regardless of secrets."""
        env_overrides = {
            "REQUIRE_WEBHOOK_AUTH": "false",
            "MOOGSOFT_WEBHOOK_SECRET": "",
        }
        with patch.dict(os.environ, env_overrides):
            import importlib
            import agui.api.intake as intake_mod
            importlib.reload(intake_mod)
            intake_mod.validate_webhook_secrets_at_startup()  # must not raise
        importlib.reload(intake_mod)
