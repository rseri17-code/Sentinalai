# ThousandEyes + SentinelAI Integration Architecture

**Date:** 2026-06-10  
**Status:** Design only — not implemented  
**Feature flag:** `ENABLE_THOUSANDEYES_RCA=false` (disabled by default)  

---

## Design Constraints

1. Behind feature flag — default off
2. Read-only from ThousandEyes
3. Fixture-first testing (no live calls required to run tests)
4. Non-blocking — ThousandEyes failures never block RCA
5. No changes to existing evidence contract (backward compatible addition only)
6. No write operations to ThousandEyes
7. No autonomous remediation based on network evidence
8. Secrets only in environment variables, never in code or logs

---

## Component Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                     SentinelAI Supervisor                           │
│                                                                     │
│  incident_id → supervisor.investigate()                             │
│       │                                                             │
│       ▼                                                             │
│  tool_selector.py                                                   │
│  [if ENABLE_THOUSANDEYES_RCA and incident_type in TE_TRIGGERS]      │
│       │                                                             │
│       ▼                                                             │
│  workers/network_worker.py  ◄─────────────────────────────────┐    │
│  [ThousandEyesWorker]                                          │    │
│       │                                                        │    │
│       ▼                                                        │    │
│  ThousandEyesMCPAdapter  ───► MCP Client ───► TE MCP Server   │    │
│       │                         (port 8004)    (ThousandEyes)  │    │
│       ▼                                                        │    │
│  ThousandEyesEvidenceNormalizer                                │    │
│       │                                                        │    │
│       ▼                                                        │    │
│  NetworkEvidence (list)                                        │    │
│       │                                                        │    │
│       ▼                                                        │    │
│  ThousandEyesCorrelationEngine                                 │    │
│       │                                                        │    │
│  ┌────▼──────────────────────────────────────────────────┐    │    │
│  │  evidence["network_evidence"] = [...]                  │    │    │
│  │  evidence["network_correlation"] = {...}               │    │    │
│  └───────────────────────────────────────────────────────┘    │    │
│       │                                                        │    │
│       ▼                                                        │    │
│  supervisor.analyze() [existing flow — unchanged]              │    │
│       │                                                        │    │
│       ▼                                                        │    │
│  _post_flight_learning()                                       │    │
│  intel_writer.capture() → incident_graph (network nodes)  ────┘    │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘

ThousandEyesFixtureLoader
  └─ used in tests when ENABLE_THOUSANDEYES_RCA=true but TE server unavailable
     (env var: TE_USE_FIXTURES=true)
```

---

## Components

### 1. `workers/network_worker.py` — ThousandEyesWorker

The SentinelAI worker that orchestrates ThousandEyes evidence collection. Follows the same interface as `apm_worker.py` and `log_worker.py`.

```python
class ThousandEyesWorker(BaseWorker):
    """
    Collects network evidence from ThousandEyes for a given service/incident.
    
    Called by tool_selector.py for network-relevant incident types.
    Non-blocking: returns {} on any failure.
    Feature-gated: ENABLE_THOUSANDEYES_RCA=false skips entirely.
    """
    
    WORKER_NAME = "thousandeyes"
    TIMEOUT_SECONDS = 20        # shorter than default 30s — network evidence is supplemental
    CACHE_TTL_SECONDS = 60      # test results cache; alerts cache is 30s
    
    def run(self, incident_id: str, service: str, incident_type: str,
            window_start: str, window_end: str) -> dict[str, Any]:
        ...
```

**Trigger incident types:**
```python
TE_TRIGGER_INCIDENT_TYPES = {
    "external_timeout", "latency_spike", "regional_outage",
    "api_unavailable", "dns_failure", "intermittent_connectivity",
    "high_latency", "cdn_degradation", "saas_dependency_failure",
    "vpn_performance", "page_load_slow", "ssl_error",
    "user_reported_slowness", "network_degradation",
}
```

---

### 2. `integrations/thousandeyes/adapter.py` — ThousandEyesMCPAdapter

Thin wrapper over the MCP client for ThousandEyes. Handles:
- Tool-to-MCP-call translation
- Per-call timeouts
- 429 retry with jitter
- Sanitizing responses before returning

```python
class ThousandEyesMCPAdapter:
    MCP_SERVER_URL = os.getenv("TE_MCP_URL", "http://localhost:8004/mcp")
    
    def list_alerts(self, start_time: str, end_time: str) -> list[dict]: ...
    def list_tests(self, test_type: str | None = None) -> list[dict]: ...
    def get_test_results(self, test_id: int, test_type: str,
                         start_time: str, end_time: str) -> list[dict]: ...
    def get_path_vis(self, test_id: int, start_time: str, end_time: str) -> list[dict]: ...
    def list_agents(self) -> list[dict]: ...
    def find_tests_for_service(self, service: str) -> list[dict]: ...
```

---

### 3. `integrations/thousandeyes/normalizer.py` — ThousandEyesEvidenceNormalizer

Converts raw ThousandEyes API responses into `NetworkEvidence` instances. Applies sanitization (IP redaction, hostname normalization). Computes confidence scores deterministically.

```python
class ThousandEyesEvidenceNormalizer:
    def normalize_http_results(self, raw: list[dict], test: dict) -> list[NetworkEvidence]: ...
    def normalize_dns_results(self, raw: list[dict], test: dict) -> list[NetworkEvidence]: ...
    def normalize_path_vis(self, raw: list[dict], test: dict) -> list[NetworkEvidence]: ...
    def normalize_alerts(self, raw: list[dict]) -> list[NetworkEvidence]: ...
    def _sanitize_ip(self, ip: str) -> str: ...  # hash last 2 octets
    def _infer_region(self, agent_location: str) -> str: ...
    def _compute_affected_scope(self, evidence: list[NetworkEvidence]) -> str: ...
```

---

### 4. `integrations/thousandeyes/correlation.py` — ThousandEyesCorrelationEngine

Evaluates the 10 deterministic correlation rules (TE-CORR-001 through TE-CORR-010) against the combined evidence set. Returns a `CorrelationResult`.

```python
@dataclass
class CorrelationResult:
    rules_fired: list[str]            # list of rule IDs that matched
    confidence_delta: int             # sum of confidence impacts
    recommended_owner: str            # primary owner from highest-confidence rule
    rca_findings: list[str]           # one string per fired rule (RCA output wording)
    network_summary: str              # one-sentence summary for evidence dict

class ThousandEyesCorrelationEngine:
    def evaluate(
        self,
        te_evidence: list[NetworkEvidence],
        app_evidence: dict[str, Any],  # existing evidence snapshot
    ) -> CorrelationResult: ...
```

---

### 5. `integrations/thousandeyes/fixture_loader.py` — ThousandEyesFixtureLoader

Loads sanitized JSON fixtures from `tools/thousandeyes_discovery/sample_outputs/` for tests and for use when `TE_USE_FIXTURES=true`.

```python
class ThousandEyesFixtureLoader:
    FIXTURE_DIR = Path("tools/thousandeyes_discovery/sample_outputs")
    
    def load(self, fixture_name: str) -> dict | list: ...
    def get_test_results(self, scenario: str) -> list[dict]: ...
    # scenario: "http_timeout" | "dns_failure" | "packet_loss" | "saas_outage" | "endpoint_issue"
```

---

### 6. `integrations/thousandeyes/enricher.py` — ThousandEyesRCAEnricher

Top-level coordinator. Called from `network_worker.py`. Orchestrates adapter → normalizer → correlation → evidence dict population.

```python
class ThousandEyesRCAEnricher:
    def enrich(
        self,
        incident_id: str,
        service: str,
        incident_type: str,
        window_start: str,
        window_end: str,
        existing_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Return updated evidence dict with network_evidence and network_correlation keys."""
        ...
```

---

## Data Flow

```
1. supervisor.investigate(incident_id) called
      │
2. tool_selector selects workers including ThousandEyesWorker
   (only if ENABLE_THOUSANDEYES_RCA=true AND incident_type in TE_TRIGGER_INCIDENT_TYPES)
      │
3. ThousandEyesWorker.run() called (timeout: 20s)
      │
4. ThousandEyesRCAEnricher.enrich() orchestrates:
   a. ThousandEyesMCPAdapter.list_alerts(window)
   b. ThousandEyesMCPAdapter.find_tests_for_service(service)
   c. ThousandEyesMCPAdapter.get_test_results(test_id, type, window)  [parallel]
   d. ThousandEyesMCPAdapter.get_path_vis(test_id, window)            [conditional]
      │
5. ThousandEyesEvidenceNormalizer.normalize_*() → [NetworkEvidence]
      │
6. ThousandEyesCorrelationEngine.evaluate(te_evidence, existing_evidence)
   → CorrelationResult {rules_fired, confidence_delta, recommended_owner, rca_findings}
      │
7. evidence dict updated:
   evidence["network_evidence"] = [e.to_dict() for e in te_evidence]
   evidence["network_correlation"] = correlation_result.to_dict()
   evidence["network_summary"] = correlation_result.network_summary
      │
8. supervisor.analyze() runs with enriched evidence (unchanged analyze() code)
      │
9. result["confidence"] += min(correlation_result.confidence_delta, 40)  [capped]
   result["network_owner"] = correlation_result.recommended_owner
   result["network_findings"] = correlation_result.rca_findings
      │
10. _post_flight_learning() → intel_writer.capture() includes network evidence
    → incident_graph: new "network" node type added for TE tests
    → dependency_graph: updated with network-observed service dependencies
```

---

## Error Handling

| Failure | Behavior |
|---------|---------|
| MCP server unreachable | Log WARN; return empty evidence; RCA continues |
| TE_TOKEN missing | Log ERROR once at startup; skip worker entirely |
| 401 / 403 from ThousandEyes | Log ERROR; skip this call; never block |
| 429 rate limited | Retry once after `Retry-After` seconds; if still 429, skip |
| Test results empty | Normal — not all services have TE tests; log DEBUG |
| Worker timeout (20s) | Return partial results collected so far; log WARN |
| Normalizer exception | Log DEBUG; return empty list; never surface to LLM |
| Correlation rule exception | Skip that rule; other rules still evaluated |

---

## Timeout Strategy

```
TE_MCP_URL call timeout:    10s per call
ThousandEyesWorker timeout: 20s total (all calls combined)
MCP server startup timeout: 5s health check
Rate limit retry wait:      Retry-After header value (max 30s)
```

---

## Retry Strategy

```
Connection error:   3 retries with exponential backoff (2s, 4s, 8s)
429 rate limited:   1 retry after Retry-After (or 10s if header missing)
500/503:            2 retries with 5s, 10s backoff
401/403/404:        0 retries (auth/permissions issue; retrying won't help)
Timeout:            0 retries (time budget exhausted)
```

---

## Cache Strategy

```python
# Cache keys and TTLs
TE_CACHE = {
    "tests_list":       (5 * 60,  lambda: adapter.list_tests()),      # 5 min
    "agents_list":      (10 * 60, lambda: adapter.list_agents()),     # 10 min
    "test_results":     (60,      lambda: adapter.get_test_results()), # 60s
    "alerts":           (30,      lambda: adapter.list_alerts()),      # 30s
    "path_vis":         (2 * 60,  lambda: adapter.get_path_vis()),     # 2 min
}
# Uses existing RetrievalCache from supervisor/retrieval/retrieval_cache.py
```

---

## Secret Handling

```
TE_TOKEN:    env var only; never logged; never in code; never in Git
TE_AID:      env var; not secret but still env-only
TE_MCP_URL:  env var (default: http://localhost:8004)

In logs:     Only log request method, URL path (no token), response code
In errors:   Sanitize error messages — no token in exception strings
In fixtures: All sample outputs have tokens removed; IPs hashed
```

---

## Feature Flag

```bash
# Environment variable (default: false)
ENABLE_THOUSANDEYES_RCA=false      # master switch
TE_TOKEN=                          # required when enabled
TE_MCP_URL=http://localhost:8004   # MCP server URL
TE_AID=                            # optional account group ID
TE_TIMEOUT=20                      # worker timeout seconds
TE_USE_FIXTURES=false              # use fixture files instead of live calls (for testing)
```

**Rollback plan:**
```bash
# Immediate rollback: set env var and redeploy/restart
ENABLE_THOUSANDEYES_RCA=false
# No database changes to roll back
# No API contract changes to roll back
# Existing RCA output unaffected (network_evidence key simply absent)
```

---

## Backward Compatibility

The integration adds these new keys to the evidence dict if ThousandEyes is enabled:
```
evidence["network_evidence"]    = list of NetworkEvidence dicts
evidence["network_correlation"] = CorrelationResult dict  
evidence["network_summary"]     = str
```

And these new keys to the result dict:
```
result["network_owner"]    = str | None
result["network_findings"] = list[str]
```

**All existing keys are unchanged.** Consumers that don't know about these new keys continue to work. The supervisor's analyze() function reads these keys if present but makes no assumptions about their existence.

---

## Integration Point in tool_selector.py

```python
# In supervisor/tool_selector.py (addition only — no existing logic changed)
if (os.getenv("ENABLE_THOUSANDEYES_RCA", "false").lower() == "true" and
        incident_type in TE_TRIGGER_INCIDENT_TYPES):
    selected_steps.append({
        "worker": "thousandeyes",
        "priority": "supplemental",
        "timeout": int(os.getenv("TE_TIMEOUT", "20")),
    })
```

This is the only change needed to `tool_selector.py`. The worker runs as a supplemental step after core evidence is collected — it cannot block or replace primary evidence collection.
