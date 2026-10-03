"""AgentCore MCP Gateway Client.

Provides a unified gateway that fronts all MCP tool targets deployed on
Amazon Bedrock AgentCore.  Every backend API (Moogsoft, Splunk, Sysdig,
SignalFx, Dynatrace, ServiceNow, GitHub) is registered as a gateway target
and accessed through a single AgentCore gateway URL via the MCP protocol.

Workers MUST call MCP tools through the gateway — never directly via boto3
or HTTP.  The gateway handles authentication, tool routing, transport,
response parsing, structured logging, and stub fallback for local dev/tests.

Requires (production):
    - GATEWAY_MODE=live (or unset with a gateway URL set)
    - strands-agents SDK  (strands.tools.mcp.MCPClient)
    - mcp SDK             (mcp.client.streamable_http)
    - MCP_GATEWAY_URL (clone-facing) or AGENTCORE_GATEWAY_URL (legacy alias)
    - OAuth2 client credentials (GATEWAY_OAUTH2_CLIENT_ID + token URL), OR
    - GATEWAY_ACCESS_TOKEN env var for static CUSTOM_JWT auth, OR
      AWS credentials for AWS_IAM (SigV4) auth

GATEWAY_MODE:
    stub | fixtures  — always return in-process stubs (even if a URL is set)
    live | agentcore — call the gateway / ARNs
    unset            — auto: live if URL or ARN is set, else stub

PLAIN_MCP (default off):
    When unset/false, tool names are still rewritten to ``Target___operation``.
    When true, the worker's plain tool name is sent with no rewrite.
    Plain mode never replaces a failed live call with stub data.
    Plain mode also refuses to fetch live evidence unless the model layer
    is NullInference (LLM_ENABLED=false or LLM_PROVIDER=null/none/disabled).
    That guard stays until identifier masking before model calls exists
    and has been verified.

Authentication priority:
    1. OAuth2 client_credentials grant (if GATEWAY_OAUTH2_CLIENT_ID is set)
       - Automatic token acquisition from Cognito token endpoint
       - In-memory caching with 10-minute pre-expiry refresh
       - Client secret from env var or AWS Secrets Manager
    2. Static Bearer token (if GATEWAY_ACCESS_TOKEN is set)
    3. No auth (local dev / tests)

Enterprise architecture (AgentCore gateway pattern):
    Worker -> McpGateway.invoke()
        -> MCPClient.call_tool_sync()
            -> streamablehttp_client (HTTPS + MCP protocol)
                -> AgentCore Gateway (bedrock-agentcore)
                    -> Gateway Target (Lambda / OpenAPI / MCP server)
                        -> Backend API

OAuth2 two-legged flow (client_credentials):
    Agent (TOKEN-A) -> AgentCore Gateway
        Gateway validates TOKEN-A, maps audience
        Gateway mints TOKEN-B (per-resource) via credential provider
        Gateway -> Resource MCP Server (TOKEN-B) -> Backend API

Gateway target naming convention:
    Tools exposed through the gateway use triple-underscore naming:
        {TARGET_NAME}___{OPERATION_NAME}
    Example: "SplunkTarget___search_oneshot"

    Workers use dotted names internally (e.g. "splunk.search_oneshot"),
    which the gateway maps to the actual gateway tool name at invocation.

Servers fronted by this gateway:
    - moogsoft     (AIOPS / incident management)
    - splunk       (log analytics / change data)
    - sysdig       (infrastructure metrics / events)
    - signalfx     (APM metrics)
    - dynatrace    (APM / problems / entities)
    - servicenow   (ITSM / CMDB / change management)
    - github       (DevOps / CI-CD / code changes)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

logger = logging.getLogger("sentinalai.mcp_client")

# ---------------------------------------------------------------------------
# Optional SDK imports (graceful — tests run without them)
# ---------------------------------------------------------------------------

try:
    from strands.tools.mcp import MCPClient
    from mcp.client.streamable_http import streamablehttp_client
    _MCP_SDK_AVAILABLE = True
except ImportError:
    _MCP_SDK_AVAILABLE = False
    MCPClient = None  # type: ignore[assignment,misc]

# Legacy boto3 import (kept for backward-compat in _parse_agent_response)
try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError as _ClientError
    _BOTO3_AVAILABLE = True
except ImportError:
    _BOTO3_AVAILABLE = False
    _ClientError = None


# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

def _env_gateway_url() -> str:
    """Read the MCP gateway URL from env.

    ``AGENTCORE_GATEWAY_URL`` wins when both are set (existing deploys).
    ``MCP_GATEWAY_URL`` is the clone-facing alias.
    """
    return (
        os.environ.get("AGENTCORE_GATEWAY_URL", "").strip()
        or os.environ.get("MCP_GATEWAY_URL", "").strip()
    )


# Gateway URL — single endpoint for all MCP targets.
# Import-time snapshot so AGENTCORE_GATEWAY_URL remains patchable by tests
# and existing compose that sets only that name. MCP_GATEWAY_URL is accepted
# at import when the AgentCore name is unset.
AGENTCORE_GATEWAY_URL = _env_gateway_url()

# GATEWAY_MODE is the real stub-vs-live switch (compose sets a URL even in stub).
#   stub | fixtures  — always in-process stubs, even if a gateway URL is set
#   live | agentcore — use URL / MCP_*_TOOL_ARN as today
#   unset / other    — auto: live if URL or ARN is set, else stub (backward compatible)
GATEWAY_MODE = os.environ.get("GATEWAY_MODE", "").strip().lower()
_STUB_GATEWAY_MODES = frozenset({"stub", "fixtures", "fixture"})
_LIVE_GATEWAY_MODES = frozenset({"live", "agentcore"})

# Authentication — static Bearer token (fallback if OAuth2 not configured)
GATEWAY_ACCESS_TOKEN = os.environ.get("GATEWAY_ACCESS_TOKEN", "")

# OAuth2 client_credentials configuration (preferred auth method)
GATEWAY_OAUTH2_CLIENT_ID = os.environ.get("GATEWAY_OAUTH2_CLIENT_ID", "")
GATEWAY_OAUTH2_CLIENT_SECRET = os.environ.get("GATEWAY_OAUTH2_CLIENT_SECRET", "")
GATEWAY_OAUTH2_TOKEN_URL = os.environ.get("GATEWAY_OAUTH2_TOKEN_URL", "")
GATEWAY_OAUTH2_SCOPE = os.environ.get("GATEWAY_OAUTH2_SCOPE", "")
# Optional: ARN of Secrets Manager secret holding client_secret
GATEWAY_OAUTH2_SECRET_ARN = os.environ.get("GATEWAY_OAUTH2_SECRET_ARN", "")
# Optional: Cognito User Pool ID (to auto-derive token_url if not provided)
GATEWAY_COGNITO_USER_POOL_ID = os.environ.get("GATEWAY_COGNITO_USER_POOL_ID", "")
GATEWAY_COGNITO_DOMAIN = os.environ.get("GATEWAY_COGNITO_DOMAIN", "")
# Token refresh buffer (seconds before expiry to trigger refresh)
_TOKEN_REFRESH_BUFFER = int(os.environ.get("GATEWAY_TOKEN_REFRESH_BUFFER_SECONDS", "600"))

# Legacy per-server ARNs (backward compat — deprecated in favor of gateway URL)
MCP_TOOL_ARNS: dict[str, str] = {
    "moogsoft": os.environ.get("MCP_MOOGSOFT_TOOL_ARN", ""),
    "splunk": os.environ.get("MCP_SPLUNK_TOOL_ARN", ""),
    "sysdig": os.environ.get("MCP_SYSDIG_TOOL_ARN", ""),
    "signalfx": os.environ.get("MCP_SIGNALFX_TOOL_ARN", ""),
    "dynatrace": os.environ.get("MCP_DYNATRACE_TOOL_ARN", ""),
    "servicenow": os.environ.get("MCP_SERVICENOW_TOOL_ARN", ""),
    "github": os.environ.get("MCP_GITHUB_TOOL_ARN", ""),
    "confluence": os.environ.get("MCP_CONFLUENCE_TOOL_ARN", ""),
}

# Retry / timeout config
MCP_CALL_TIMEOUT = int(os.environ.get("MCP_CALL_TIMEOUT_SECONDS", "30"))
MCP_MAX_RETRIES = int(os.environ.get("MCP_MAX_RETRIES", "2"))


def _has_any_arn() -> bool:
    """Check if any MCP tool ARN is configured (legacy check)."""
    return any(arn for arn in MCP_TOOL_ARNS.values())


def resolved_gateway_url() -> str:
    """Effective MCP gateway URL.

    Prefers the module-level ``AGENTCORE_GATEWAY_URL`` so tests that patch
    ``workers.mcp_client.AGENTCORE_GATEWAY_URL`` keep working. When that
    value is empty, falls back to ``MCP_GATEWAY_URL``.
    """
    if AGENTCORE_GATEWAY_URL:
        return AGENTCORE_GATEWAY_URL
    return os.environ.get("MCP_GATEWAY_URL", "").strip()


def force_stub_gateway() -> bool:
    """True when GATEWAY_MODE explicitly requests in-process stubs.

    Compose may set a gateway URL even in stub; this flag wins.
    """
    return GATEWAY_MODE in _STUB_GATEWAY_MODES


def resolved_gateway_mode() -> str:
    """Effective MCP path: ``stub`` or ``live``.

    ``GATEWAY_MODE=stub`` forces stubs. ``live``/``agentcore`` uses URL/ARNs
    when present. Unset keeps today's auto behavior (URL or ARN → live).
    """
    url = resolved_gateway_url()
    if force_stub_gateway():
        return "stub"
    if GATEWAY_MODE in _LIVE_GATEWAY_MODES:
        return "live" if (url or _has_any_arn()) else "stub"
    if url or _has_any_arn():
        return "live"
    return "stub"


def plain_mcp_enabled() -> bool:
    """True when plain MCP tool names are opted in. Default off."""
    return os.environ.get("PLAIN_MCP", "").strip().lower() in {"1", "true", "yes", "on"}


def mcp_call_timeout_seconds() -> float:
    """Per-call timeout for live MCP and legacy calls.

    ``MCP_CALL_TIMEOUT`` is the import-time default (30s). A later env change
    is honored so tests can bound a hung call without reimporting.
    """
    raw = os.environ.get("MCP_CALL_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return float(MCP_CALL_TIMEOUT)
    try:
        value = float(raw)
    except ValueError:
        return float(MCP_CALL_TIMEOUT)
    if value <= 0:
        return float(MCP_CALL_TIMEOUT)
    return value


# Identifier-masking guard. Live plain-MCP evidence must not reach a real model
# until masking exists and is verified. Fail closed if the model layer cannot
# be confirmed null.
_PLAIN_MCP_MODEL_GUARD = (
    "Refusing to send live MCP evidence to a non-null model; "
    "identifier masking is not verified. Set LLM_PROVIDER=null or "
    "LLM_ENABLED=false. This guard stays until identifier masking "
    "before model calls is implemented and verified."
)

_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(authorization|bearer|token|secret|password|api[_-]?key)\b\s*[:=]\s*\S+"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")


def scrub_secrets(text: str, limit: int = 200) -> str:
    """Drop credential-shaped fragments and bound the length."""
    cleaned = _BEARER_RE.sub("Bearer [redacted]", text or "")
    cleaned = _SECRET_ASSIGN_RE.sub(lambda match: f"{match.group(1)}=[redacted]", cleaned)
    return cleaned[:limit]


def plain_mcp_model_block_reason() -> str | None:
    """Return the guard message when plain MCP would feed a non-null model.

    Returns None when plain mode is off or the resolved port is NullInference.
    An import or resolution failure fails closed (message returned) so live
    evidence is not fetched.
    """
    if not plain_mcp_enabled():
        return None
    try:
        from supervisor.llm import get_inference_port
        port = get_inference_port()
    except Exception:
        return _PLAIN_MCP_MODEL_GUARD
    if type(port).__name__ == "NullInference":
        return None
    return _PLAIN_MCP_MODEL_GUARD


def _is_unauthorized(exc: BaseException) -> bool:
    """True when *exc* or its cause chain looks like an HTTP 401."""
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in seen:
        seen.append(current)
        text = str(current)
        if "401" in text or "Unauthorized" in text:
            return True
        current = current.__cause__ or current.__context__
    return False


def _call_with_timeout(
    fn: Callable[[], Any],
    timeout_s: float,
    *,
    on_timeout: Callable[[threading.Thread, dict[str, Any]], None] | None = None,
) -> Any:
    """Run *fn* and raise TimeoutError if it has not finished in *timeout_s*.

    The worker is a daemon so a timed-out call cannot block process exit.
    Anything the worker raises is re-raised on the caller. ``Exception`` is
    unchanged. ``KeyboardInterrupt`` and ``SystemExit`` propagate unchanged.
    Any other ``BaseException`` is wrapped so ``invoke`` can turn it into an
    error dict instead of treating a dead worker as a ``None`` result.
    ``on_timeout`` runs only when the worker is still alive, before the
    TimeoutError is raised.
    """
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:
            box["error"] = exc
        except BaseException as exc:
            # Not Exception: if this kills the worker, invoke sees None and
            # reports raw_response "None" instead of the failure.
            box["fatal"] = exc

    worker = threading.Thread(target=_run, name="mcp-call-timeout", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        if on_timeout is not None:
            on_timeout(worker, box)
        raise TimeoutError(f"mcp call exceeded {timeout_s:g}s")
    if "error" in box:
        raise box["error"]
    if "fatal" in box:
        fatal = box["fatal"]
        if isinstance(fatal, (KeyboardInterrupt, SystemExit)):
            raise fatal
        raise RuntimeError(f"{type(fatal).__name__}: {fatal}") from fatal
    return box.get("value")


def _stop_abandoned_client(client: Any) -> None:
    """Stop a session whose start() finished after the timeout already fired."""
    stop = getattr(client, "stop", None)
    if callable(stop):
        try:
            stop(None, None, None)
            return
        except TypeError:
            try:
                stop()
                return
            except Exception as exc:
                logger.warning("Failed to stop MCP client after a timed-out start: %s", exc)
                return
        except Exception as exc:
            logger.warning("Failed to stop MCP client after a timed-out start: %s", exc)
            return
    close = getattr(client, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception as exc:
        logger.warning("Failed to close MCP client after a timed-out start: %s", exc)


def _reap_late_start(client: Any, worker: threading.Thread, box: dict[str, Any]) -> None:
    """If start() succeeds on the daemon after its timeout, close that session."""

    def _reap() -> None:
        worker.join()
        if "error" in box or "fatal" in box:
            return
        _stop_abandoned_client(client)

    threading.Thread(target=_reap, name="mcp-start-reap", daemon=True).start()


def _failure_dict(mcp_tool_name: str, exc: BaseException, *, plain: bool) -> dict[str, Any]:
    """Error dict for a live call that did not succeed.

    Flag off keeps the historical ``gateway_exception`` string so existing
    callers and tests stay stable. Plain mode never attaches stub payload
    keys; the connection state is ``failed``.
    """
    if not plain:
        return {"error": f"gateway_exception: {exc}", "tool": mcp_tool_name}
    safe = scrub_secrets(str(exc))
    return {
        "error": f"failed: {type(exc).__name__}: {safe}",
        "tool": mcp_tool_name,
        "error_class": type(exc).__name__,
        "connection_state": "failed",
    }


def _mark_stub_substitution(payload: dict[str, Any]) -> dict[str, Any]:
    """Label a live-path stub fallback. Fixture mode does not use this."""
    marked = dict(payload)
    marked["connection_state"] = "stubbed"
    return marked


# Optional HTTP library for OAuth2 token requests
try:
    import requests as _requests_lib
    _REQUESTS_AVAILABLE = True
except ImportError:
    _requests_lib = None  # type: ignore[assignment]
    _REQUESTS_AVAILABLE = False


# =========================================================================
# OAuth2 Credential Provider — client_credentials grant with token caching
# =========================================================================

class OAuth2CredentialProvider:
    """Manages OAuth2 access tokens via client_credentials grant.

    Implements the two-legged OAuth flow used by AgentCore gateways:
    - Agent authenticates to Cognito with client_id + client_secret
    - Receives an M2M access token (TOKEN-A) with configured scopes
    - Token is cached in memory and refreshed automatically before expiry

    The AgentCore gateway then validates TOKEN-A and uses its own
    credential provider to mint per-resource tokens (TOKEN-B/C) for
    downstream MCP targets (Splunk, ServiceNow, GitHub, etc.).

    Thread-safe: uses a lock for token refresh operations.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_url: str,
        scope: str = "",
        refresh_buffer_seconds: int = 600,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._scope = scope
        self._refresh_buffer = timedelta(seconds=refresh_buffer_seconds)
        self._token: str | None = None
        self._expiry: datetime | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> OAuth2CredentialProvider | None:
        """Create a provider from environment variables, or None if not configured.

        Resolves the token URL from either:
        - GATEWAY_OAUTH2_TOKEN_URL (explicit)
        - GATEWAY_COGNITO_DOMAIN + AWS_REGION (Cognito convention)

        Resolves client_secret from either:
        - GATEWAY_OAUTH2_CLIENT_SECRET (env var)
        - GATEWAY_OAUTH2_SECRET_ARN (fetched from Secrets Manager at init)
        """
        client_id = GATEWAY_OAUTH2_CLIENT_ID
        if not client_id:
            return None

        # Resolve token URL
        token_url = GATEWAY_OAUTH2_TOKEN_URL
        if not token_url and GATEWAY_COGNITO_DOMAIN:
            token_url = (
                f"https://{GATEWAY_COGNITO_DOMAIN}"
                f".auth.{AWS_REGION}.amazoncognito.com/oauth2/token"
            )
        if not token_url:
            logger.warning(
                "GATEWAY_OAUTH2_CLIENT_ID is set but no token URL configured "
                "(set GATEWAY_OAUTH2_TOKEN_URL or GATEWAY_COGNITO_DOMAIN)"
            )
            return None

        # Resolve client secret
        client_secret = GATEWAY_OAUTH2_CLIENT_SECRET
        if not client_secret and GATEWAY_OAUTH2_SECRET_ARN:
            client_secret = _fetch_secret_from_asm(GATEWAY_OAUTH2_SECRET_ARN)
        if not client_secret:
            logger.warning(
                "GATEWAY_OAUTH2_CLIENT_ID is set but no client secret configured "
                "(set GATEWAY_OAUTH2_CLIENT_SECRET or GATEWAY_OAUTH2_SECRET_ARN)"
            )
            return None

        scope = GATEWAY_OAUTH2_SCOPE
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            token_url=token_url,
            scope=scope,
            refresh_buffer_seconds=_TOKEN_REFRESH_BUFFER,
        )

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing if expired or near-expiry.

        Uses double-checked locking to minimize contention: concurrent
        callers skip the lock entirely when a valid cached token exists.
        Only the first thread to detect expiry acquires the lock and
        performs the HTTP refresh; late arrivals recheck after acquiring
        the lock and reuse the freshly-refreshed token.

        Returns empty string if token acquisition fails.
        """
        # Fast path (no lock): return cached token if still valid
        now = datetime.now(timezone.utc)
        if self._token and self._expiry and now < self._expiry:
            return self._token

        # Slow path: acquire lock, recheck, refresh if still expired
        with self._lock:
            now = datetime.now(timezone.utc)
            if self._token and self._expiry and now < self._expiry:
                return self._token
            return self._refresh()

    def get_auth_headers(self) -> dict[str, str]:
        """Return Authorization headers with a valid Bearer token."""
        token = self.get_access_token()
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    def invalidate(self) -> None:
        """Force token refresh on next call (e.g., after a 401 response)."""
        with self._lock:
            self._token = None
            self._expiry = None

    def _refresh(self) -> str:
        """Acquire a new token via client_credentials grant.

        Called under lock. Returns the new token or empty string on failure.
        """
        if not _REQUESTS_AVAILABLE:
            logger.warning("requests library not installed — OAuth2 token refresh disabled")
            return ""

        try:
            data: dict[str, str] = {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
            if self._scope:
                data["scope"] = self._scope

            response = _requests_lib.post(
                self._token_url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=data,
                timeout=10,
            )
            response.raise_for_status()
            body = response.json()

            self._token = body["access_token"]
            expires_in = body.get("expires_in", 3600)
            self._expiry = (
                datetime.now(timezone.utc)
                + timedelta(seconds=expires_in)
                - self._refresh_buffer
            )
            logger.info(
                "OAuth2 token acquired: expires_in=%ds scope=%s",
                expires_in, self._scope or "(default)",
            )
            return self._token

        except Exception as exc:
            logger.error("OAuth2 token refresh failed: %s", exc)
            self._token = None
            self._expiry = None
            return ""


def _fetch_secret_from_asm(secret_arn: str) -> str:
    """Fetch a client_secret from AWS Secrets Manager.

    Returns the secret string, or empty string on failure.
    Used when GATEWAY_OAUTH2_SECRET_ARN is set instead of a direct secret.
    """
    if not _BOTO3_AVAILABLE:
        logger.warning("boto3 not available — cannot fetch secret from Secrets Manager")
        return ""
    try:
        sm = boto3.client("secretsmanager", region_name=AWS_REGION)
        response = sm.get_secret_value(SecretId=secret_arn)
        secret_str = response.get("SecretString", "")
        # Support both plain string and JSON {"client_secret": "..."} formats
        if secret_str.startswith("{"):
            try:
                secret_dict = json.loads(secret_str)
                return secret_dict.get("client_secret", secret_dict.get("secret", secret_str))
            except (json.JSONDecodeError, TypeError):
                pass
        return secret_str
    except Exception as exc:
        logger.error("Failed to fetch secret from Secrets Manager (%s): %s", secret_arn, exc)
        return ""


# ---------------------------------------------------------------------------
# MCP Tool name -> server mapping (all 7 servers)
# ---------------------------------------------------------------------------

_TOOL_TO_SERVER: dict[str, str] = {
    # Moogsoft (AIOPS)
    "moogsoft.get_incident_by_id": "moogsoft",
    "moogsoft.get_incidents": "moogsoft",
    "moogsoft.get_critical_incidents": "moogsoft",
    "moogsoft.get_alerts": "moogsoft",
    "moogsoft.get_historical_analysis": "moogsoft",
    "moogsoft.get_closed_incidents": "moogsoft",
    # Splunk (logs / change data)
    "splunk.search_oneshot": "splunk",
    "splunk.search_export": "splunk",
    "splunk.get_change_data": "splunk",
    "splunk.app_change_data": "splunk",
    "splunk.get_host_metrics": "splunk",
    "splunk.get_health_status": "splunk",
    "splunk.get_incident_data": "splunk",
    # Sysdig (infrastructure metrics / events)
    "sysdig.query_metrics": "sysdig",
    "sysdig.golden_signals": "sysdig",
    "sysdig.get_events": "sysdig",
    "sysdig.discover_resources": "sysdig",
    "sysdig.environment_status": "sysdig",
    # SignalFx (APM)
    "signalfx.query_signalfx_metrics": "signalfx",
    "signalfx.get_signalfx_active_incidents": "signalfx",
    # Dynatrace (APM / problems)
    "dynatrace.get_problems": "dynatrace",
    "dynatrace.get_metrics": "dynatrace",
    "dynatrace.get_entities": "dynatrace",
    "dynatrace.get_events": "dynatrace",
    # ServiceNow (ITSM / CMDB)
    "servicenow.get_ci_details": "servicenow",
    "servicenow.search_incidents": "servicenow",
    "servicenow.get_change_records": "servicenow",
    "servicenow.get_known_errors": "servicenow",
    # GitHub (DevOps / CI-CD)
    "github.get_recent_deployments": "github",
    "github.get_pr_details": "github",
    "github.get_commit_diff": "github",
    "github.get_workflow_runs": "github",
    "github.create_fix_pr": "github",
    "github.create_pull_request": "github",
    "github.reply_to_review_comment": "github",
    "github.request_reviewers": "github",
    # Kubernetes (rollback / scale)
    "kubernetes.rollback_deployment": "kubernetes",
    "kubernetes.scale_service": "kubernetes",
    "kubernetes.get_deployment_status": "kubernetes",
    "kubernetes.get_pod_logs": "kubernetes",
    # Confluence (documentation / runbooks / post-mortems)
    "confluence.search_runbooks": "confluence",
    "confluence.search_postmortems": "confluence",
    "confluence.get_page": "confluence",
}

# Server -> default AgentCore gateway target name mapping.
# These are the target names configured when creating the gateway via
# bedrock-agentcore-control.create_gateway_target().
# Override via AGENTCORE_TARGET_{SERVER} env vars for custom target names.
_SERVER_TO_TARGET: dict[str, str] = {
    "moogsoft": os.environ.get("AGENTCORE_TARGET_MOOGSOFT", "MoogsoftTarget"),
    "splunk": os.environ.get("AGENTCORE_TARGET_SPLUNK", "SplunkTarget"),
    "sysdig": os.environ.get("AGENTCORE_TARGET_SYSDIG", "SysdigTarget"),
    "signalfx": os.environ.get("AGENTCORE_TARGET_SIGNALFX", "SignalFxTarget"),
    "dynatrace": os.environ.get("AGENTCORE_TARGET_DYNATRACE", "DynatraceTarget"),
    "servicenow": os.environ.get("AGENTCORE_TARGET_SERVICENOW", "ServiceNowTarget"),
    "github": os.environ.get("AGENTCORE_TARGET_GITHUB", "GitHubTarget"),
    "confluence": os.environ.get("AGENTCORE_TARGET_CONFLUENCE", "ConfluenceTarget"),
    "kubernetes": os.environ.get("AGENTCORE_TARGET_KUBERNETES", "KubernetesTarget"),
}


def _to_gateway_tool_name(mcp_tool_name: str) -> str:
    """Convert internal dotted name to AgentCore gateway tool name.

    Internal:  "splunk.search_oneshot"
    Gateway:   "SplunkTarget___search_oneshot"

    The gateway uses triple-underscore to separate target name from operation.
    """
    server = _TOOL_TO_SERVER.get(mcp_tool_name, "")
    if not server:
        return mcp_tool_name
    target = _SERVER_TO_TARGET.get(server, "")
    if not target:
        return mcp_tool_name
    # Extract operation from dotted name: "splunk.search_oneshot" -> "search_oneshot"
    parts = mcp_tool_name.split(".", 1)
    operation = parts[1] if len(parts) > 1 else mcp_tool_name
    return f"{target}___{operation}"


def outbound_tool_name(mcp_tool_name: str) -> str:
    """Name placed on the wire.

    PLAIN_MCP off: AgentCore ``Target___operation`` (unchanged).
    PLAIN_MCP on: the worker's plain tool name, with no rewrite.
    The OSS validation shim already accepts both forms; this client does
    not reimplement that shim.
    """
    if plain_mcp_enabled():
        return mcp_tool_name
    return _to_gateway_tool_name(mcp_tool_name)


def tool_identity(name: str) -> tuple[str, str] | None:
    """Map a plain or AgentCore tool name to ``(server, operation)``.

    Used to compare a worker's tool with a ``tools/list`` entry. This is
    not the gateway shim's dispatcher.
    """
    raw = (name or "").strip()
    if not raw:
        return None
    if "___" in raw:
        target, operation = raw.split("___", 1)
        target, operation = target.strip(), operation.strip()
        if not target or not operation:
            return None
        for server, configured in _SERVER_TO_TARGET.items():
            if configured == target or configured.lower() == target.lower():
                return server, operation
        lowered = target.lower().removesuffix("target")
        if lowered in _SERVER_TO_TARGET:
            return lowered, operation
        return None
    if "." in raw:
        server, operation = raw.split(".", 1)
        server, operation = server.strip().lower(), operation.strip()
        if server and operation:
            return server, operation
    return None


def _parse_tool_text(text: str, status: Any, mcp_tool_name: str) -> dict[str, Any]:
    """Parse tool content text. ``status == "error"`` stays an error dict."""
    try:
        parsed = json.loads(text) if text else {}
    except (json.JSONDecodeError, TypeError):
        if status == "error":
            return _failure_dict(
                mcp_tool_name, RuntimeError(text or "tool status error"), plain=plain_mcp_enabled(),
            )
        return {"raw_response": text}
    if isinstance(parsed, dict):
        if status == "error" and "error" not in parsed:
            parsed = dict(parsed)
            parsed["error"] = text or "tool status error"
        return parsed
    if status == "error":
        return _failure_dict(
            mcp_tool_name, RuntimeError(text or "tool status error"), plain=plain_mcp_enabled(),
        )
    return {"raw_response": text}


def normalize_mcp_result(result: Any, mcp_tool_name: str) -> dict[str, Any]:
    """Normalize an MCP tool result to a dict.

    A tool ``status == "error"`` becomes an error dict (never a stub).
    An MCP envelope (``toolUseId`` + ``content``) is unwrapped once so
    callers see the tool JSON, including ``skipped: true``. If that JSON
    is itself envelope-shaped, it is returned as-is and not opened again.
    A plain dict that is already a tool payload is returned unchanged.
    ``gateway_exception``, ``failed:``, and ``connection_state`` are left
    in place.
    """
    if isinstance(result, dict):
        if "toolUseId" in result and isinstance(result.get("content"), list):
            return _parse_tool_text(
                _content_text(result.get("content")), result.get("status"), mcp_tool_name,
            )
        if result.get("status") == "error" and "error" not in result:
            return _failure_dict(
                mcp_tool_name,
                RuntimeError(_content_text(result.get("content")) or "tool status error"),
                plain=plain_mcp_enabled(),
            )
        return result
    status = getattr(result, "status", None)
    content_parts = getattr(result, "content", None)
    if status == "error":
        text = _content_text(content_parts) or "tool status error"
        return _parse_tool_text(text, status, mcp_tool_name)
    if content_parts is not None:
        return _parse_tool_text(_content_text(content_parts), status, mcp_tool_name)
    return {"raw_response": str(result)}


def _content_text(content_parts: Any) -> str:
    if not isinstance(content_parts, list):
        return ""
    text_parts: list[str] = []
    for part in content_parts:
        if isinstance(part, dict) and "text" in part:
            text_parts.append(str(part["text"]))
        elif hasattr(part, "text"):
            text_parts.append(str(part.text))
    return "\n".join(text_parts)


def tool_names_from_list(payload: Any) -> list[str]:
    """Extract tool names from an MCP ``tools/list`` result. Order preserved."""
    if isinstance(payload, dict) and "tools" in payload:
        items = payload["tools"]
    elif hasattr(payload, "tools"):
        items = payload.tools
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    names: list[str] = []
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
        elif hasattr(item, "tool_name"):
            # strands MCPAgentTool.tool_name is a string property. Older
            # objects expose a tool_name() method. Accept both.
            value = item.tool_name() if callable(item.tool_name) else item.tool_name
            if value:
                names.append(str(value))
        elif getattr(item, "name", None):
            names.append(str(item.name))
    return names


# =========================================================================
# Token-bucket rate limiter (per MCP server)
# =========================================================================

# Default rate limits per MCP server (requests-per-minute, 0 = unlimited)
_DEFAULT_RATE_LIMITS: dict[str, int] = {
    "moogsoft": 60,
    "splunk": 0,      # unlimited
    "sysdig": 100,
    "signalfx": 60,
    "dynatrace": 100,
    "servicenow": 60,
    "github": 30,
}


class _TokenBucket:
    """Thread-safe token-bucket rate limiter for a single server.

    Efficiency: non-blocking fast path when tokens are available.
    Sleep is capped at 0.5s per iteration to prevent thread starvation.
    """

    __slots__ = ("_capacity", "_tokens", "_refill_rate", "_last_refill", "_lock")

    # Max sleep per iteration to avoid thread starvation in concurrent workloads
    _MAX_SLEEP_SECONDS = 0.5

    def __init__(self, requests_per_minute: int) -> None:
        # 0 means unlimited — set very large capacity
        if requests_per_minute <= 0:
            self._capacity = float("inf")
            self._tokens = float("inf")
            self._refill_rate = 0.0
        else:
            self._capacity = float(requests_per_minute)
            self._tokens = float(requests_per_minute)
            self._refill_rate = requests_per_minute / 60.0  # tokens per second
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 5.0) -> bool:
        """Try to acquire a token.  Returns True if allowed, False if rate-limited.

        Refills tokens based on elapsed time, then consumes one.
        Blocks up to *timeout* seconds waiting for a token to become available.
        Sleep is capped per iteration to prevent thread starvation under
        concurrent ThreadPoolExecutor workloads.
        """
        if self._capacity == float("inf"):
            return True

        deadline = time.monotonic() + timeout
        with self._lock:
            first_try = True
            while True:
                now = time.monotonic()
                # Allow the first attempt even with timeout=0
                if not first_try and now >= deadline:
                    return False
                first_try = False

                elapsed = now - self._last_refill
                self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True

                # Non-blocking: if timeout=0, fail immediately after first check
                if timeout <= 0:
                    return False

                # How long until one token is available?
                wait = (1.0 - self._tokens) / self._refill_rate if self._refill_rate > 0 else timeout + 1
                if now + wait > deadline:
                    return False

                # Release lock while waiting, then reacquire
                # Cap sleep to prevent thread starvation in pooled executors
                sleep_time = min(wait, deadline - now, self._MAX_SLEEP_SECONDS)
                self._lock.release()
                try:
                    time.sleep(sleep_time)
                finally:
                    self._lock.acquire()


class RateLimiterRegistry:
    """Registry of per-server token-bucket rate limiters.

    Set ``unlimited=True`` (or env RATE_LIMITER_DISABLED=1) to bypass all
    rate limiting — useful for tests and local dev where stub responses
    are instant and rate limits cause unnecessary thread contention.
    """

    def __init__(
        self,
        limits: dict[str, int] | None = None,
        unlimited: bool = False,
    ) -> None:
        self._limits = limits or _DEFAULT_RATE_LIMITS
        self._buckets: dict[str, _TokenBucket] = {}
        self._lock = threading.Lock()
        self._unlimited = unlimited or os.environ.get("RATE_LIMITER_DISABLED", "").lower() in ("1", "true", "yes")

    def acquire(self, server: str, timeout: float = 5.0) -> bool:
        """Acquire a rate-limit token for *server*.  Returns False if blocked."""
        if self._unlimited:
            return True
        bucket = self._get_bucket(server)
        return bucket.acquire(timeout)

    def _get_bucket(self, server: str) -> _TokenBucket:
        with self._lock:
            if server not in self._buckets:
                rpm = self._limits.get(server, 0)  # default: unlimited
                self._buckets[server] = _TokenBucket(rpm)
            return self._buckets[server]


# =========================================================================
# McpGateway — singleton class fronting all MCP servers via AgentCore
# =========================================================================

# One guard for lazily attaching a build lock to ``McpGateway.__new__`` doubles.
_MCP_CLIENT_LOCK_GUARD = threading.Lock()


class _McpStartFlight:
    """One in-flight create+start, shared by every waiter in that wave."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.client: Any = None
        self.error: BaseException | None = None


class McpGateway:
    """Unified gateway that fronts all MCP tool targets on AgentCore.

    Uses the MCP protocol over streamable HTTP to communicate with the
    AgentCore gateway.  This is the production-grade pattern from the
    amazon-bedrock-agentcore-samples SRE agent reference implementation.

    Every worker MUST route MCP calls through a gateway instance.
    The gateway owns:
      - MCPClient lifecycle (lazy init, singleton)
      - OAuth2 credential provider (client_credentials + token caching)
      - Tool name mapping (internal dotted -> gateway triple-underscore)
      - Transport (streamablehttp_client via MCP protocol)
      - Stub fallback (local dev / tests when gateway URL not set)
      - Structured logging at the transport boundary

    Authentication priority:
      1. OAuth2 client_credentials (if GATEWAY_OAUTH2_CLIENT_ID is set)
      2. Static Bearer token (if GATEWAY_ACCESS_TOKEN is set)
      3. No auth headers (local dev / tests)

    Usage:
        gateway = McpGateway.get_instance()
        result = gateway.invoke("splunk.search_oneshot", "search_logs", params)

    Injection:
        Workers accept an optional gateway parameter in __init__().
        The supervisor injects the shared gateway instance.
    """

    _instance: McpGateway | None = None

    def __init__(
        self,
        oauth2_provider: OAuth2CredentialProvider | None = None,
        rate_limiter: RateLimiterRegistry | None = None,
    ) -> None:
        self._mcp_client = None
        self._mcp_client_lock = threading.Lock()
        self._mcp_start_flight: _McpStartFlight | None = None
        self._client_error_slot = threading.local()
        self._tools_cache: tuple[float, frozenset[str]] | None = None
        # Legacy boto3 client for backward compat during migration
        self._boto3_client = None
        # OAuth2 provider (lazy-init from env if not injected)
        self._oauth2_provider = oauth2_provider
        # Fast-path: skip rate limiting for in-process stubs
        stub_mode = force_stub_gateway() or not resolved_gateway_url()
        self._rate_limiter = rate_limiter or RateLimiterRegistry(
            unlimited=stub_mode,
        )
        # Duplicate-call suppression (MCP_DEDUP_ENABLED=true to activate)
        self._call_signatures: set[str] = set()
        self._mode = resolved_gateway_mode()

    @classmethod
    def get_instance(cls) -> McpGateway:
        """Return the singleton gateway.  Thread-safe for read-only access."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton (for tests only)."""
        if cls._instance is not None:
            cls._instance.dispose()
        cls._instance = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def invoke(
        self,
        mcp_tool_name: str,
        tool_action: str,
        params: dict[str, Any],
        user_identity: str | None = None,
    ) -> dict[str, Any]:
        """Invoke an MCP tool via the AgentCore gateway.

        Routes through the MCP protocol (streamable HTTP) when the gateway
        is configured and GATEWAY_MODE is not stub.  Falls back to legacy
        invoke_inline_agent if only per-server ARNs are set.  Returns stub
        responses for local dev/tests and when GATEWAY_MODE=stub.

        Args:
            mcp_tool_name: Internal dotted tool name (e.g. "splunk.search_oneshot")
            tool_action: The action/method on the MCP server
            params: Parameters to pass to the tool
            user_identity: Optional user identity string to propagate via
                X-User-Identity header for downstream authorization (G3.2).

        Returns:
            Response dict from the MCP tool, or error dict on failure.
        """
        # G3.2: Store user identity for header propagation
        self._current_user_identity = user_identity

        # Policy gate pre-check (POLICY_GATE_ENABLED=false by default — no-op in normal operation)
        try:
            from supervisor.policy_gate import evaluate as _policy_evaluate
            _policy_result = _policy_evaluate(mcp_tool_name, params)
            if not _policy_result.allowed:
                logger.warning(
                    "Policy gate REJECTED tool=%s reason=%s",
                    mcp_tool_name, _policy_result.reason,
                )
                return {"error": "policy_rejected", "reason": _policy_result.reason, "tool": mcp_tool_name}
        except ImportError:
            pass  # policy_gate not available — allow

        # Duplicate-call suppression (feature-flagged — default off)
        if os.environ.get("MCP_DEDUP_ENABLED", "false").lower() in ("1", "true", "yes"):
            try:
                _sig_params = json.dumps(params, sort_keys=True) if isinstance(params, dict) else str(params)
            except Exception:
                _sig_params = str(params)
            _sig = f"{mcp_tool_name}:{tool_action}:{_sig_params}"
            if _sig in self._call_signatures:
                logger.debug("Dedup: skipping identical call %s:%s", mcp_tool_name, tool_action)
                return {
                    "status": "skipped",
                    "result": "duplicate_call",
                    "note": "identical call already executed in this session",
                    "worker": mcp_tool_name,
                    "action": tool_action,
                }
            self._call_signatures.add(_sig)

        # Rate-limit check (per-server token bucket)
        server = _TOOL_TO_SERVER.get(mcp_tool_name, "")
        if server and not self._rate_limiter.acquire(server):
            logger.warning(
                "Rate limited: server=%s tool=%s", server, mcp_tool_name,
            )
            return {"error": "rate_limited", "server": server, "tool": mcp_tool_name}

        # GATEWAY_MODE=stub wins over a configured URL (compose sets both).
        if force_stub_gateway():
            logger.debug(
                "GATEWAY_MODE=%s — returning stub for %s", GATEWAY_MODE, mcp_tool_name,
            )
            return _stub_response(mcp_tool_name, tool_action, params)

        url = resolved_gateway_url()
        # Live evidence must not reach a non-null model while masking is absent.
        # Fixture mode returned above, so this only applies to a configured endpoint.
        if url or self.get_arn_for_tool(mcp_tool_name):
            blocked = plain_mcp_model_block_reason()
            if blocked:
                return {
                    "error": blocked,
                    "tool": mcp_tool_name,
                    "error_class": "PlainMcpModelGuard",
                    "connection_state": "failed",
                }

        # Priority 1: AgentCore gateway (MCP protocol — production path)
        if url and _MCP_SDK_AVAILABLE:
            return self._invoke_via_gateway(mcp_tool_name, tool_action, params)

        # URL set but the MCP SDK/client cannot be built. Legacy ARN still wins
        # when one is configured. Otherwise the historical path substitutes a
        # stub; plain mode fails closed instead.
        if url and not _MCP_SDK_AVAILABLE:
            arn = self.get_arn_for_tool(mcp_tool_name)
            if arn:
                return self._invoke_via_legacy(mcp_tool_name, tool_action, params, arn)
            if plain_mcp_enabled():
                return _failure_dict(
                    mcp_tool_name, RuntimeError("MCP SDK not installed"), plain=True,
                )
            logger.warning("MCP SDK not installed — stub substitution for %s", mcp_tool_name)
            return _mark_stub_substitution(_stub_response(mcp_tool_name, tool_action, params))

        # Priority 2: Legacy per-server ARNs (invoke_inline_agent)
        arn = self.get_arn_for_tool(mcp_tool_name)
        if arn:
            return self._invoke_via_legacy(mcp_tool_name, tool_action, params, arn)

        # Priority 3: Stub responses (local dev / tests). Unchanged bytes.
        logger.debug("No gateway or ARN configured for %s — returning stub", mcp_tool_name)
        return _stub_response(mcp_tool_name, tool_action, params)

    # ------------------------------------------------------------------ #
    # AgentCore gateway invocation (production path)
    # ------------------------------------------------------------------ #

    def _invoke_via_gateway(
        self, mcp_tool_name: str, tool_action: str, params: dict[str, Any],
        _is_retry: bool = False,
    ) -> dict[str, Any]:
        """Invoke via AgentCore gateway using MCPClient + streamablehttp_client.

        On 401 (Unauthorized), invalidates the OAuth2 token and retries once
        to handle token expiry during mid-flight requests.
        """
        gateway_tool_name = outbound_tool_name(mcp_tool_name)
        tool_use_id = f"sentinalai-{uuid.uuid4().hex[:12]}"

        start = time.monotonic()
        try:
            client = self._get_mcp_client()
            if client is None:
                if plain_mcp_enabled():
                    exc = self._consume_client_error() or RuntimeError(
                        "MCP client could not be built",
                    )
                    return _failure_dict(mcp_tool_name, exc, plain=True)
                logger.warning("MCPClient unavailable — returning stub for %s", mcp_tool_name)
                return _mark_stub_substitution(
                    _stub_response(mcp_tool_name, tool_action, params),
                )

            timeout_s = mcp_call_timeout_seconds()
            try:
                result = _call_with_timeout(
                    lambda: client.call_tool_sync(
                        tool_use_id=tool_use_id,
                        name=gateway_tool_name,
                        arguments=params,
                        read_timeout_seconds=timedelta(seconds=timeout_s),
                    ),
                    timeout_s,
                )
            except TimeoutError as exc:
                # The call may still be running on the daemon thread. Drop the
                # client (and the session it owns) so the next invoke builds a
                # new one instead of reusing a stuck connection.
                self._drop_mcp_client()
                return _failure_dict(mcp_tool_name, exc, plain=plain_mcp_enabled())

            elapsed_ms = (time.monotonic() - start) * 1000
            logger.info(
                "MCP gateway call: tool=%s gateway_name=%s elapsed=%.1fms",
                mcp_tool_name, gateway_tool_name, elapsed_ms,
            )
            return normalize_mcp_result(result, mcp_tool_name)

        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000

            # 401 retry: invalidate OAuth2 token and retry once
            if _is_unauthorized(exc) and not _is_retry and self._oauth2_provider is not None:
                logger.warning(
                    "MCP gateway 401 for %s — invalidating token and retrying",
                    mcp_tool_name,
                )
                self._oauth2_provider.invalidate()
                # Force new MCPClient with fresh auth headers
                self._drop_mcp_client()
                return self._invoke_via_gateway(
                    mcp_tool_name, tool_action, params, _is_retry=True,
                )

            logger.error(
                "MCP gateway call failed: tool=%s error=%s elapsed=%.1fms",
                mcp_tool_name, exc, elapsed_ms,
            )
            if plain_mcp_enabled():
                return _failure_dict(mcp_tool_name, exc, plain=True)
            return {"error": f"gateway_exception: {exc}", "tool": mcp_tool_name}

    def _get_auth_headers(self) -> dict[str, str]:
        """Resolve authentication headers using the configured auth method.

        Priority:
            1. OAuth2 credential provider (client_credentials with auto-refresh)
            2. Static Bearer token (GATEWAY_ACCESS_TOKEN env var)
            3. Empty dict (no auth — local dev / tests)

        G3.2: Includes X-User-Identity header when user identity is available.
        """
        headers: dict[str, str] = {}

        # Lazy-init OAuth2 provider from env on first call
        if self._oauth2_provider is None:
            self._oauth2_provider = OAuth2CredentialProvider.from_env()

        # Priority 1: OAuth2 client_credentials
        if self._oauth2_provider is not None:
            auth_headers = self._oauth2_provider.get_auth_headers()
            if auth_headers:
                headers.update(auth_headers)
            # OAuth2 configured but token acquisition failed — fall through

        # Priority 2: Static Bearer token
        if not headers and GATEWAY_ACCESS_TOKEN:
            headers["Authorization"] = f"Bearer {GATEWAY_ACCESS_TOKEN}"

        # G3.2: Propagate user identity to downstream MCPs
        user_identity = getattr(self, "_current_user_identity", None)
        if user_identity:
            headers["X-User-Identity"] = user_identity

        return headers

    def _ensure_client_state(self) -> None:
        """Attach the build lock, flight slot, and per-thread error if needed."""
        if (
            getattr(self, "_mcp_client_lock", None) is not None
            and getattr(self, "_client_error_slot", None) is not None
            and hasattr(self, "_mcp_start_flight")
        ):
            return
        with _MCP_CLIENT_LOCK_GUARD:
            if getattr(self, "_mcp_client_lock", None) is None:
                self._mcp_client_lock = threading.Lock()
            if getattr(self, "_client_error_slot", None) is None:
                self._client_error_slot = threading.local()
            if not hasattr(self, "_mcp_start_flight"):
                self._mcp_start_flight = None

    def _remember_client_error(self, exc: BaseException | None) -> None:
        """Record a build/start failure for the calling thread only."""
        self._ensure_client_state()
        slot = self._client_error_slot
        if exc is None:
            if hasattr(slot, "exc"):
                del slot.exc
            return
        slot.exc = exc

    def _consume_client_error(self) -> BaseException | None:
        """Return and clear this thread's build/start failure, if any."""
        self._ensure_client_state()
        slot = self._client_error_slot
        exc = getattr(slot, "exc", None)
        if hasattr(slot, "exc"):
            del slot.exc
        return exc

    def _drop_mcp_client(self) -> None:
        """Forget the live client. The next call builds and starts a new one."""
        self._ensure_client_state()
        with self._mcp_client_lock:
            self._mcp_client = None

    def _claim_start_flight(self) -> tuple[Any, _McpStartFlight | None, bool]:
        """Join the in-flight start, or become its leader.

        Returns ``(client, flight, is_leader)``. ``client`` is the instance
        observed while holding the lock when one is already published.
        Callers must use that object: a later read of ``_mcp_client`` can
        be ``None`` if a timeout drops it after this lock is released.
        """
        with self._mcp_client_lock:
            client = self._mcp_client
            if client is not None:
                return client, None, False
            flight = self._mcp_start_flight
            if flight is not None:
                return None, flight, False
            flight = _McpStartFlight()
            self._mcp_start_flight = flight
            return None, flight, True

    def _join_start_flight(self, flight: _McpStartFlight) -> Any:
        """Return the leader's client, or None and the same failure."""
        flight.done.wait()
        if flight.client is not None:
            return flight.client
        self._remember_client_error(flight.error)
        return None

    def _finish_start_flight(self, flight: _McpStartFlight) -> None:
        """End this wave. The slot is cleared before waiters wake, so a caller
        that arrives as the wave ends may retry instead of joining it.
        """
        with self._mcp_client_lock:
            if self._mcp_start_flight is flight:
                self._mcp_start_flight = None
        flight.done.set()

    def _build_mcp_client(self, gateway_url: str) -> Any:
        # Capture self for the lambda so auth headers are resolved
        # dynamically on each connection (picks up refreshed tokens).
        gw_self = self
        return MCPClient(
            lambda: streamablehttp_client(
                url=gateway_url,
                headers=gw_self._get_auth_headers(),
            ),
        )

    def _lead_start_flight(self, flight: _McpStartFlight, gateway_url: str) -> Any:
        """Create and start one client. Publish it only after start() returns."""
        if not gateway_url.endswith("/mcp"):
            gateway_url = f"{gateway_url}/mcp"
        try:
            client = self._build_mcp_client(gateway_url)
        except Exception as exc:
            logger.warning("Failed to create MCPClient: %s", exc)
            flight.error = exc
            self._remember_client_error(exc)
            return None
        try:
            # strands MCPClient refuses call_tool_sync until start() has
            # opened the session. Do not publish the client before that.
            _call_with_timeout(
                client.start,
                mcp_call_timeout_seconds(),
                on_timeout=lambda worker, box: _reap_late_start(client, worker, box),
            )
        except Exception as exc:
            logger.warning("Failed to start MCPClient: %s", exc)
            flight.error = exc
            self._remember_client_error(exc)
            return None
        except BaseException as exc:
            flight.error = exc
            raise
        with self._mcp_client_lock:
            self._mcp_client = client
        flight.client = client
        logger.info("MCPClient connected to AgentCore gateway: %s", gateway_url)
        return client

    def _get_mcp_client(self):
        """Lazily create the MCPClient connected to the AgentCore gateway.

        The transport factory lambda calls _get_auth_headers() on each
        connection so that refreshed OAuth2 tokens are picked up automatically.

        One wave of concurrent first calls shares a single create+start.
        Waiters receive that attempt's client or its failure. The client is
        published only after ``start()`` returns. A later call, after the
        wave has finished, may retry. A start failure is remembered on this
        thread for plain mode; flag off still treats a missing client as a stub.
        """
        self._ensure_client_state()
        # Drop a stale failure before this attempt. Only an error raised
        # below is reported to the caller.
        self._remember_client_error(None)
        if self._mcp_client is not None:
            return self._mcp_client
        if not _MCP_SDK_AVAILABLE:
            logger.debug("strands/mcp SDK not installed — MCP gateway disabled")
            return None
        gateway_url = resolved_gateway_url()
        if not gateway_url:
            return None
        held, flight, leader = self._claim_start_flight()
        if flight is None:
            return held
        if not leader:
            return self._join_start_flight(flight)
        try:
            return self._lead_start_flight(flight, gateway_url)
        finally:
            self._finish_start_flight(flight)

    # ------------------------------------------------------------------ #
    # Legacy invocation (invoke_inline_agent — deprecated)
    # ------------------------------------------------------------------ #

    def _invoke_via_legacy(
        self, mcp_tool_name: str, tool_action: str, params: dict[str, Any], arn: str,
    ) -> dict[str, Any]:
        """Legacy: invoke via bedrock-agent-runtime invoke_inline_agent.

        Deprecated in favor of AgentCore gateway.  Kept for backward
        compatibility during migration.
        """
        client = self._get_boto3_client()
        if client is None:
            logger.warning("No bedrock-agent-runtime client — returning stub for %s", mcp_tool_name)
            return _stub_response(mcp_tool_name, tool_action, params)

        start = time.monotonic()
        try:
            response = client.invoke_inline_agent(
                inputText=json.dumps({
                    "tool": mcp_tool_name,
                    "action": tool_action,
                    "parameters": params,
                }),
                sessionId=params.get("session_id", "sentinalai-default"),
                enableTrace=True,
                inlineSessionState={
                    "invocationId": f"mcp-{mcp_tool_name}-{int(time.time())}",
                },
            )

            elapsed_ms = (time.monotonic() - start) * 1000
            result = _parse_agent_response(response)

            logger.info(
                "MCP legacy call: tool=%s action=%s elapsed=%.1fms",
                mcp_tool_name, tool_action, elapsed_ms,
            )
            return result

        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            if _BOTO3_AVAILABLE and _ClientError and isinstance(exc, _ClientError):
                error_code = exc.response.get("Error", {}).get("Code", "Unknown")
                logger.error(
                    "MCP call failed: tool=%s error=%s elapsed=%.1fms",
                    mcp_tool_name, error_code, elapsed_ms,
                )
                return {"error": f"mcp_call_failed: {error_code}", "tool": mcp_tool_name}
            logger.error(
                "MCP call exception: tool=%s error=%s elapsed=%.1fms",
                mcp_tool_name, exc, elapsed_ms,
            )
            return {"error": f"mcp_exception: {exc}", "tool": mcp_tool_name}

    def _get_boto3_client(self):
        """Lazily create the bedrock-agent-runtime boto3 client (legacy)."""
        if self._boto3_client is not None:
            return self._boto3_client
        if not _BOTO3_AVAILABLE:
            logger.debug("boto3 not installed — legacy MCP calls disabled")
            return None
        try:
            self._boto3_client = boto3.client(
                "bedrock-agent-runtime",
                region_name=AWS_REGION,
                config=BotoConfig(
                    retries={"max_attempts": MCP_MAX_RETRIES, "mode": "adaptive"},
                    connect_timeout=10,
                    read_timeout=MCP_CALL_TIMEOUT,
                ),
            )
            return self._boto3_client
        except Exception as exc:
            logger.warning("Failed to create bedrock-agent-runtime client: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    # Tool discovery
    # ------------------------------------------------------------------ #

    # All known server names (used as fallback when gateway is unavailable)
    _ALL_KNOWN_SERVERS: frozenset[str] = frozenset(
        {"moogsoft", "splunk", "sysdig", "signalfx", "dynatrace",
         "servicenow", "github", "confluence", "kubernetes"}
    )

    # Cache TTL for discovery results (5 minutes)
    _DISCOVERY_TTL_SECONDS = int(os.environ.get("TOOL_DISCOVERY_TTL_SECONDS", "300"))

    def discover_tools(self, *, force_refresh: bool = False) -> frozenset[str]:
        """Query the gateway (or stub) for currently connected tool servers.

        Returns a frozenset of server names that are reachable, e.g.
        ``frozenset({"splunk", "servicenow", "github"})``.

        Caches results for ``_DISCOVERY_TTL_SECONDS`` (default 5 min).
        Falls back to all known servers on any network/parse error so the
        agent always starts, even if the gateway is temporarily unavailable.

        Override the discovery URL via:
            TOOL_DISCOVERY_URL  — explicit URL (e.g. http://stub-tools:9000/tools)
        """
        now = time.monotonic()
        if not force_refresh and self._tools_cache is not None:
            cached_at, cached_servers = self._tools_cache
            if now - cached_at < self._DISCOVERY_TTL_SECONDS:
                return cached_servers

        servers = self._fetch_available_servers()
        self._tools_cache = (now, servers)
        logger.info("Tool discovery complete: %s", sorted(servers))
        return servers

    def _fetch_available_servers(self) -> frozenset[str]:
        """Attempt HTTP discovery; return all known servers on any failure."""
        if not _REQUESTS_AVAILABLE:
            logger.debug("requests not available — assuming all tools connected")
            return self._ALL_KNOWN_SERVERS

        # Determine discovery URL
        explicit_url = os.environ.get("TOOL_DISCOVERY_URL", "")
        stub_url = os.environ.get("STUB_TOOLS_URL", "")

        if explicit_url:
            discovery_url = explicit_url
        elif force_stub_gateway():
            # Do not probe the gateway URL while the process is in stub mode.
            if stub_url:
                discovery_url = stub_url.rstrip("/") + "/tools"
            else:
                logger.debug("GATEWAY_MODE=stub — assuming all in-process stub tools")
                return self._ALL_KNOWN_SERVERS
        elif resolved_gateway_url():
            discovery_url = resolved_gateway_url().rstrip("/") + "/tools"
        elif stub_url:
            discovery_url = stub_url.rstrip("/") + "/tools"
        else:
            logger.debug("No discovery URL configured — assuming all tools connected")
            return self._ALL_KNOWN_SERVERS

        try:
            resp = _requests_lib.get(discovery_url, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            return self._parse_discovery_response(data)
        except Exception as exc:
            logger.warning("Tool discovery failed (%s) — assuming all tools connected", exc)
            return self._ALL_KNOWN_SERVERS

    @staticmethod
    def _parse_discovery_response(data: dict) -> frozenset[str]:
        """Parse discovery endpoint response into a set of server names.

        Handles three response formats:
          1. {"available_servers": ["splunk", "github", ...]}       — standard
          2. {"stub_tools": {"splunk": [...], "github": [...], ...}} — stub catalog
          3. {"tools": [{"server": "splunk", ...}, ...]}             — gateway list
        """
        if "available_servers" in data:
            return frozenset(str(s).lower() for s in data["available_servers"])
        if "stub_tools" in data:
            return frozenset(str(k).lower() for k in data["stub_tools"].keys())
        if "tools" in data and isinstance(data["tools"], list):
            servers: set[str] = set()
            for item in data["tools"]:
                if isinstance(item, dict) and "server" in item:
                    servers.add(str(item["server"]).lower())
                elif isinstance(item, str):
                    # "SplunkTarget___search_oneshot" → "splunk"
                    name = item.lower()
                    for known in McpGateway._ALL_KNOWN_SERVERS:
                        if name.startswith(known):
                            servers.add(known)
                            break
            return frozenset(servers) if servers else McpGateway._ALL_KNOWN_SERVERS
        # Unknown format — assume all available
        return McpGateway._ALL_KNOWN_SERVERS

    # ------------------------------------------------------------------ #
    # Resolution helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def get_server_for_tool(mcp_tool_name: str) -> str:
        """Map an MCP tool name to its server."""
        return _TOOL_TO_SERVER.get(mcp_tool_name, "")

    @staticmethod
    def get_arn_for_tool(mcp_tool_name: str) -> str:
        """Get the AgentCore tool ARN for an MCP tool name (legacy)."""
        server = _TOOL_TO_SERVER.get(mcp_tool_name, "")
        return MCP_TOOL_ARNS.get(server, "")

    # ------------------------------------------------------------------ #
    # Client lifecycle
    # ------------------------------------------------------------------ #

    def clear_call_signatures(self) -> None:
        """Clear the duplicate-call suppression set (for testing)."""
        self._call_signatures.clear()

    def dispose(self) -> None:
        """Release clients, tokens, and cached state."""
        self._mcp_client = None
        self._boto3_client = None
        self._tools_cache = None
        if self._oauth2_provider is not None:
            self._oauth2_provider.invalidate()


# =========================================================================
# Module-level backward-compatible API
# =========================================================================
# These functions delegate to the singleton McpGateway so that existing
# code using `from workers.mcp_client import invoke_mcp_tool` continues
# to work without changes.  New code should use the gateway instance.

_client = None  # legacy — kept for test_mcp_client_coverage compatibility


def _get_client():
    """Legacy: lazily create the bedrock-agent-runtime boto3 client."""
    gw = McpGateway.get_instance()
    return gw._get_boto3_client()


def get_server_for_tool(mcp_tool_name: str) -> str:
    """Map an MCP tool name to its server."""
    return _TOOL_TO_SERVER.get(mcp_tool_name, "")


def get_arn_for_tool(mcp_tool_name: str) -> str:
    """Get the AgentCore tool ARN for an MCP tool name (legacy)."""
    server = get_server_for_tool(mcp_tool_name)
    return MCP_TOOL_ARNS.get(server, "")


def invoke_mcp_tool(
    mcp_tool_name: str,
    tool_action: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Invoke an MCP tool via the singleton McpGateway.

    Backward-compatible wrapper.  Workers that accept a gateway
    parameter should call gateway.invoke() directly instead.
    """
    return McpGateway.get_instance().invoke(mcp_tool_name, tool_action, params)


def dispose() -> None:
    """Release all clients (legacy + gateway)."""
    global _client
    _client = None
    McpGateway.get_instance().dispose()


# =========================================================================
# Response parsing (legacy — for invoke_inline_agent responses)
# =========================================================================

def _parse_agent_response(response: dict) -> dict[str, Any]:
    """Parse the bedrock-agent-runtime streaming response into a dict."""
    completion = response.get("completion", [])
    result_text = ""

    if isinstance(completion, str):
        result_text = completion
    elif isinstance(completion, list):
        for event in completion:
            if isinstance(event, dict):
                chunk = event.get("chunk", {})
                if isinstance(chunk, dict) and "bytes" in chunk:
                    result_text += chunk["bytes"].decode("utf-8", errors="replace")
    elif hasattr(completion, "__iter__"):
        for event in completion:
            if isinstance(event, dict):
                chunk = event.get("chunk", {})
                if isinstance(chunk, dict) and "bytes" in chunk:
                    result_text += chunk["bytes"].decode("utf-8", errors="replace")

    # Try to parse as JSON
    if result_text:
        try:
            return json.loads(result_text)
        except (json.JSONDecodeError, TypeError):
            return {"raw_response": result_text}

    return {"raw_response": result_text or "empty"}


# =========================================================================
# Stub responses (fallback when gateway not configured)
# =========================================================================

def _stub_moogsoft(action: str, params: dict) -> dict:
    incident_id = params.get("incident_id", "unknown")
    return {"incident": {"incident_id": incident_id, "status": "pending"}}


def _stub_splunk(action: str, params: dict) -> dict:
    if "change" in action:
        return {"changes": []}
    return {"logs": {"results": [], "count": 0}}


def _stub_sysdig(action: str, params: dict) -> dict:
    if "event" in action:
        return {"events": []}
    if "golden" in action or "signal" in action:
        return {"signals": {}}
    return {"metrics": {"metrics": [], "baseline": 0}}


def _stub_signalfx(action: str, params: dict) -> dict:
    if "golden" in action or "signal" in action:
        return {"signals": {}}
    return {"metrics": {}}


def _stub_dynatrace(action: str, params: dict) -> dict:
    if "problem" in action:
        return {"problems": []}
    if "event" in action:
        return {"events": []}
    return {"metrics": {}}


def _stub_servicenow(action: str, params: dict) -> dict:
    if "incident" in action:
        return {"incidents": []}
    if "ci_detail" in action or action == "get_ci_details":
        return {"ci": {}}
    if "change" in action:
        return {"change_records": []}
    if "known" in action or "error" in action:
        return {"known_errors": []}
    return {"ci": {}}


def _stub_github(action: str, params: dict) -> dict:
    if "deployment" in action:
        return {"deployments": []}
    if "create" in action and ("pr" in action or "pull" in action):
        # NEVER fabricate a remediation. In stub mode (no GitHub gateway) no PR
        # is created; say so explicitly so an operator cannot mistake this for a
        # real, mergeable PR. No fake URL, no PR number, no mergeable flag.
        return {
            "pr": None,
            "created": False,
            "stub": True,
            "error": "github_gateway_not_configured",
            "detail": ("No PR was created — GitHub gateway is not configured "
                       "(stub mode). Configure MCP_GATEWAY_URL (or "
                       "AGENTCORE_GATEWAY_URL) to enable "
                       "real remediation."),
        }
    if "pr" in action or "pull" in action:
        return {"pr": None, "stub": True,
                "error": "github_gateway_not_configured",
                "detail": "No PR data — GitHub gateway not configured (stub mode)."}
    if "commit" in action or "diff" in action:
        return {"commit": {}}
    if "workflow" in action:
        return {"workflow_runs": []}
    return {}


def _stub_kubernetes(action: str, params: dict) -> dict:
    service = params.get("service", params.get("deployment", "unknown-service"))
    namespace = params.get("namespace", "default")
    if "rollback" in action:
        return {
            "success": True,
            "deployment": service,
            "namespace": namespace,
            "rolled_back_to": params.get("revision", "previous"),
            "status": "RolloutComplete",
            "message": f"Deployment {service} rolled back successfully",
        }
    if "scale" in action:
        replicas = params.get("replicas", 2)
        return {
            "success": True,
            "deployment": service,
            "namespace": namespace,
            "replicas": replicas,
            "status": "ScalingComplete",
            "message": f"Deployment {service} scaled to {replicas} replicas",
        }
    if "status" in action:
        return {
            "deployment": service,
            "namespace": namespace,
            "ready_replicas": 2,
            "desired_replicas": 2,
            "available": True,
        }
    if "log" in action:
        return {"logs": [], "pod_count": 0}
    return {"success": True, "service": service}


def _stub_confluence(action: str, params: dict) -> dict:
    if "runbook" in action:
        return {"runbooks": []}
    if "postmortem" in action:
        return {"postmortems": []}
    if "page" in action:
        return {"page": {}}
    return {}


_STUB_DISPATCH: dict[str, Any] = {
    "moogsoft": _stub_moogsoft,
    "splunk": _stub_splunk,
    "sysdig": _stub_sysdig,
    "signalfx": _stub_signalfx,
    "dynatrace": _stub_dynatrace,
    "servicenow": _stub_servicenow,
    "github": _stub_github,
    "confluence": _stub_confluence,
    "kubernetes": _stub_kubernetes,
}


def _stub_response(mcp_tool_name: str, tool_action: str, params: dict) -> dict:
    """Return a minimal stub response for local dev / tests."""
    server = get_server_for_tool(mcp_tool_name)
    handler = _STUB_DISPATCH.get(server)
    if handler is None:
        return {}
    return handler(tool_action.lower(), params)


def call_tool(tool_name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke an MCP tool via ``McpGateway`` using the dotted tool name.

    Optional intelligence / GitHub-automation paths historically constructed
    the strands ``MCPClient`` with no transport and called ``.call()``. Route
    those through the gateway so auth, stubs, and rate limits still apply.
    """
    action = tool_name.rsplit(".", 1)[-1]
    return McpGateway.get_instance().invoke(tool_name, action, params or {})
