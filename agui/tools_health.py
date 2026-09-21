"""Investigation LLM + MCP gateway health payload (no FastAPI / auth imports)."""
from __future__ import annotations


def build_tools_health() -> dict:
    """Actual MCP gateway mode + investigation InferencePort (not unused keys)."""
    from workers.mcp_client import resolved_gateway_mode
    from supervisor.llm import get_inference_port, is_enabled, resolved_provider

    gw_mode = resolved_gateway_mode()
    llm_enabled = bool(is_enabled())
    port = get_inference_port()
    llm_info = {
        "env_var": "LLM_ENABLED / LLM_PROVIDER",
        "configured": llm_enabled,
        "enabled": llm_enabled,
        "provider": resolved_provider(),
        "port": type(port).__name__,
        "mode": "live" if llm_enabled else "disabled",
        "status": "connected" if llm_enabled else "disabled",
        "description": (
            "Investigation LLM overlay via converse()/InferencePort. "
            "Unused ANTHROPIC_API_KEY / OPENAI_API_KEY do not enable this."
        ),
    }

    tools = {
        "servicenow": {
            "env_var": "AGENTCORE_GATEWAY_URL",
            "description": "CMDB, change records, incident write-back",
        },
        "github": {
            "env_var": "AGENTCORE_GATEWAY_URL",
            "description": "Deployment history, code diffs, PR creation",
        },
        "splunk": {
            "env_var": "AGENTCORE_GATEWAY_URL",
            "description": "Log aggregation, error search",
        },
        "sysdig": {
            "env_var": "AGENTCORE_GATEWAY_URL",
            "description": "Infrastructure metrics, golden signals",
        },
        "dynatrace": {
            "env_var": "AGENTCORE_GATEWAY_URL",
            "description": "APM, distributed tracing, error sampling",
        },
        "moogsoft": {
            "env_var": "AGENTCORE_GATEWAY_URL",
            "description": "Alert correlation, incident intake",
        },
        "confluence": {
            "env_var": "AGENTCORE_GATEWAY_URL",
            "description": "Runbooks, post-mortems, knowledge base",
        },
        "kubernetes": {
            "env_var": "AGENTCORE_GATEWAY_URL",
            "description": "Pod management, rollback, scaling",
        },
    }
    for info in tools.values():
        info["configured"] = gw_mode == "live"
        info["mode"] = gw_mode
        info["status"] = "via_gateway" if gw_mode == "live" else "stub_mode"

    tools["llm"] = llm_info
    connected_count = sum(1 for t in tools.values() if t.get("configured"))
    total = len(tools)
    stub_count = sum(1 for t in tools.values() if t.get("mode") in ("stub", "disabled"))

    return {
        "gateway_mode": gw_mode,
        "llm": {
            "enabled": llm_enabled,
            "provider": llm_info["provider"],
            "port": llm_info["port"],
        },
        "tools_connected": connected_count,
        "tools_total": total,
        "tools_in_stub_mode": stub_count,
        "ready_for_production": gw_mode == "live",
        "tools": tools,
        "setup_instructions": (
            "MCP tools follow GATEWAY_MODE + AGENTCORE_GATEWAY_URL "
            "(see .env.example). Investigation LLM follows LLM_ENABLED + "
            "LLM_PROVIDER (null | bedrock | anthropic)."
        ),
    }
