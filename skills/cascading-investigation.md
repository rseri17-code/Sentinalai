---
skill: cascading-investigation
description: >
  Investigate a cascading failure across multiple services. Identifies the
  origin service, the failure propagation path, and whether circuit breakers
  or retry storms amplified the incident.
playbook: cascading
incident_types: [cascading]
max_calls: 6
---

# Cascading Failure Investigation Skill

## When to Use

Activate when the incident summary contains: `cascading`, `cascade`,
`multiple services`, `circuit breaker`, `dependency failure`, `upstream`,
`downstream`, `chain`.

## Investigation Steps (ordered)

### Step 1 — Fetch Incident (`ops_worker.get_incident_by_id`)

### Step 2 — Search Error Logs (`log_worker.search_logs`)
Query: `error cascade {service}`
Look for: which service logged errors first, retry storm patterns,
circuit breaker open logs, timeout chains across service boundaries.

### Step 3 — Check Golden Signals (`apm_worker.get_golden_signals`)
Look for: which service shows saturation/error/latency degrading first,
fan-out error pattern (one service → many callers), timeline of degradation
spreading across the call graph.

### Step 4 — Check Metrics (`metrics_worker.query_metrics`)
Look for: retry rate metrics, queue depth growth, connection pool exhaustion
in calling services, thread pool saturation from blocked callers.

### Step 5 — Check Changes (`log_worker.get_change_data`)

### Step 6 — Check ITSM Changes (`itsm_worker.get_change_records`)

## Hypotheses to Score

| Hypothesis              | Key evidence signal                                      |
|-------------------------|----------------------------------------------------------|
| origin_service_failure  | One service errors first in timeline, rest follow       |
| retry_storm             | Retry rate surge, request rate > normal in logs         |
| shared_resource_exhaustion | DB/cache exhaustion shared across services           |
| thundering_herd         | Simultaneous reconnect attempts after brief outage      |
| circuit_breaker_misconfig | Circuit opens but does not recover, starving callers  |

## Success Criteria

- Origin service identified (the first to fail in the timeline)
- Propagation path documented (A → B → C)
- Whether retry/circuit breaker behavior amplified or contained the failure
