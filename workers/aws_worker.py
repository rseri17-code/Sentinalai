"""AWS / CloudWatch evidence worker (IE-4).

Gated by ``IE_AWS_ENABLED`` (default: false). When disabled every action returns
``{}`` and the supervisor does not register the worker, so existing RCA flows are
unaffected.

When enabled it queries the AWS CloudWatch MCP through the standard gateway and
returns additive, normalized cloud metrics the supervisor merges under
``aws_evidence`` — S3 error/throttle counts, VPC flow-log rejects, and per-AZ
health used both for AWS-primary root causes and for cross-domain corroboration.

  get_error_metrics → {"metrics": { s3_403, s3_503_slowdown, throttled,
                                    conn_reset, flow_log, az, status,
                                    affected_pct } }
"""
from __future__ import annotations

import logging
import os
from typing import Any

from workers.base_worker import BaseWorker

logger = logging.getLogger(__name__)

_ENABLED_VALUES = ("1", "true", "yes")


def _aws_enabled() -> bool:
    return os.environ.get("IE_AWS_ENABLED", "false").lower() in _ENABLED_VALUES


class AwsWorker(BaseWorker):
    """Worker that calls the AWS/CloudWatch MCP for cloud service evidence."""

    worker_name = "aws_worker"

    def __init__(self, gateway: Any | None = None) -> None:
        super().__init__()
        from workers.mcp_client import McpGateway
        self._gateway = gateway or McpGateway.get_instance()
        self.register("get_error_metrics", self._get_error_metrics)

    def _get_error_metrics(self, params: dict) -> dict:
        """Fetch CloudWatch error/throttle/AZ metrics. {} when flag off."""
        if not _aws_enabled():
            return {}
        service = params.get("service", "")
        resp = self._gateway.invoke("aws_cloudwatch.get_error_metrics",
                                    "get_error_metrics", {"service": service})
        metrics = (resp or {}).get("metrics")
        return {"metrics": metrics if isinstance(metrics, dict) else {}}
