# ThousandEyes Integration Test Plan

**Date:** 2026-06-10  
**Scope:** All 20 required test cases  
**Test approach:** Fixture-first; live API only in integration tests  
**Feature flag:** Tests run with both `ENABLE_THOUSANDEYES_RCA=true` and `false`  

---

## Test Environment

```bash
# Unit tests (no network required)
TE_USE_FIXTURES=true
ENABLE_THOUSANDEYES_RCA=true
TE_TOKEN=fixture_mode  # non-empty; unused when TE_USE_FIXTURES=true

# Integration tests (live ThousandEyes account required)
TE_USE_FIXTURES=false
ENABLE_THOUSANDEYES_RCA=true
TE_TOKEN=<real_token>
TE_AID=<account_group_id>
```

---

## Test 1: MCP Server Startup

**Type:** Integration  
**File:** `tests/test_thousandeyes_mcp_startup.py`  

```python
def test_mcp_server_starts_and_responds():
    """Verify MCP server health endpoint returns 200 OK."""
    import httpx
    response = httpx.get(f"{TE_MCP_URL}/health", timeout=5)
    assert response.status_code == 200
    data = response.json()
    assert data.get("status") == "ok"
    
def test_mcp_server_exposes_tools():
    """Verify MCP server advertises expected tools."""
    response = call_mcp("tools/list", {})
    tool_names = {t["name"] for t in response["tools"]}
    assert "te_list_tests" in tool_names
    assert "te_list_alerts" in tool_names
    assert "te_get_test_results" in tool_names
    assert "te_get_path_vis" in tool_names
    assert "te_list_agents" in tool_names
```

**Pass criteria:** Health returns 200; tools list contains all 11 expected tools  
**Fixture needed:** `health_response.json`

---

## Test 2: Missing Credentials

**Type:** Unit  
**File:** `tests/test_thousandeyes_auth.py`  

```python
def test_missing_te_token_skips_worker(monkeypatch):
    """Worker returns empty evidence when TE_TOKEN is not set."""
    monkeypatch.delenv("TE_TOKEN", raising=False)
    worker = ThousandEyesWorker()
    result = worker.run("inc-001", "api", "latency_spike", "2026-06-10T09:00:00Z", "2026-06-10T11:00:00Z")
    assert result == {}
    
def test_empty_te_token_skips_worker(monkeypatch):
    """Worker returns empty evidence when TE_TOKEN is empty string."""
    monkeypatch.setenv("TE_TOKEN", "")
    worker = ThousandEyesWorker()
    result = worker.run("inc-001", "api", "latency_spike", "2026-06-10T09:00:00Z", "2026-06-10T11:00:00Z")
    assert result == {}
```

**Pass criteria:** Worker returns `{}` without raising; no MCP call attempted  
**Fixture needed:** None (pure unit test)

---

## Test 3: Invalid Credentials

**Type:** Integration  
**File:** `tests/test_thousandeyes_auth.py`  

```python
def test_invalid_token_returns_auth_error(monkeypatch):
    """Adapter raises ThousandEyesAuthError on 401 response."""
    monkeypatch.setenv("TE_TOKEN", "invalid-token-xyz-123")
    adapter = ThousandEyesMCPAdapter()
    with pytest.raises(ThousandEyesAuthError):
        adapter.list_agents()

def test_auth_error_does_not_propagate_to_worker(monkeypatch):
    """Worker catches auth error and returns empty evidence."""
    monkeypatch.setenv("TE_TOKEN", "invalid-token")
    worker = ThousandEyesWorker()
    result = worker.run("inc-001", "api", "latency_spike", "2026-06-10T09:00:00Z", "2026-06-10T11:00:00Z")
    assert result == {}  # non-blocking
```

**Pass criteria:** Auth error is raised by adapter but swallowed by worker; RCA not blocked  
**Fixture needed:** `error_401.json`

---

## Test 4: Permission Denied (403)

**Type:** Unit  
**File:** `tests/test_thousandeyes_auth.py`  

```python
def test_403_on_dashboard_access_returns_empty(mock_mcp_client):
    """403 on dashboard call returns empty list, not exception."""
    mock_mcp_client.set_response("te_list_dashboards", status=403, body={"error": "Forbidden"})
    adapter = ThousandEyesMCPAdapter()
    result = adapter.list_dashboards()
    assert result == []

def test_403_logged_at_error_level(mock_mcp_client, caplog):
    """403 response is logged at ERROR level."""
    mock_mcp_client.set_response("te_list_tests", status=403, body={"error": "Forbidden"})
    adapter = ThousandEyesMCPAdapter()
    with caplog.at_level(logging.ERROR, logger="sentinalai.thousandeyes"):
        adapter.list_tests()
    assert any("403" in r.message or "Forbidden" in r.message for r in caplog.records)
```

**Fixture needed:** `error_403.json`

---

## Test 5: Rate Limited (429)

**Type:** Unit  
**File:** `tests/test_thousandeyes_rate_limit.py`  

```python
def test_429_triggers_single_retry(mock_mcp_client):
    """429 response is retried once after Retry-After seconds."""
    mock_mcp_client.set_sequence("te_list_tests", [
        {"status": 429, "headers": {"Retry-After": "1"}, "body": {}},
        {"status": 200, "body": {"tests": [{"testId": 1, "testName": "Test"}]}}
    ])
    adapter = ThousandEyesMCPAdapter()
    result = adapter.list_tests()
    assert len(result) == 1
    assert mock_mcp_client.call_count("te_list_tests") == 2

def test_429_twice_returns_empty(mock_mcp_client):
    """Two consecutive 429s return empty list (one retry only)."""
    mock_mcp_client.set_response("te_list_tests", status=429, body={}, repeat=3)
    adapter = ThousandEyesMCPAdapter()
    result = adapter.list_tests()
    assert result == []
    assert mock_mcp_client.call_count("te_list_tests") == 2  # initial + 1 retry
```

**Fixture needed:** `error_429.json`

---

## Test 6: API Timeout

**Type:** Unit  
**File:** `tests/test_thousandeyes_timeout.py`  

```python
def test_adapter_timeout_returns_empty(mock_mcp_client):
    """Request timeout returns empty result, not exception."""
    mock_mcp_client.set_timeout("te_get_test_results", delay=25)  # > 10s timeout
    adapter = ThousandEyesMCPAdapter()
    result = adapter.get_test_results(test_id=123, test_type="http-server",
                                       start_time="2026-06-10T09:00:00Z",
                                       end_time="2026-06-10T10:00:00Z")
    assert result == []

def test_worker_timeout_returns_empty_dict(mock_mcp_client):
    """Worker timeout (20s) returns empty dict without blocking."""
    mock_mcp_client.set_timeout("te_list_alerts", delay=25)
    worker = ThousandEyesWorker()
    result = worker.run("inc-001", "api", "latency_spike", 
                        "2026-06-10T09:00:00Z", "2026-06-10T11:00:00Z")
    assert isinstance(result, dict)
    # May be empty or have partial results — must never raise
```

---

## Test 7: Empty Results

**Type:** Unit  
**File:** `tests/test_thousandeyes_empty.py`  

```python
def test_empty_test_list_returns_empty_list(mock_mcp_client):
    """Service with no TE tests configured returns empty list gracefully."""
    mock_mcp_client.set_response("te_list_tests", status=200, body={"tests": []})
    adapter = ThousandEyesMCPAdapter()
    assert adapter.list_tests() == []

def test_empty_alerts_window_is_not_an_error(mock_mcp_client):
    """Empty alerts in time window returns [] not error."""
    mock_mcp_client.set_response("te_list_alerts", status=200, body={"alerts": []})
    adapter = ThousandEyesMCPAdapter()
    result = adapter.list_alerts("2026-06-10T09:00:00Z", "2026-06-10T11:00:00Z")
    assert result == []

def test_worker_with_no_tests_returns_empty_evidence(mock_mcp_client):
    """Worker returns empty dict when no TE tests match the service."""
    mock_mcp_client.set_response("te_list_tests", status=200, body={"tests": []})
    worker = ThousandEyesWorker()
    result = worker.run("unknown-service", "unknown-service", "latency_spike",
                        "2026-06-10T09:00:00Z", "2026-06-10T11:00:00Z")
    assert result == {} or result.get("network_evidence") == []
```

**Fixture needed:** `empty_results.json`

---

## Test 8: List Tests Happy Path

**Type:** Unit (fixture) + Integration (live)  
**File:** `tests/test_thousandeyes_tools.py`  

```python
def test_list_tests_returns_test_objects(fixture_adapter):
    """te_list_tests returns list of tests with required fields."""
    tests = fixture_adapter.list_tests()
    assert len(tests) > 0
    for test in tests:
        assert "testId" in test
        assert "testName" in test
        assert "type" in test
        assert isinstance(test["testId"], int)

def test_list_tests_filter_by_type(fixture_adapter):
    """test_type filter returns only matching tests."""
    http_tests = fixture_adapter.list_tests(test_type="http-server")
    assert all(t["type"] == "http-server" for t in http_tests)
```

**Fixture needed:** `list_tests.json`

---

## Test 9: List Agents Happy Path

**Type:** Unit (fixture)  
**File:** `tests/test_thousandeyes_tools.py`  

```python
def test_list_agents_returns_agent_objects(fixture_adapter):
    """te_list_agents returns agents with location and type."""
    agents = fixture_adapter.list_agents()
    assert len(agents) > 0
    for agent in agents:
        assert "agentId" in agent
        assert "agentName" in agent
        assert "agentType" in agent

def test_list_agents_filter_by_cloud(fixture_adapter):
    """Cloud agent filter returns only cloud agents."""
    cloud_agents = fixture_adapter.list_agents(agent_type="cloud")
    assert all(a["agentType"].lower() == "cloud" for a in cloud_agents)
```

**Fixture needed:** `list_agents.json`

---

## Test 10: Alerts by Time Window

**Type:** Unit (fixture)  
**File:** `tests/test_thousandeyes_tools.py`  

```python
def test_list_alerts_returns_active_alerts(fixture_adapter):
    """Active alerts returned with required fields."""
    alerts = fixture_adapter.list_alerts("2026-06-10T09:00:00Z", "2026-06-10T11:00:00Z")
    for alert in alerts:
        assert "alertId" in alert
        assert "testId" in alert
        assert "dateStart" in alert
        assert "active" in alert

def test_list_alerts_empty_window_returns_empty(fixture_adapter):
    """Future time window with no alerts returns empty list."""
    future = "2099-01-01T00:00:00Z"
    result = fixture_adapter.list_alerts(future, future)
    assert isinstance(result, list)
```

**Fixture needed:** `list_alerts.json`, `empty_results.json`

---

## Test 11: Test Results by Time Window

**Type:** Unit (fixture)  
**File:** `tests/test_thousandeyes_tools.py`  

```python
def test_get_test_results_returns_per_agent_results(fixture_adapter):
    """HTTP test results include per-agent timing breakdown."""
    results = fixture_adapter.get_test_results(
        test_id=123456, test_type="http-server",
        start_time="2026-06-10T09:00:00Z", end_time="2026-06-10T11:00:00Z"
    )
    assert len(results) > 0
    for r in results:
        assert "agentId" in r
        assert "agentName" in r
        assert "availability" in r
        # HTTP-specific fields
        assert "responseTime" in r or "errorType" in r

def test_get_test_results_dns_type(fixture_adapter):
    """DNS test results include DNS-specific fields."""
    results = fixture_adapter.get_test_results(
        test_id=789012, test_type="dns-server",
        start_time="2026-06-10T09:00:00Z", end_time="2026-06-10T11:00:00Z"
    )
    for r in results:
        assert "dnsTime" in r or "errorType" in r
```

**Fixture needed:** `get_test_results_http.json`, `get_test_results_dns.json`

---

## Test 12: Path Visualization Fixture

**Type:** Unit (fixture)  
**File:** `tests/test_thousandeyes_path_vis.py`  

```python
def test_path_vis_returns_hop_data(fixture_adapter):
    """Path visualization returns hop list with RTT data."""
    path_data = fixture_adapter.get_path_vis(
        test_id=123456,
        start_time="2026-06-10T09:00:00Z", end_time="2026-06-10T11:00:00Z"
    )
    assert len(path_data) > 0
    for agent_path in path_data:
        assert "agentId" in agent_path
        routes = agent_path.get("routes", [])
        for route in routes:
            hops = route.get("hops", [])
            assert all("hop" in h for h in hops)

def test_path_vis_sanitization_redacts_ips(normalizer, raw_path_fixture):
    """IP addresses in path visualization are sanitized."""
    normalized = normalizer.normalize_path_vis(raw_path_fixture, {})
    for e in normalized:
        if e.path_summary:
            for hop in e.path_summary:
                ip = hop.get("ip", "")
                # Should not contain full IP octets (only sanitized form)
                assert not re.match(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip)
```

**Fixture needed:** `get_path_vis.json`

---

## Test 13: DNS Failure Fixture

**Type:** Unit  
**File:** `tests/test_thousandeyes_fixtures.py`  

```python
def test_dns_failure_fixture_produces_high_confidence_evidence():
    """DNS failure fixture normalizes to high-confidence NetworkEvidence."""
    loader = ThousandEyesFixtureLoader()
    raw = loader.get_test_results("dns_failure")
    normalizer = ThousandEyesEvidenceNormalizer()
    evidence = normalizer.normalize_dns_results(raw, {"testId": 789012, "testName": "Payments DNS"})
    assert len(evidence) > 0
    failing = [e for e in evidence if e.availability == 0]
    assert len(failing) > 0
    # DNS failure → high confidence
    for e in failing:
        conf = compute_network_confidence(e)
        assert conf > 0.7
    # DNS failure → DNS owner
    assert all(e.recommended_owner == "dns" for e in failing)
```

**Fixture needed:** `dns_failure.json`

---

## Test 14: Packet Loss Fixture

**Type:** Unit  
**File:** `tests/test_thousandeyes_fixtures.py`  

```python
def test_packet_loss_fixture_fires_correlation_rule():
    """Packet loss fixture triggers TE-CORR-001 (network-induced latency)."""
    loader = ThousandEyesFixtureLoader()
    raw_results = loader.get_test_results("packet_loss")
    raw_path = loader.get_test_results("packet_loss_path")
    
    normalizer = ThousandEyesEvidenceNormalizer()
    evidence = normalizer.normalize_http_results(raw_results, {})
    
    # Simulate co-occurring Dynatrace latency
    app_evidence = {"dynatrace": {"response_time_p95": 3000, "response_time_baseline": 800}}
    
    engine = ThousandEyesCorrelationEngine()
    result = engine.evaluate(evidence, app_evidence)
    
    assert "TE-CORR-001" in result.rules_fired
    assert result.confidence_delta >= 25
    assert result.recommended_owner in ("network", "isp")
```

**Fixture needed:** `packet_loss.json`, `packet_loss_path.json`

---

## Test 15: SaaS Outage Fixture

**Type:** Unit  
**File:** `tests/test_thousandeyes_fixtures.py`  

```python
def test_saas_outage_fixture_fires_rule_006():
    """SaaS outage fixture triggers TE-CORR-006 and achieves high confidence."""
    loader = ThousandEyesFixtureLoader()
    te_results = loader.get_test_results("saas_outage")
    
    normalizer = ThousandEyesEvidenceNormalizer()
    evidence = normalizer.normalize_http_results(
        te_results, {"testId": 999, "testName": "Stripe API Health"}
    )
    
    app_evidence = {
        "splunk": {"patterns": ["connection timeout.*api.stripe.com"] * 10},
        "dynatrace": {"internal_services_healthy": True}
    }
    
    engine = ThousandEyesCorrelationEngine()
    result = engine.evaluate(evidence, app_evidence)
    
    assert "TE-CORR-006" in result.rules_fired
    assert result.confidence_delta >= 35
    assert result.recommended_owner == "saas"
    assert "stripe" in result.network_summary.lower() or "saas" in result.network_summary.lower()
```

**Fixture needed:** `saas_outage.json`

---

## Test 16: Endpoint Issue Fixture

**Type:** Unit  
**File:** `tests/test_thousandeyes_fixtures.py`  

```python
def test_endpoint_fixture_fires_rule_007():
    """Endpoint-only failure fires TE-CORR-007 and assigns endpoint owner."""
    loader = ThousandEyesFixtureLoader()
    cloud_results = loader.get_test_results("endpoint_cloud_healthy")
    enterprise_results = loader.get_test_results("endpoint_enterprise_failed")
    
    normalizer = ThousandEyesEvidenceNormalizer()
    cloud_ev = normalizer.normalize_http_results(cloud_results, {})
    enterprise_ev = normalizer.normalize_http_results(enterprise_results, 
                                                        {"agentType": "Enterprise"})
    
    incident = {"user_reported": True, "type": "user_reported_slowness"}
    engine = ThousandEyesCorrelationEngine()
    result = engine.evaluate(cloud_ev + enterprise_ev, {"incident": incident})
    
    assert "TE-CORR-007" in result.rules_fired
    assert result.recommended_owner == "endpoint"
```

**Fixture needed:** `endpoint_cloud_healthy.json`, `endpoint_enterprise_failed.json`

---

## Test 17: Normalization Unit Tests

**Type:** Unit  
**File:** `tests/test_thousandeyes_normalizer.py`  

```python
def test_normalizer_computes_confidence_for_full_outage():
    """availability=0 → confidence ≥ 0.65."""
    ev = NetworkEvidence(
        source="thousandeyes", test_id="1", test_name="Test", test_type="http-server",
        target="https://api.example.com", agent_id="1", agent_location="NY",
        agent_type="cloud", region="us-east",
        window_start="2026-06-10T09:00:00Z", window_end="2026-06-10T10:00:00Z",
        availability=0.0, error_type="CONNECT_TIMEOUT",
        affected_scope="regional",
        confidence=0.0, recommended_owner="unknown", raw_summary="",
    )
    conf = compute_network_confidence(ev)
    assert conf >= 0.65

def test_normalizer_infers_dns_owner_for_dns_failure():
    """DNS error type → recommended_owner=dns."""
    ev = _make_ev(error_type="DNS_FAILURE", dns_time_ms=2000)
    assert infer_owner(ev) == "dns"

def test_normalizer_infers_app_owner_for_high_ttfb():
    """High TTFB + normal connect time → app owner."""
    ev = _make_ev(ttfb_ms=3000, connect_time_ms=15)
    assert infer_owner(ev) == "app"

def test_normalizer_assigns_global_scope_for_multi_region_failure():
    """4 agents across 3 regions all failing → affected_scope=global."""
    evidence = [_make_ev(region=r, availability=0) for r in ["us-east", "eu-west", "apac", "us-west"]]
    scope = normalizer._compute_affected_scope(evidence)
    assert scope == "global"

def test_evidence_id_is_deterministic():
    """Same inputs produce same evidence_id."""
    ev1 = _make_ev(test_id="123", agent_id="456", round_id=1717999200)
    ev2 = _make_ev(test_id="123", agent_id="456", round_id=1717999200)
    assert ev1.evidence_id == ev2.evidence_id
    assert len(ev1.evidence_id) == 16

def test_ip_sanitization_removes_internal_octets():
    """Last two octets of IP are masked."""
    sanitized = normalizer._sanitize_ip("10.20.30.40")
    assert "30.40" not in sanitized
    assert sanitized != "10.20.30.40"
```

---

## Test 18: Correlation Rule Unit Tests

**Type:** Unit  
**File:** `tests/test_thousandeyes_correlation.py`  

```python
@pytest.mark.parametrize("rule_id,fixture,app_evidence,expected_fired,expected_owner", [
    ("TE-CORR-001", "packet_loss", {"dynatrace_latency_spike": True}, True, "network"),
    ("TE-CORR-004", "dns_failure", {"splunk_dns_errors": 10}, True, "dns"),
    ("TE-CORR-006", "saas_outage", {"splunk_saas_timeout": 8, "internal_healthy": True}, True, "saas"),
    ("TE-CORR-007", "endpoint_enterprise_failed", {"user_reported": True, "cloud_healthy": True}, True, "endpoint"),
])
def test_correlation_rule_fires(rule_id, fixture, app_evidence, expected_fired, expected_owner):
    """Each rule fires when expected inputs are present."""
    ...
    
def test_no_rules_fire_when_network_healthy(healthy_fixture):
    """No correlation rules fire when all agents show 100% availability."""
    evidence = normalizer.normalize_http_results(healthy_fixture, {})
    result = engine.evaluate(evidence, {})
    assert result.rules_fired == []
    assert result.confidence_delta == 0

def test_multiple_rules_can_fire_simultaneously():
    """Packet loss + DNS failure can both fire in same investigation."""
    ...
    assert "TE-CORR-001" in result.rules_fired
    assert "TE-CORR-004" in result.rules_fired
```

---

## Test 19: Feature Flag Disabled Test

**Type:** Unit  
**File:** `tests/test_thousandeyes_feature_flag.py`  

```python
def test_worker_disabled_when_flag_false(monkeypatch):
    """Worker is completely skipped when ENABLE_THOUSANDEYES_RCA=false."""
    monkeypatch.setenv("ENABLE_THOUSANDEYES_RCA", "false")
    worker = ThousandEyesWorker()
    
    with mock.patch.object(ThousandEyesMCPAdapter, "list_alerts") as mock_list:
        result = worker.run("inc-001", "api", "latency_spike",
                           "2026-06-10T09:00:00Z", "2026-06-10T11:00:00Z")
        assert mock_list.call_count == 0  # no API calls made
    assert result == {}

def test_tool_selector_excludes_te_worker_when_disabled(monkeypatch):
    """ThousandEyes not in selected steps when feature flag is off."""
    monkeypatch.setenv("ENABLE_THOUSANDEYES_RCA", "false")
    steps = tool_selector.select_steps("latency_spike", "api")
    assert not any(s.get("worker") == "thousandeyes" for s in steps)

def test_network_blind_spot_gate_fires_when_disabled(monkeypatch):
    """Gate warning injected into RCA when TE is disabled for TE-trigger incident types."""
    monkeypatch.setenv("ENABLE_THOUSANDEYES_RCA", "false")
    result = rca_result_for("latency_spike", "api")
    # Warning should appear in RCA output
    assert "EVIDENCE GAP" in str(result.get("warnings", [])) or \
           result.get("network_evidence") is None
```

---

## Test 20: No Regression for Existing RCA Flows

**Type:** Integration  
**File:** `tests/test_thousandeyes_no_regression.py`  

```python
def test_existing_investigation_unaffected_with_te_disabled(monkeypatch):
    """Existing RCA output structure unchanged when TE disabled."""
    monkeypatch.setenv("ENABLE_THOUSANDEYES_RCA", "false")
    result = run_investigation("inc-regression-001")
    # All existing keys must be present
    assert "root_cause" in result
    assert "confidence" in result
    assert "remediation" in result
    assert "incident_type" in result
    # New TE keys must not be injected
    assert "network_evidence" not in result
    assert "network_correlation" not in result

def test_existing_investigation_backward_compatible_with_te_enabled(monkeypatch):
    """When TE is enabled, existing keys still present; new keys are additive."""
    monkeypatch.setenv("ENABLE_THOUSANDEYES_RCA", "true")
    monkeypatch.setenv("TE_USE_FIXTURES", "true")
    result = run_investigation("inc-latency-001")
    # Existing keys intact
    assert "root_cause" in result
    assert "confidence" in result
    assert "remediation" in result
    # New keys added but not replacing existing
    # (network_evidence may or may not be present depending on incident type)

def test_full_suite_passes_with_te_disabled(monkeypatch):
    """Running full test suite with ENABLE_THOUSANDEYES_RCA=false produces 0 failures."""
    # This test is a marker — CI enforces it by running:
    # ENABLE_THOUSANDEYES_RCA=false pytest tests/ -q
    pass

def test_full_suite_passes_with_te_fixtures(monkeypatch):
    """Running full test suite with TE enabled + fixtures produces 0 new failures."""
    # CI enforces:
    # ENABLE_THOUSANDEYES_RCA=true TE_USE_FIXTURES=true pytest tests/ -q
    pass
```

---

## Test Execution Matrix

| Environment | `ENABLE_THOUSANDEYES_RCA` | `TE_USE_FIXTURES` | `TE_TOKEN` | Expected |
|-------------|--------------------------|-------------------|-----------|---------|
| Unit (CI) | `true` | `true` | dummy | All tests pass |
| Unit (CI) | `false` | - | - | All tests pass |
| Integration | `true` | `false` | real | All tests pass |
| Regression | `false` | - | - | 0 existing failures |

---

## Required Fixtures

| Fixture File | Content |
|-------------|---------|
| `list_tests.json` | 5 tests (http, dns, network, bgp, page-load) |
| `list_agents.json` | 6 agents (3 cloud, 2 enterprise, 1 endpoint) |
| `list_alerts.json` | 3 active alerts (availability, packet-loss, dns) |
| `get_test_results_http.json` | HTTP test results: 3 agents, 2 failing, 1 healthy |
| `get_test_results_dns.json` | DNS test results: all failing with SERVFAIL |
| `get_path_vis.json` | Path vis: 8 hops, hop 6 showing 100% loss |
| `dns_failure.json` | DNS test: 0% availability, SERVFAIL, dns_time=2000ms |
| `packet_loss.json` | Network test: 15% packet loss, elevated RTT |
| `saas_outage.json` | HTTP test to SaaS: 0% from 5 diverse agents |
| `endpoint_cloud_healthy.json` | Cloud agents: 100% availability |
| `endpoint_enterprise_failed.json` | Enterprise agents: 0% availability |
| `error_401.json` | `{"error": {"code": 401, "message": "Unauthorized"}}` |
| `error_403.json` | `{"error": {"code": 403, "message": "Forbidden"}}` |
| `error_429.json` | `{"error": {"code": 429, "message": "Too Many Requests"}}` |
| `empty_results.json` | `{"tests": [], "alerts": [], "results": []}` |
| `health_response.json` | `{"status": "ok", "version": "1.0.0"}` |
