"""Stub MCP Tool Server — serves fixture responses for all 8 integrations.

This server is what makes SentinalAI plug-and-play:
- Zero real tool credentials needed
- All workers get realistic fixture responses
- Replaces every integration in docker-compose stub mode
- Can be toggled per-tool: set SERVICENOW_MCP_URL to the real endpoint
  while leaving all others pointing to this stub

Routes:
  GET  /health                   → liveness
  POST /mcp/{tool}/{action}      → tool invocation (returns fixture data)
  GET  /mcp/catalog              → list all available stub tools+actions

Supported tools: servicenow, github, splunk, sysdig, dynatrace, moogsoft,
                 confluence, kubernetes
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("stub_mcp_server")

app = FastAPI(title="SentinalAI Stub MCP Server", version="1.0.0")

FIXTURES_DIR = Path(os.environ.get("FIXTURES_DIR", "tests/fixtures"))
PORT = int(os.environ.get("PORT", "9000"))

# ---------------------------------------------------------------------------
# Fixture loaders (lazy, cached)
# ---------------------------------------------------------------------------

_cache: dict[str, list] = {}


def _load_fixture(filename: str) -> list:
    if filename not in _cache:
        path = FIXTURES_DIR / filename
        if path.exists():
            with open(path) as f:
                raw = json.load(f)
            # Handle both list and dict-with-list structures
            if isinstance(raw, list):
                _cache[filename] = raw
            elif isinstance(raw, dict):
                # Try common keys
                for key in ("incidents", "records", "items", "data"):
                    if key in raw and isinstance(raw[key], list):
                        _cache[filename] = raw[key]
                        break
                else:
                    _cache[filename] = [raw]
        else:
            _cache[filename] = []
    return _cache[filename]


def _rand_item(items: list) -> dict:
    return random.choice(items) if items else {}


# ---------------------------------------------------------------------------
# Tool response generators
# ---------------------------------------------------------------------------

def _servicenow(action: str, params: dict) -> dict:
    service = params.get("service", "payment-service")
    incident_id = params.get("incident_id", "")

    if action == "get_ci_details":
        return {
            "ci": {
                "name": service,
                "sys_class_name": "cmdb_ci_service",
                "tier": 1,
                "owner": "platform-team",
                "dependencies": [f"{service}-db", f"{service}-cache"],
                "sla": "99.9%",
                "environment": "production",
            }
        }

    if action == "search_incidents":
        items = _load_fixture("servicenow_incidents_1000.json")
        matches = [i for i in items if service in i.get("cmdb_ci", "")][:5]
        return {"incidents": matches or items[:3]}

    if action == "get_change_records":
        return {
            "change_records": [
                {
                    "number": "CHG0099001",
                    "type": "normal",
                    "short_description": f"Deploy {service} v2.1.0 — connection pool resize",
                    "state": "closed",
                    "start_date": "2024-01-15T13:45:00",
                    "end_date": "2024-01-15T14:15:00",
                    "requested_by": "devops-automation",
                    "approval": "approved",
                    "risk": "medium",
                    "rollback_plan": f"kubectl rollout undo deployment/{service}",
                    "ci_impact": service,
                }
            ]
        }

    if action == "get_known_errors":
        return {
            "known_errors": [
                {
                    "number": "PRB0010001",
                    "short_description": f"Intermittent connection pool exhaustion on {service}",
                    "workaround": "Increase MAX_POOL_SIZE; add connection timeout",
                    "related_problem": "PRB0010001",
                }
            ]
        }

    if action == "update_incident":
        return {
            "updated": {
                "number": incident_id,
                "state": params.get("state", "resolved"),
                "sys_updated_on": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "resolved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        }

    return {}


def _github(action: str, params: dict) -> dict:
    service = params.get("service", "payment-service")
    repo = params.get("repo", f"myorg/{service}")

    if action == "get_recent_deployments":
        return {
            "deployments": [
                {
                    "sha": "abc123def456",
                    "ref": "main",
                    "description": f"Deploy {service} v2.1.0",
                    "author": "devops-bot",
                    "merged_at": "2024-01-15T13:50:00Z",
                    "pr_number": 847,
                    "environment": "production",
                    "repo": repo,
                }
            ]
        }

    if action == "get_pr_details":
        pr = params.get("pr_number", 847)
        return {
            "pr": {
                "number": pr,
                "title": f"fix: resize connection pool for {service}",
                "author": "john.doe",
                "merged_at": "2024-01-15T13:50:00Z",
                "files_changed": [
                    {
                        "filename": f"src/{service}/config/pool.py",
                        "additions": 3,
                        "deletions": 2,
                    }
                ],
                "ci_status": "success",
                "review_state": "approved",
                "labels": ["hotfix"],
            }
        }

    if action == "get_commit_diff":
        sha = params.get("sha", "abc123")
        return {
            "commit": {
                "sha": sha,
                "message": "Resize connection pool",
                "author": "john.doe",
                "date": "2024-01-15T13:48:00Z",
                "files": [
                    {
                        "filename": "src/config/database.py",
                        "patch": (
                            "@@ -42,7 +42,6 @@ class DatabaseConfig:\n"
                            "-    MAX_CONNECTIONS = 10\n"
                            "+    MAX_CONNECTIONS = 5  # reduced for cost savings\n"
                            "     TIMEOUT_MS = 30000\n"
                        ),
                        "additions": 1,
                        "deletions": 1,
                    }
                ],
                "stats": {"total": 2, "additions": 1, "deletions": 1},
            }
        }

    if action == "get_workflow_runs":
        return {
            "workflow_runs": [
                {
                    "id": 12345,
                    "name": "CI/CD Pipeline",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": "abc123def456",
                    "created_at": "2024-01-15T13:40:00Z",
                    "updated_at": "2024-01-15T13:50:00Z",
                }
            ]
        }

    if action == "create_fix_pr":
        return {
            "pr": {
                "number": 848,
                "html_url": f"https://github.com/{repo}/pull/848",
                "branch": "fix/auto-sentinalai-abc123",
                "sha": "fix456",
            }
        }

    if action in ("git_log", "git_log_for_service"):
        return {
            "commits": [
                {
                    "sha": "abc123def456789012345678901234567890abcd",
                    "message": "fix: resize connection pool — reduce MAX_CONNECTIONS",
                    "author": "john.doe",
                    "date": "2024-01-15T13:48:00Z",
                    "files_changed": ["src/config/database.py", "tests/test_db.py"],
                    "insertions": 3,
                    "deletions": 2,
                },
                {
                    "sha": "789abc123def456789012345678901234567890a",
                    "message": "chore: bump dependencies",
                    "author": "dependabot",
                    "date": "2024-01-14T09:00:00Z",
                    "files_changed": ["requirements.txt"],
                    "insertions": 5,
                    "deletions": 5,
                },
            ]
        }

    if action in ("git_blame", "git_blame_line"):
        return {
            "blame": [
                {
                    "line": params.get("line_start", 42),
                    "sha": "abc123def456789012345678901234567890abcd",
                    "author": "john.doe",
                    "date": "2024-01-15T13:48:00Z",
                    "message": "fix: resize connection pool",
                }
            ]
        }

    if action in ("git_show", "git_show_commit"):
        sha = params.get("sha", "abc123")
        return {
            "sha": sha,
            "message": "fix: resize connection pool — reduce MAX_CONNECTIONS",
            "author": "john.doe",
            "date": "2024-01-15T13:48:00Z",
            "patch": (
                "@@ -42,7 +42,6 @@ class DatabaseConfig:\n"
                "-    MAX_CONNECTIONS = 10\n"
                "+    MAX_CONNECTIONS = 5  # reduced for cost savings\n"
            ),
            "files_changed": ["src/config/database.py"],
            "insertions": 1,
            "deletions": 1,
            "parent_sha": "789abc123def456789012345678901234567890a",
        }

    if action in ("git_diff", "git_diff_range"):
        return {
            "diff": (
                "@@ -42,7 +42,6 @@ class DatabaseConfig:\n"
                "-    MAX_CONNECTIONS = 10\n"
                "+    MAX_CONNECTIONS = 5  # reduced for cost savings\n"
            ),
            "files_changed": ["src/config/database.py"],
            "insertions": 1,
            "deletions": 1,
        }

    if action == "get_pr_for_commit":
        return {
            "pr_number": 847,
            "pr_title": f"fix: resize connection pool for {service}",
            "merged_at": "2024-01-15T13:50:00Z",
        }

    return {}


def _splunk(action: str, params: dict) -> dict:
    service = params.get("service", "payment-service")

    if action in ("search_logs", "search_oneshot"):
        return {
            "logs": [
                {
                    "_time": "2024-01-15T14:02:11.123Z",
                    "host": f"{service}-pod-abc",
                    "source": f"kubernetes/{service}",
                    "sourcetype": "kube:container:app",
                    "index": "production",
                    "_raw": f"2024-01-15T14:02:11 ERROR pool.exhausted service={service} connections=1024/1024 waiting=47",
                    "level": "ERROR",
                    "service": service,
                    "message": "Connection pool exhausted: connections=1024/1024, waiting=47",
                },
                {
                    "_time": "2024-01-15T14:02:12.456Z",
                    "host": f"{service}-pod-abc",
                    "source": f"kubernetes/{service}",
                    "sourcetype": "kube:container:app",
                    "index": "production",
                    "_raw": f"2024-01-15T14:02:12 ERROR timeout.acquiring.connection timeout=30000ms service={service}",
                    "level": "ERROR",
                    "service": service,
                    "message": "Timeout acquiring database connection after 30000ms",
                },
            ]
        }

    if action == "get_change_data":
        return {
            "change_data": [
                {
                    "change_id": "CHG0099001",
                    "service": service,
                    "description": f"Deploy {service} v2.1.0",
                    "timestamp": "2024-01-15T13:50:00Z",
                    "deployed_by": "devops-automation",
                }
            ]
        }

    return {}


def _sysdig(action: str, params: dict) -> dict:
    service = params.get("service", "payment-service")

    if action in ("get_service_metrics", "get_golden_signals"):
        return {
            "metrics": {
                "service": service,
                "window_minutes": params.get("window_minutes", 5),
                "error_rate": 0.087,
                "latency_p50_ms": 142.0,
                "latency_p95": 843.0,
                "p95_ms": 843.0,
                "latency_p99_ms": 2100.0,
                "request_rate": 847.3,
                "rps": 847.3,
                "saturation_pct": 94.2,
            }
        }

    if action == "get_kubernetes_events":
        return {
            "events": [
                {
                    "name": "OOMKilled",
                    "namespace": "production",
                    "pod": f"{service}-pod-abc",
                    "timestamp": "2024-01-15T14:00:00Z",
                    "reason": "OOMKilled",
                    "message": f"Container {service} killed due to memory pressure",
                }
            ]
        }

    if action == "get_metric_chart":
        metric = params.get("metric", "net.http.request.time")
        return {
            "chart": {
                "title": f"{service} {metric} — last 2h",
                "source": "sysdig",
                "metric": metric,
                "url": (
                    f"https://app.sysdig.com/charts/{service}/"
                    f"{metric.replace('.', '-')}?from=1705315200&to=1705322400"
                ),
                "image_b64": None,
                "time_range": {
                    "from": params.get("from_iso", ""),
                    "to": params.get("to_iso", ""),
                },
                "annotation": "spike at 14:02 UTC correlates with deploy v2.1.0",
            }
        }

    if action == "get_dashboard_snapshot":
        return {
            "dashboard": {
                "url": f"https://app.sysdig.com/dashboards/{service}",
                "image_b64": None,
                "source": "sysdig",
                "charts": [
                    {"metric": "net.http.request.time", "title": "Request Latency"},
                    {"metric": "net.http.error.count", "title": "Error Rate"},
                    {"metric": "container.memory.used.percent", "title": "Memory Usage"},
                ],
            }
        }

    return {}


def _dynatrace(action: str, params: dict) -> dict:
    service = params.get("service", "payment-service")

    if action == "get_problems":
        return {
            "problems": [
                {
                    "problemId": "P-001",
                    "displayName": f"Response time degradation on {service}",
                    "severityLevel": "PERFORMANCE",
                    "status": "OPEN",
                    "startTime": 1705322400000,
                    "affectedEntities": [{"name": service}],
                }
            ]
        }

    if action == "get_error_samples":
        return {
            "errors": [
                {
                    "message": "java.sql.SQLException: Timeout waiting for connection from pool",
                    "stack_trace": (
                        f"at com.example.{service}.db.ConnectionPool.acquire(Pool.java:42)\n"
                        f"at com.example.{service}.PaymentService.process(PaymentService.java:87)"
                    ),
                    "timestamp": "2024-01-15T14:02:11Z",
                    "count": 247,
                    "trace_id": "abc123def456789012345678901234ab",
                }
            ]
        }

    if action in ("get_topology", "get_topology_map"):
        return {
            "topology": {
                "nodes": [
                    {"service": service, "type": "service", "error_rate": 0.087},
                    {"service": f"{service}-db", "type": "database", "error_rate": 0.0},
                    {"service": "auth-service", "type": "service", "error_rate": 0.12},
                ],
                "edges": [
                    {"from": "client", "to": service, "calls_per_min": 847, "error_rate": 0.087},
                    {"from": service, "to": f"{service}-db", "calls_per_min": 320, "error_rate": 0.032},
                    {"from": service, "to": "auth-service", "calls_per_min": 847, "error_rate": 0.12},
                ],
                "image_url": f"https://dynatrace.example.com/topology/{service}",
            }
        }

    if action == "get_trace":
        return {
            "spans": [
                {
                    "span_id": "span001",
                    "parent_span_id": None,
                    "service_name": "client",
                    "operation_name": "checkout",
                    "duration_ms": 5200,
                    "start_time": "2024-01-15T14:02:08.000Z",
                    "error": "",
                },
                {
                    "span_id": "span002",
                    "parent_span_id": "span001",
                    "service_name": service,
                    "operation_name": "process_payment",
                    "duration_ms": 5180,
                    "start_time": "2024-01-15T14:02:08.020Z",
                    "error": "",
                },
                {
                    "span_id": "span003",
                    "parent_span_id": "span002",
                    "service_name": "auth-service",
                    "operation_name": "session_lookup",
                    "duration_ms": 4820,
                    "start_time": "2024-01-15T14:02:08.040Z",
                    "error": "connection_timeout",
                },
                {
                    "span_id": "span004",
                    "parent_span_id": "span003",
                    "service_name": "session-db",
                    "operation_name": "redis_get",
                    "duration_ms": 4810,
                    "start_time": "2024-01-15T14:02:08.060Z",
                    "error": "",
                },
            ]
        }

    return {}


def _moogsoft(action: str, params: dict) -> dict:
    if action == "get_incident":
        incident_id = params.get("incident_id", "MOOG001")
        items = _load_fixture("moogsoft_incidents_1000.json")
        match = next((i for i in items if str(i.get("incident_id")) == str(incident_id)), None)
        return match or (_rand_item(items) if items else {"incident_id": incident_id, "summary": "Service degradation"})

    if action == "search_incidents":
        items = _load_fixture("moogsoft_incidents_1000.json")
        return {"incidents": items[:5]}

    return {}


def _confluence(action: str, params: dict) -> dict:
    service = params.get("service", "payment-service")

    if action == "search_runbooks":
        return {
            "runbooks": [
                {
                    "title": f"{service} Incident Response Runbook",
                    "url": f"https://confluence.example.com/runbooks/{service}",
                    "space": "OPS",
                    "excerpt": f"Steps to diagnose and resolve incidents on {service}: 1. Check error rate in Sysdig...",
                    "last_updated": "2024-01-10T12:00:00Z",
                }
            ]
        }

    if action == "search_postmortems":
        return {
            "postmortems": [
                {
                    "title": f"Post-Mortem: {service} Connection Pool Exhaustion (2023-11-15)",
                    "url": "https://confluence.example.com/postmortems/2023-11-15",
                    "root_cause": "MAX_CONNECTIONS reduced in PR #721 without load testing",
                    "resolution": "Reverted MAX_CONNECTIONS to 50. Added load test to CI pipeline.",
                    "date": "2023-11-15",
                }
            ]
        }

    return {}


def _kubernetes(action: str, params: dict) -> dict:
    service = params.get("service", "payment-service")
    namespace = params.get("namespace", "production")

    if action == "rollback_deployment":
        return {
            "rollback": {
                "status": "success",
                "deployment": service,
                "namespace": namespace,
                "previous_revision": "42",
                "message": f"Rolled back {service} to revision 42",
            }
        }

    if action == "scale_service":
        replicas = params.get("replicas", 2)
        return {
            "scale": {
                "status": "success",
                "deployment": service,
                "namespace": namespace,
                "replicas": replicas,
            }
        }

    return {}


# Tool dispatch map
_TOOL_HANDLERS = {
    "servicenow": _servicenow,
    "github": _github,
    "splunk": _splunk,
    "sysdig": _sysdig,
    "dynatrace": _dynatrace,
    "moogsoft": _moogsoft,
    "confluence": _confluence,
    "kubernetes": _kubernetes,
}


# ---------------------------------------------------------------------------
# FastAPI routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "mode": "stub", "tools": list(_TOOL_HANDLERS.keys())}


@app.get("/tools")
async def tools_discovery():
    """Standard tool discovery endpoint consumed by McpGateway.discover_tools().

    Returns available server names in the canonical format so the supervisor
    can dynamically decide which workers to instantiate at startup.
    """
    return {
        "available_servers": list(_TOOL_HANDLERS.keys()),
    }


@app.get("/mcp/catalog")
async def catalog():
    """List all stub tool actions available."""
    return {
        "stub_tools": {
            "servicenow": ["get_ci_details", "search_incidents", "get_change_records",
                           "get_known_errors", "update_incident"],
            "github":     ["get_recent_deployments", "get_pr_details", "get_commit_diff",
                           "get_workflow_runs", "create_fix_pr",
                           "git_log", "git_blame", "git_show", "git_diff", "get_pr_for_commit"],
            "splunk":     ["search_logs", "search_oneshot", "get_change_data"],
            "sysdig":     ["get_service_metrics", "get_golden_signals", "get_kubernetes_events",
                           "get_metric_chart", "get_dashboard_snapshot"],
            "dynatrace":  ["get_problems", "get_error_samples", "get_topology", "get_trace"],
            "moogsoft":   ["get_incident", "search_incidents"],
            "confluence": ["search_runbooks", "search_postmortems"],
            "kubernetes": ["rollback_deployment", "scale_service"],
        }
    }


@app.post("/mcp/{tool}/{action}")
async def invoke_tool(tool: str, action: str, request: Request):
    """Invoke a stub tool action and return fixture-based response."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    handler = _TOOL_HANDLERS.get(tool)
    if not handler:
        return JSONResponse({"error": f"Unknown tool: {tool}"}, status_code=404)

    logger.info("STUB %s.%s params=%s", tool, action, body)
    result = handler(action, body)
    return JSONResponse(result)


# Also support the AgentCore triple-underscore tool naming format
@app.post("/invoke")
async def invoke_agentcore_format(request: Request):
    """Handle AgentCore-style tool invocations: toolName = 'ServiceNowTarget___get_ci_details'"""
    body = await request.json()
    tool_name = body.get("toolName", "")
    input_params = body.get("toolInput", body.get("input", {}))

    # Parse {Target}___{action} format
    if "___" in tool_name:
        target, action = tool_name.split("___", 1)
        # Map target names to our tool names
        tool_map = {
            "ServiceNowTarget": "servicenow",
            "GitHubTarget": "github",
            "SplunkTarget": "splunk",
            "SysdigTarget": "sysdig",
            "DynatraceTarget": "dynatrace",
            "MoogsoftTarget": "moogsoft",
            "ConfluenceTarget": "confluence",
            "KubernetesTarget": "kubernetes",
        }
        tool = tool_map.get(target, target.lower().replace("target", ""))
    else:
        # Try dotted format: servicenow.get_ci_details
        parts = tool_name.split(".", 1)
        tool = parts[0] if len(parts) > 1 else tool_name
        action = parts[1] if len(parts) > 1 else "unknown"

    handler = _TOOL_HANDLERS.get(tool)
    if not handler:
        return JSONResponse({"error": f"Unknown tool: {tool_name}"}, status_code=404)

    result = handler(action, input_params)
    return JSONResponse({"content": [{"type": "text", "text": json.dumps(result)}]})


if __name__ == "__main__":
    logger.info("Starting stub MCP server on port %d", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
