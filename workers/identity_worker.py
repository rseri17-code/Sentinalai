"""Identity / IAM evidence worker (IE-3 pilot).

Gated by ``IE_IDENTITY_ENABLED`` (default: false). When disabled every action
returns ``{}`` and the supervisor does not even register the worker, so existing
RCA flows are completely unaffected.

When enabled it queries the Identity/IAM MCP through the standard gateway and
returns additive, normalized evidence the supervisor merges under
``identity_evidence`` — distinguishing the two authentication/authorization
failure classes the analyzer reasons over:

  check_token_signing → {"signing_key": {kid, status, ...} | None}   (authN)
  get_policy_changes  → {"policy_changes": [ {change, effect, permission} ]}  (authZ)
"""
from __future__ import annotations

import logging
import os
from typing import Any

from workers.base_worker import BaseWorker

logger = logging.getLogger(__name__)

_ENABLED_VALUES = ("1", "true", "yes")


def _identity_enabled() -> bool:
    return os.environ.get("IE_IDENTITY_ENABLED", "false").lower() in _ENABLED_VALUES


class IdentityWorker(BaseWorker):
    """Worker that calls the Identity/IAM MCP for authN/authZ evidence."""

    worker_name = "identity_worker"

    def __init__(self, gateway: Any | None = None) -> None:
        super().__init__()
        from workers.mcp_client import McpGateway
        self._gateway = gateway or McpGateway.get_instance()
        self.register("check_token_signing", self._check_token_signing)
        self.register("get_policy_changes", self._get_policy_changes)

    def _check_token_signing(self, params: dict) -> dict:
        """Authentication: token signing key status. {} when flag off."""
        if not _identity_enabled():
            return {}
        service = params.get("service", "")
        resp = self._gateway.invoke("identity.check_token_signing",
                                    "check_token_signing", {"service": service})
        key = (resp or {}).get("signing_key")
        return {"signing_key": key} if key else {"signing_key": None}

    def _get_policy_changes(self, params: dict) -> dict:
        """Authorization: recent IAM/permission policy changes. {} when flag off."""
        if not _identity_enabled():
            return {}
        service = params.get("service", "")
        resp = self._gateway.invoke("identity.get_policy_changes",
                                    "get_policy_changes", {"service": service})
        changes = (resp or {}).get("policy_changes")
        return {"policy_changes": changes if isinstance(changes, list) else []}
