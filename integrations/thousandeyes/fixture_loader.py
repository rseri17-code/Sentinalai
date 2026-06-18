"""Fixture loader for ThousandEyes offline testing.

When TE_USE_FIXTURES=true, the adapter delegates here instead of calling
the live MCP server. All fixture files are sanitized (RFC 5737 IPs, no
real tokens) and safe to commit.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_FIXTURE_DIR = Path(__file__).parent.parent.parent / "tools" / "thousandeyes_discovery" / "sample_outputs"

_TOOL_TO_FIXTURE: dict[str, str] = {
    "te_list_tests": "list_tests.json",
    "te_list_agents": "list_agents.json",
    "te_list_alerts": "list_alerts.json",
    "te_get_test_results": "get_test_results_http.json",
    "te_get_path_vis": "get_path_vis.json",
}

_SCENARIO_FIXTURES: dict[str, str] = {
    "dns_failure": "dns_failure.json",
    "packet_loss": "packet_loss.json",
    "saas_outage": "saas_outage.json",
    "endpoint_cloud_healthy": "endpoint_cloud_healthy.json",
    "endpoint_enterprise_failed": "endpoint_enterprise_failed.json",
    "empty": "empty_results.json",
}


def fixture_mode_enabled() -> bool:
    return os.environ.get("TE_USE_FIXTURES", "false").lower() in ("1", "true", "yes")


def load(tool_name: str, scenario: str | None = None) -> dict:
    """Load a sanitized fixture for *tool_name*, optionally selecting a *scenario*.

    Falls back to empty dict if the fixture file is missing or malformed.
    """
    if scenario and scenario in _SCENARIO_FIXTURES:
        fixture_file = _FIXTURE_DIR / _SCENARIO_FIXTURES[scenario]
    else:
        filename = _TOOL_TO_FIXTURE.get(tool_name)
        if not filename:
            logger.debug("No fixture mapped for tool %s", tool_name)
            return {}
        fixture_file = _FIXTURE_DIR / filename

    try:
        with open(fixture_file) as f:
            data = json.load(f)
        logger.debug("Loaded fixture: %s", fixture_file.name)
        return data
    except FileNotFoundError:
        logger.warning("Fixture file not found: %s", fixture_file)
        return {}
    except json.JSONDecodeError as exc:
        logger.warning("Fixture parse error (%s): %s", fixture_file.name, exc)
        return {}
