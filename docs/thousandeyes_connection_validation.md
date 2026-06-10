# ThousandEyes Connection Validation

**Phase:** Pre-integration connectivity check  
**Date:** 2026-06-10  
**Scope:** Read-only validation only — no state changes  

---

## Overview

Before integrating ThousandEyes into SentinelAI's RCA pipeline, validate:

1. MCP server starts and responds
2. Authentication succeeds
3. API returns expected data shapes
4. Error cases are handled correctly
5. Sample fixtures are captured for offline testing

All validation runs in `tools/thousandeyes_discovery/` — isolated from SentinelAI production code.

---

## Prerequisites

```bash
# Required environment variables (set before running any validation)
export TE_TOKEN="<your_token>"        # never hardcode
export TE_AID="<account_group_id>"   # optional for single-account

# Verify not empty
test -z "$TE_TOKEN" && echo "ERROR: TE_TOKEN not set" && exit 1
```

---

## Validation Checklist

### Step 1 — MCP Server Startup

```bash
# Start server (Docker recommended for isolation)
docker run -d --name te-mcp-validation \
           -e TE_TOKEN=$TE_TOKEN \
           -e TE_AID=$TE_AID \
           -p 8004:8004 \
           cisco/thousandeyes-mcp-server:latest

# Wait for startup
sleep 3

# Health check
curl -s http://localhost:8004/health
# Expected: {"status": "ok", "version": "x.y.z"}
```

**Pass criteria:** HTTP 200 with `{"status": "ok"}`  
**Fail criteria:** Connection refused, non-200, or `{"status": "error"}`

---

### Step 2 — Authentication Validation

```bash
# Valid token test (via MCP tool call)
curl -s -X POST http://localhost:8004/mcp \
  -H "Content-Type: application/json" \
  -d '{"method": "tools/call", "params": {"name": "te_list_agents", "arguments": {"agent_type": "cloud"}}}' \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print('AUTH_OK' if 'agents' in str(r) else 'AUTH_FAIL')"
```

**Pass criteria:** Response contains agent data  
**Fail criteria:** `{"error": {"code": 401, ...}}`

---

### Step 3 — Invalid Credentials Test

```bash
# Test with bad token
docker run --rm \
           -e TE_TOKEN="invalid-token-xyz" \
           -p 8005:8004 \
           cisco/thousandeyes-mcp-server:latest &

sleep 2
curl -s -X POST http://localhost:8005/mcp \
  -H "Content-Type: application/json" \
  -d '{"method": "tools/call", "params": {"name": "te_list_agents", "arguments": {}}}' \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print(r)"
# Expected: error with 401 code, NOT a crash or unhandled exception
```

---

### Step 4 — Read-Only Call Catalogue

Run each tool with minimal/safe parameters and capture response structure:

```bash
# List all tests
curl -s -X POST http://localhost:8004/mcp \
  -H "Content-Type: application/json" \
  -d '{"method": "tools/call", "params": {"name": "te_list_tests", "arguments": {}}}'

# List alerts (last 2 hours)
START=$(date -u -d '2 hours ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-2H +%Y-%m-%dT%H:%M:%SZ)
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
curl -s -X POST http://localhost:8004/mcp \
  -H "Content-Type: application/json" \
  -d "{\"method\": \"tools/call\", \"params\": {\"name\": \"te_list_alerts\", \"arguments\": {\"start_time\": \"$START\", \"end_time\": \"$END\"}}}"

# List agents
curl -s -X POST http://localhost:8004/mcp \
  -H "Content-Type: application/json" \
  -d '{"method": "tools/call", "params": {"name": "te_list_agents", "arguments": {}}}'
```

---

### Step 5 — Error Cases

| Scenario | How to Reproduce | Expected Behavior |
|----------|-----------------|-------------------|
| 401 Unauthorized | Use invalid `TE_TOKEN` | Error response with code 401; no crash |
| 403 Forbidden | Use token without dashboard access | Error response with code 403; graceful skip |
| 429 Rate Limited | Send >240 requests/minute | `Retry-After` header respected; error propagated |
| Timeout | Set `TE_TIMEOUT=1` | Timeout error surfaced, not hung |
| Empty results | Query future time window | `{"tests": []}` or `{"alerts": []}` — empty list, not error |
| Test not found | `te_get_test_results` with id=0 | 404 error, not crash |
| MCP server down | Kill docker container mid-call | Connection refused; SentinelAI falls back gracefully |

---

### Step 6 — Fixture Capture

After successful validation, capture sanitized samples:

```bash
# Sanitize helper — redact IPs and internal hostnames
sanitize() {
  python3 -c "
import sys, json, re
data = sys.stdin.read()
data = re.sub(r'\b(\d{1,3}\.){3}\d{1,3}\b', 'x.x.x.x', data)
data = re.sub(r'[a-zA-Z0-9.-]+\.internal', 'hostname.internal', data)
print(data)
"
}

# Capture each tool response as fixture
curl -s ... | sanitize > tools/thousandeyes_discovery/sample_outputs/list_tests.json
curl -s ... | sanitize > tools/thousandeyes_discovery/sample_outputs/list_alerts.json
curl -s ... | sanitize > tools/thousandeyes_discovery/sample_outputs/list_agents.json
curl -s ... | sanitize > tools/thousandeyes_discovery/sample_outputs/get_test_results_http.json
curl -s ... | sanitize > tools/thousandeyes_discovery/sample_outputs/get_path_vis.json
curl -s ... | sanitize > tools/thousandeyes_discovery/sample_outputs/get_test_results_dns.json
curl -s ... | sanitize > tools/thousandeyes_discovery/sample_outputs/error_401.json
curl -s ... | sanitize > tools/thousandeyes_discovery/sample_outputs/error_429.json
curl -s ... | sanitize > tools/thousandeyes_discovery/sample_outputs/empty_results.json
```

---

## Validation Report Template

```
# ThousandEyes Validation Report
Date: YYYY-MM-DD
Environment: <sandbox|staging|prod>
Token scope: <view-only|full>
Account groups tested: <count>

## Results

| Check | Status | Notes |
|-------|--------|-------|
| MCP server startup | PASS/FAIL | |
| Health endpoint | PASS/FAIL | |
| Authentication | PASS/FAIL | |
| te_list_tests | PASS/FAIL | n tests found |
| te_list_agents | PASS/FAIL | n agents found |
| te_list_alerts (2h window) | PASS/FAIL | n alerts found |
| te_get_test_results | PASS/FAIL | |
| te_get_path_vis | PASS/FAIL | |
| 401 handling | PASS/FAIL | |
| 429 handling | PASS/FAIL | |
| Empty result handling | PASS/FAIL | |
| Timeout handling | PASS/FAIL | |
| Fixture capture | PASS/FAIL | n files captured |

## Observations
<free text>

## Fixtures Captured
<list files>

## Recommendation
[ ] PROCEED with Phase 7 integration design
[ ] BLOCKED — issues found: <describe>
```

---

## Cleanup

```bash
docker stop te-mcp-validation && docker rm te-mcp-validation
# Unset env vars after validation session
unset TE_TOKEN
unset TE_AID
```
