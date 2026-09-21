"""OSS validation MCP gateway.

A minimal AgentCore-shaped name-shim so ``GATEWAY_MODE=live`` can talk to
Prometheus, Loki, Alertmanager, and (optionally) Kubernetes instead of
Moogsoft/Splunk/Sysdig. Not a production AgentCore replacement.

See ``docs/clone/OSS_VALIDATION.md``.
"""

from __future__ import annotations

__version__ = "0.1.0"
