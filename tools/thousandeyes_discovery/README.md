# ThousandEyes Discovery Tools

This directory contains discovery scripts and sanitized fixture data for the SentinelAI + ThousandEyes integration.

**Purpose:** Validate ThousandEyes MCP connectivity and capture sanitized sample outputs before building production integration code.

**Safety constraints:**
- All scripts are read-only. No write operations.
- All IP addresses and ASNs in fixtures are sanitized (replaced with RFC 5737/documentation ranges).
- Never commit real `TE_TOKEN` values. Use env var only.
- `TE_USE_FIXTURES=true` bypasses all live API calls for offline testing.

---

## Directory Structure

```
tools/thousandeyes_discovery/
├── README.md                    ← this file
└── sample_outputs/              ← sanitized fixture JSON files
    ├── list_tests.json          ← te_list_tests response
    ├── list_agents.json         ← te_list_agents response
    ├── list_alerts.json         ← te_list_alerts response (with active alerts)
    ├── get_test_results_http.json   ← te_get_test_results (HTTP server test)
    ├── get_test_results_dns.json    ← te_get_test_results (DNS test)
    ├── get_path_vis.json            ← te_get_path_vis response
    ├── dns_failure.json             ← DNS resolution failure scenario
    ├── packet_loss.json             ← Packet loss >20% scenario
    ├── saas_outage.json             ← SaaS provider outage scenario
    ├── endpoint_cloud_healthy.json  ← Endpoint degraded, cloud agents healthy
    ├── endpoint_enterprise_failed.json  ← Enterprise agent failure scenario
    ├── error_401.json           ← 401 Unauthorized response
    ├── error_403.json           ← 403 Forbidden response
    ├── error_429.json           ← 429 Rate limit exceeded response
    ├── empty_results.json       ← Empty results (no tests/alerts configured)
    └── health_response.json     ← MCP server health check response
```

---

## Fixture Files Reference

Fixtures are used by `ThousandEyesFixtureLoader` when `TE_USE_FIXTURES=true`.

The loader maps MCP tool names to fixture files:

| MCP Tool | Fixture File |
|----------|-------------|
| `te_list_tests` | `list_tests.json` |
| `te_list_agents` | `list_agents.json` |
| `te_list_alerts` | `list_alerts.json` |
| `te_get_test_results` (http-server) | `get_test_results_http.json` |
| `te_get_test_results` (dns) | `get_test_results_dns.json` |
| `te_get_path_vis` | `get_path_vis.json` |

Scenario fixtures (selected by incident type / error condition):

| Scenario | Fixture File |
|----------|-------------|
| DNS failure | `dns_failure.json` |
| Packet loss >20% | `packet_loss.json` |
| SaaS outage | `saas_outage.json` |
| Endpoint cloud healthy | `endpoint_cloud_healthy.json` |
| Enterprise agent failed | `endpoint_enterprise_failed.json` |
| Error — 401 Unauthorized | `error_401.json` |
| Error — 403 Forbidden | `error_403.json` |
| Error — 429 Rate limited | `error_429.json` |
| Empty results | `empty_results.json` |
| Health check | `health_response.json` |

---

## Sanitization Notes

Real fixture data captured from a live ThousandEyes account must be sanitized before committing:

1. **IP addresses** → Replace with RFC 5737 documentation range (`192.0.2.x`, `198.51.100.x`, `203.0.113.x`)
2. **ASNs** → Replace with AS64496–AS64511 (documentation range per RFC 5398)
3. **Account IDs / org IDs** → Replace with `aid-REDACTED` / `org-REDACTED`
4. **Agent IDs** → Replace with sequential integers starting at `10001`
5. **Test IDs** → Replace with sequential integers starting at `123456`
6. **API tokens** → Must never appear in fixture data
7. **Alert rule IDs** → Replace with sequential integers starting at `987654`
8. **Domain names** → Replace with `example.com` / `api.example.com` variants
9. **Provider names** → May be kept if publicly known ISPs (Comcast, AT&T, etc.) — check with security team
10. **User emails/names** → Remove entirely; `te_get_users` data must not be captured

---

## Using Fixtures in Tests

```python
import os
import pytest

@pytest.fixture(autouse=True)
def use_fixtures(monkeypatch):
    monkeypatch.setenv("TE_USE_FIXTURES", "true")
    monkeypatch.setenv("ENABLE_THOUSANDEYES_RCA", "true")
```

See `tests/test_thousandeyes_*.py` for full test suite.

---

## Capturing New Fixtures (Live Account Required)

To capture fresh fixture data from a live ThousandEyes account:

```bash
# 1. Set credentials (never commit these)
export TE_TOKEN="your-token-here"
export TE_MCP_URL="http://localhost:8004"

# 2. Start the ThousandEyes MCP server
docker run -p 8004:8004 -e TE_TOKEN=$TE_TOKEN thousandeyes/mcp-server:latest

# 3. Capture fixtures (substitute your account's test/agent IDs)
curl -s -X POST $TE_MCP_URL/mcp \
  -H "Content-Type: application/json" \
  -d '{"tool": "te_list_tests"}' | python3 -m json.tool > raw_list_tests.json

# 4. Sanitize before committing
python3 tools/thousandeyes_discovery/sanitize_fixtures.py raw_list_tests.json \
  > sample_outputs/list_tests.json

# 5. Verify no real IPs/tokens in output
grep -E '([0-9]{1,3}\.){3}[0-9]{1,3}' sample_outputs/list_tests.json
# Should only show 192.0.2.x, 198.51.100.x, or 203.0.113.x ranges

# 6. Unset credentials
unset TE_TOKEN
```
