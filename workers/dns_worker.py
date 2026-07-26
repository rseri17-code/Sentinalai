"""DNS / Route53 evidence worker (IE-2 pilot).

Gated by ``IE_DNS_ENABLED`` (default: false). When disabled, every action returns
``{}`` so existing RCA flows are completely unaffected — the worker is not even
registered by the supervisor when the flag is off (see ``SentinalAISupervisor``),
so this internal guard is a second, defence-in-depth safety net.

When enabled, it queries the Route53 / DNS MCP through the standard gateway
(``route53.get_record`` / ``route53.check_resolver``) and returns additive,
normalized evidence the supervisor merges under ``dns_evidence`` — never mutating
existing keys.

Production contract: mirrors a Route53/DNS MCP.
  get_dns_record   → {"record": {name, points_to, type, ttl} | None}
  check_resolver   → {"resolver": {status, query_timeouts, nxdomain_rate} | None}
"""
from __future__ import annotations

import logging
import os
from typing import Any

from workers.base_worker import BaseWorker

logger = logging.getLogger(__name__)

_ENABLED_VALUES = ("1", "true", "yes")


def _dns_enabled() -> bool:
    return os.environ.get("IE_DNS_ENABLED", "false").lower() in _ENABLED_VALUES


class DnsWorker(BaseWorker):
    """Worker that calls the Route53/DNS MCP for DNS resolution evidence."""

    worker_name = "dns_worker"

    def __init__(self, gateway: Any | None = None) -> None:
        super().__init__()
        from workers.mcp_client import McpGateway
        self._gateway = gateway or McpGateway.get_instance()
        self.register("get_dns_record", self._get_dns_record)
        self.register("check_resolver", self._check_resolver)

    def _get_dns_record(self, params: dict) -> dict:
        """Fetch the DNS record a service resolves to. {} when flag off."""
        if not _dns_enabled():
            return {}
        service = params.get("service", "")
        resp = self._gateway.invoke("route53.get_record", "get_dns_record",
                                    {"service": service})
        record = (resp or {}).get("record")
        return {"record": record} if record else {"record": None}

    def _check_resolver(self, params: dict) -> dict:
        """Check DNS resolver health for a service. {} when flag off."""
        if not _dns_enabled():
            return {}
        service = params.get("service", "")
        resp = self._gateway.invoke("route53.check_resolver", "check_resolver",
                                    {"service": service})
        resolver = (resp or {}).get("resolver")
        return {"resolver": resolver} if resolver else {"resolver": None}
