"""APM Worker - handles golden signals and application performance monitoring.

Uses Dynatrace and SignalFx for APM data. Both serve the same purpose:
application-level performance monitoring, golden signals, and problem detection.
Calls MCP servers via AgentCore gateway. Falls back to stub responses for local dev.
"""

from __future__ import annotations

import logging

from workers.base_worker import BaseWorker
from workers.mcp_client import McpGateway

logger = logging.getLogger(__name__)


class ApmWorker(BaseWorker):
    """Worker that interfaces with Dynatrace + SignalFx for APM / golden signals."""

    worker_name = "apm_worker"

    def __init__(self, gateway: McpGateway | None = None):
        super().__init__()
        self._gateway = gateway or McpGateway.get_instance()
        self.register("get_golden_signals", self._get_golden_signals)
        self.register("check_latency", self._get_golden_signals)

    def _get_golden_signals(self, params: dict) -> dict:
        """Get golden signals from Dynatrace (primary) enriched with SignalFx.

        Primary: Dynatrace problems + metrics (APM)
        Enrichment: SignalFx query_signalfx_metrics (APM)

        Both sources serve application-level monitoring. Results are merged
        so the analysis engine gets the broadest APM signal.
        """
        # Primary: Dynatrace APM
        result = self._gateway.invoke(
            "dynatrace.get_metrics",
            "get_golden_signals",
            params,
        )

        # Enrichment: SignalFx APM metrics
        try:
            signalfx_result = self._gateway.invoke(
                "signalfx.query_signalfx_metrics",
                "get_golden_signals",
                params,
            )
            if signalfx_result:
                result["signalfx_apm"] = signalfx_result
        except Exception:
            logger.debug("SignalFx enrichment skipped (non-critical)")

        return result
