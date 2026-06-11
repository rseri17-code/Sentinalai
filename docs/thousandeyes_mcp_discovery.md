# ThousandEyes MCP Discovery

**Status:** Discovery complete — no production integration yet  
**Date:** 2026-06-10  
**Audience:** Principal SRE, Network Engineers, RCA Platform Team  

---

## 1. Setup and Install

### Official Source

ThousandEyes MCP is an official Cisco product, not available on PyPI or npm as a pre-built package. Deployment options:

**Option A — Docker (recommended for isolation)**
```bash
docker run -e TE_TOKEN=<token> \
           -e TE_AID=<account_group_id> \
           -p 8004:8004 \
           cisco/thousandeyes-mcp-server:latest
```

**Option B — Source (Python 3.12+)**
```bash
git clone https://github.com/cisco/thousandeyes-mcp-server
cd thousandeyes-mcp-server
pip install -r requirements.txt
# requirements: fastmcp>=3.1.0, requests>=2.31.0, uvicorn>=0.35.0, python-dotenv>=1.0.0
uvicorn server:app --host 0.0.0.0 --port 8004
```

**Option C — FastMCP (community reference)**
```bash
# Reference: github.com/pamosima/network-mcp-docker-suite
# thousandeyes-mcp-server/ subdirectory
```

**Required environment variables:**
```
TE_TOKEN=<bearer_token>         # required; ThousandEyes OAuth2 Bearer token
TE_AID=<account_group_id>       # required for multi-account; omit for default account
TE_BASE_URL=https://api.thousandeyes.com/v7   # optional override
TE_TIMEOUT=30                   # optional; default 30s
```

---

## 2. Server Entrypoint

- **Framework:** FastMCP (Python)
- **Entrypoint:** `server.py` → `app = FastMCP("thousandeyes")`
- **Runtime:** Uvicorn ASGI server
- **Default port:** 8004
- **Health check:** `GET http://localhost:8004/health`

---

## 3. Runtime Mode

| Transport | Details |
|-----------|---------|
| **Primary** | HTTP (Uvicorn) on port 8004 |
| **MCP protocol** | HTTP/SSE (Server-Sent Events for streaming responses) |
| **Alternate** | stdio mode supported for CLI/pipe usage |
| **Claude integration** | Configurable as HTTP MCP server in `.claude/settings.json` |

**MCP settings.json entry:**
```json
{
  "mcpServers": {
    "thousandeyes": {
      "type": "http",
      "url": "http://localhost:8004/mcp",
      "env": {
        "TE_TOKEN": "${TE_TOKEN}"
      }
    }
  }
}
```

---

## 4. Auth Model and Required Env Vars

| Variable | Required | Purpose |
|----------|----------|---------|
| `TE_TOKEN` | **Yes** | OAuth2 Bearer token from ThousandEyes account settings |
| `TE_AID` | Conditional | Account group ID; required when token has multi-account access |
| `TE_BASE_URL` | No | Override API base (default: `https://api.thousandeyes.com/v7`) |
| `TE_TIMEOUT` | No | Per-request timeout in seconds (default: 30) |

**Auth header:** `Authorization: Bearer {TE_TOKEN}`

**Token generation:**
1. ThousandEyes portal → Account Settings → Users and Roles → User API Tokens
2. OAuth2 tokens: Settings → OAuth Bearer Tokens (recommended for automation)

**Security note:** Token is read-only scoped if created with `View` permissions only. Always use minimal-permission tokens for RCA tooling.

---

## 5. Available MCP Tools

11 tools across 4 categories:

| # | Tool Name | Category | Read/Write |
|---|-----------|----------|-----------|
| 1 | `te_list_tests` | Test Management | Read |
| 2 | `te_get_test_results` | Test Management | Read |
| 3 | `te_get_path_vis` | Test Management | Read |
| 4 | `te_list_agents` | Agent Management | Read |
| 5 | `te_list_dashboards` | Dashboards | Read |
| 6 | `te_get_dashboard` | Dashboards | Read |
| 7 | `te_get_dashboard_widget` | Dashboards | Read |
| 8 | `te_list_alerts` | Monitoring | Read |
| 9 | `te_get_users` | Account | Read |
| 10 | `te_get_account_groups` | Account | Read |
| 11 | `te_get_test_details` | Test Management | Read |

**All 11 tools are read-only. No write or mutating operations.**

---

## 6. Resources and Prompts

- No MCP `resources` (file/URI resources) exposed
- No MCP `prompts` (templated prompt chains) exposed
- All capabilities surface through `tools` only

---

## 7. Tool Schemas (Full Detail)

---

### Tool 1: `te_list_tests`

**Purpose:** List all configured ThousandEyes tests. Tests represent scheduled network probes (HTTP, DNS, network TCP/ICMP, page-load, web-transaction, BGP).

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `test_type` | string | No | Filter by type: `network`, `http-server`, `page-load`, `dns-server`, `dns-trace`, `bgp`, `web-transactions`, `agent-to-agent`, `voice` |
| `aid` | string | No | Account group ID override |

**Sample safe request:**
```json
{
  "tool": "te_list_tests",
  "parameters": {
    "test_type": "http-server"
  }
}
```

**Sample sanitized response:**
```json
{
  "tests": [
    {
      "testId": 123456,
      "testName": "API Gateway - Production Health",
      "type": "http-server",
      "url": "https://api.example.com/health",
      "interval": 60,
      "enabled": true,
      "agents": [
        {"agentId": 10001, "agentName": "San Jose, CA"},
        {"agentId": 10002, "agentName": "New York, NY"},
        {"agentId": 10003, "agentName": "London, UK"}
      ],
      "createdDate": "2025-01-15T08:00:00Z",
      "modifiedDate": "2026-05-01T12:00:00Z"
    }
  ]
}
```

**RCA usefulness:** HIGH — lets SentinelAI discover which services are being actively monitored and which agents cover which regions. Entry point for targeted evidence collection.

**Risk level:** ZERO — pure metadata read.

---

### Tool 2: `te_get_test_results`

**Purpose:** Retrieve time-series test results for a specific test. The most important tool for RCA — provides latency, packet loss, availability, HTTP response codes, and timing breakdowns.

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `test_id` | integer | **Yes** | ThousandEyes test ID |
| `test_type` | string | **Yes** | Type: `network`, `http-server`, `page-load`, `dns-server`, `bgp` |
| `start_time` | string | No | ISO-8601 window start (default: 2h ago) |
| `end_time` | string | No | ISO-8601 window end (default: now) |
| `aid` | string | No | Account group override |

**Sample safe request:**
```json
{
  "tool": "te_get_test_results",
  "parameters": {
    "test_id": 123456,
    "test_type": "http-server",
    "start_time": "2026-06-10T09:00:00Z",
    "end_time": "2026-06-10T11:00:00Z"
  }
}
```

**Sample sanitized response:**
```json
{
  "results": [
    {
      "agentId": 10001,
      "agentName": "San Jose, CA",
      "roundId": 1717999200,
      "timestamp": "2026-06-10T10:00:00Z",
      "availability": 100.0,
      "responseTime": 245,
      "totalTime": 312,
      "dnsTime": 12,
      "connectTime": 48,
      "sslTime": 86,
      "waitTime": 95,
      "receiveTime": 4,
      "responseCode": 200,
      "numRedirects": 0,
      "errorType": null,
      "errorDetails": null
    },
    {
      "agentId": 10002,
      "agentName": "New York, NY",
      "roundId": 1717999200,
      "timestamp": "2026-06-10T10:00:00Z",
      "availability": 0.0,
      "responseTime": 0,
      "totalTime": 0,
      "dnsTime": 0,
      "connectTime": 0,
      "responseCode": 0,
      "errorType": "CONNECT_TIMEOUT",
      "errorDetails": "Connection timed out after 10000ms"
    }
  ]
}
```

**RCA usefulness:** CRITICAL — direct evidence of user-facing failure. Per-agent, per-round granularity enables geographic correlation. Timing breakdowns (DNS/connect/SSL/wait) isolate failure layer.

**Risk level:** ZERO — read-only time-series query.

---

### Tool 3: `te_get_path_vis`

**Purpose:** Network path visualization — hop-by-hop traceroute data including RTT, packet loss, and MPLS labels per hop. Reveals where in the internet path degradation occurs.

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `test_id` | integer | **Yes** | ThousandEyes test ID |
| `start_time` | string | No | ISO-8601 window start |
| `end_time` | string | No | ISO-8601 window end |
| `aid` | string | No | Account group override |

**Sample safe request:**
```json
{
  "tool": "te_get_path_vis",
  "parameters": {
    "test_id": 123456,
    "start_time": "2026-06-10T09:30:00Z",
    "end_time": "2026-06-10T10:30:00Z"
  }
}
```

**Sample sanitized response:**
```json
{
  "pathVis": [
    {
      "agentId": 10001,
      "agentName": "San Jose, CA",
      "timestamp": "2026-06-10T10:00:00Z",
      "routes": [
        {
          "hops": [
            {"hop": 1, "ipAddress": "10.x.x.1", "rdns": "gateway.corp", "rtt": [1.2, 1.1, 1.3]},
            {"hop": 2, "ipAddress": "72.x.x.x", "rdns": "border-router.isp.net", "rtt": [4.5, 4.6, 4.4]},
            {"hop": 3, "ipAddress": "x.x.x.x", "rdns": "peer.transit.net", "rtt": [12.1, 45.8, 78.2], "mpls": true},
            {"hop": 4, "ipAddress": "x.x.x.x", "rdns": "edge.cdn.example.com", "rtt": [999, 999, 999], "loss": 100}
          ],
          "finalHop": false
        }
      ]
    }
  ]
}
```

**RCA usefulness:** HIGH — identifies exactly which hop causes packet loss or latency. Enables ISP/carrier attribution. The `rdns` field often reveals provider identity without exposing sensitive internals.

**Risk level:** LOW — IPs are partially masked in sanitized output. Raw hop IPs may reveal internal topology; sanitize before storing.

---

### Tool 4: `te_list_agents`

**Purpose:** List all ThousandEyes agents (Enterprise, Enterprise Cluster, Cloud). Agents are the vantage points from which tests run — their locations map directly to user populations.

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_type` | string | No | Filter: `enterprise`, `enterprise-cluster`, `cloud` |
| `aid` | string | No | Account group override |

**Sample safe request:**
```json
{
  "tool": "te_list_agents",
  "parameters": {
    "agent_type": "cloud"
  }
}
```

**Sample sanitized response:**
```json
{
  "agents": [
    {
      "agentId": 10001,
      "agentName": "San Jose, CA",
      "agentType": "Cloud",
      "countryId": "US",
      "location": "San Jose, California",
      "network": "Comcast Cable Communications",
      "prefix": "73.x.x.0/22",
      "ipv6Policy": "FORCE_IPV4",
      "enabled": true
    },
    {
      "agentId": 20001,
      "agentName": "NYC-Office-Enterprise",
      "agentType": "Enterprise",
      "countryId": "US",
      "location": "New York, NY",
      "network": "Corporate Network",
      "prefix": "203.x.x.0/24",
      "enabled": true,
      "lastSeen": "2026-06-10T09:58:00Z",
      "status": "Online"
    }
  ]
}
```

**RCA usefulness:** MEDIUM — agent metadata is used to map test results to user populations (internal vs. external; specific regions; VPN users via enterprise agents). Essential for geographic blast radius analysis.

**Risk level:** LOW — internal enterprise agent IPs should be masked in output.

---

### Tool 5: `te_list_alerts`

**Purpose:** List ThousandEyes active or historical alerts. Alerts fire when test metrics cross configured thresholds (packet loss %, latency ms, availability %, etc.).

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `start_time` | string | No | ISO-8601 window start (default: 1h ago) |
| `end_time` | string | No | ISO-8601 window end (default: now) |
| `alert_rule_id` | integer | No | Filter to specific alert rule |
| `test_id` | integer | No | Filter to specific test |
| `aid` | string | No | Account group override |

**Sample safe request:**
```json
{
  "tool": "te_list_alerts",
  "parameters": {
    "start_time": "2026-06-10T09:00:00Z",
    "end_time": "2026-06-10T11:00:00Z"
  }
}
```

**Sample sanitized response:**
```json
{
  "alerts": [
    {
      "alertId": 987654,
      "testId": 123456,
      "testName": "API Gateway - Production Health",
      "type": "HTTP Server",
      "ruleId": 111,
      "ruleName": "Availability < 90%",
      "violationCount": 3,
      "dateStart": "2026-06-10T09:47:00Z",
      "dateEnd": null,
      "active": true,
      "agents": [
        {"agentId": 10002, "agentName": "New York, NY", "metricsAtStart": {"availability": 0}}
      ],
      "severity": "CRITICAL",
      "apiLinks": []
    }
  ]
}
```

**RCA usefulness:** HIGH — alert timestamps provide precise incident start time and affected regions. Alerts are the most direct ThousandEyes signal for correlation with app-layer incidents.

**Risk level:** ZERO — alert metadata only, no credentials or internal data.

---

### Tool 6: `te_list_dashboards`

**Purpose:** List available ThousandEyes dashboards. Dashboards aggregate multiple tests into operational views.

**RCA usefulness:** LOW for automated RCA; useful for human exploration.  
**Risk level:** ZERO.

---

### Tool 7: `te_get_dashboard`

**Purpose:** Get dashboard detail including widget configurations.

**RCA usefulness:** LOW for automated RCA.  
**Risk level:** ZERO.

---

### Tool 8: `te_get_dashboard_widget`

**Purpose:** Retrieve computed widget data (often aggregated metrics across tests/agents).

**RCA usefulness:** MEDIUM — summary widgets can provide quick signal on whether ThousandEyes sees degradation without querying every test individually.  
**Risk level:** ZERO.

---

### Tool 9: `te_get_users`

**Purpose:** List account users.

**RCA usefulness:** NONE for automated RCA.  
**Risk level:** LOW — may expose PII (email addresses). Not needed for RCA integration.

---

### Tool 10: `te_get_account_groups`

**Purpose:** List account groups (organizations/tenants in multi-account deployments).

**RCA usefulness:** LOW — needed for multi-tenant setup only.  
**Risk level:** ZERO.

---

### Tool 11: `te_get_test_details`

**Purpose:** Get full configuration and metadata for a specific test by ID.

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `test_id` | integer | **Yes** | ThousandEyes test ID |

**RCA usefulness:** MEDIUM — reveals test target URL, configured thresholds, agent set. Used during correlation to understand what a test is monitoring.

**Risk level:** ZERO.

---

## 8. Read vs. Write Classification

| Category | Tools | Classification |
|----------|-------|----------------|
| All 11 tools | `te_list_*`, `te_get_*` | **READ ONLY** |
| Potential write tools (NOT in MCP) | Test CRUD, alert rule management | Not exposed |
| Remediation (NOT in MCP) | Agent restart, test enable/disable | Not exposed |

**Conclusion:** The ThousandEyes MCP server is a **pure read interface**. No state mutations are possible through this MCP.

---

## 9. Pagination and Rate Limiting

**Pagination:**
- List endpoints support `limit` and `startPosition` parameters
- Default page size: 100 items
- Response includes `_links.next` when more pages exist
- ThousandEyes uses cursor-based pagination for large result sets

**Rate limits:**
- REST API: 240 requests/minute per token (v7)
- Burst allowed up to 60 requests/10 seconds
- Response headers: `X-Organization-Rate-Limit-Remaining`, `X-Organization-Rate-Limit-Reset`
- Recommended: implement exponential backoff on 429; cache aggressively (tests list changes infrequently)

**Recommended cache TTLs for SentinelAI:**
| Resource | TTL |
|----------|-----|
| Test list | 5 minutes |
| Agent list | 10 minutes |
| Test results | 60 seconds |
| Alerts | 30 seconds |
| Path visualization | 2 minutes |

---

## 10. Error Handling

| HTTP Status | Meaning | Handling |
|-------------|---------|---------|
| 200 | Success | Parse response |
| 204 | No content (empty result set) | Return empty list — not an error |
| 400 | Bad request (invalid params) | Log parameters, surface to caller |
| 401 | Invalid/expired token | Fail with auth error; do NOT retry |
| 403 | Insufficient permissions | Log and skip; not all tokens have dashboard access |
| 404 | Test/agent not found | Return None; may have been deleted |
| 429 | Rate limited | Retry after `Retry-After` header; use jitter |
| 500/503 | Server error | Retry with exponential backoff (3 attempts max) |
| Timeout | MCP server or API timeout | Return empty result, log warning; never block RCA |

---

## 11. Logging Behavior

- All ThousandEyes API requests should be logged at DEBUG level with: method, URL (no token), response code, latency
- 429s logged at WARN level
- 401/403 logged at ERROR level (credential issue)
- No token values ever written to logs
- Response bodies logged at DEBUG with PII redaction (no email fields)

---

## 12. Security Concerns

| Concern | Risk | Mitigation |
|---------|------|-----------|
| Bearer token exposure | HIGH | Store in environment variable only; never commit; never log |
| Internal IP exposure via path-vis | MEDIUM | Sanitize hop IPs before storing in evidence graph |
| Multi-account data leakage | MEDIUM | Scope token to minimum required account groups |
| Agent hostname exposure | LOW | Enterprise agent hostnames may reveal internal naming conventions |
| PII in user list | LOW | Do not call `te_get_users` in RCA automation |
| Rate limit exhaustion | LOW | Implement cache + rate limiter; shared token across investigations |
| MCP server not authenticated | MEDIUM | Deploy MCP server with network-level access control (not public) |

---

## Summary: RCA-Relevant Tools (Priority Order)

| Priority | Tool | RCA Use Case |
|----------|------|-------------|
| P0 | `te_list_alerts` | First call: is ThousandEyes firing during this incident? |
| P0 | `te_get_test_results` | Core evidence: availability, latency, error type per agent |
| P1 | `te_get_path_vis` | Deep-dive: which hop is failing? |
| P1 | `te_list_tests` | Bootstrap: discover monitored services |
| P2 | `te_list_agents` | Context: map agents to user populations |
| P3 | `te_get_test_details` | Detail: what thresholds, what target URL? |
| P4 | `te_get_dashboard_widget` | Broad scan when test ID unknown |
| Skip | `te_get_users` | PII, not useful for RCA |
