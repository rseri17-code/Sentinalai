---
skill: timeout-investigation
description: >
  Investigate a timeout incident. Fetches incident metadata, searches for
  timeout-related logs, checks golden signals and latency metrics, then
  correlates with recent changes. Use for: 504, request timeout, deadline
  exceeded, upstream timeout incidents.
playbook: timeout
incident_types: [timeout]
max_calls: 5
---

# Timeout Investigation Skill

## When to Use

Activate when the incident summary contains: `timeout`, `timed out`,
`deadline`, `gateway timeout`, `504`, `upstream timeout`.

## Investigation Steps (ordered)

### Step 1 — Fetch Incident (`ops_worker.get_incident_by_id`)
Retrieve the canonical incident record. Required before all other steps.

### Step 2 — Search Timeout Logs (`log_worker.search_logs`)
Query: `timeout {service}`
Look for: upstream connection timeouts, read/write deadline exceeded messages,
HTTP 504 responses, gRPC deadline exceeded.

### Step 3 — Check Golden Signals (`apm_worker.get_golden_signals`)
Look for: latency spike correlating with incident timestamp, error rate
co-spike, saturation increase preceding the timeout window.

### Step 4 — Check Latency Metrics (`metrics_worker.query_metrics`)
Metric hint: `response_time_ms`
Look for: sustained p95/p99 elevation, sudden step change, gradual ramp
that crossed SLA threshold.

### Step 5 — Check Changes (`log_worker.get_change_data`)
Look for: deployments, config changes, or infra changes in the 2-hour
window preceding the timeout spike.

## Hypotheses to Score

| Hypothesis                 | Key evidence signal                              |
|----------------------------|--------------------------------------------------|
| upstream_service_slow      | Logs show downstream call timing out            |
| deployment_regression      | Change in window + latency spike after deploy   |
| resource_saturation        | CPU/thread exhaustion co-occurring               |
| database_slow_query        | DB call latency in logs exceeds threshold        |
| network_congestion         | Packet loss / network latency in signals         |

## Success Criteria

- Evidence gathered from ≥ 3 of the 5 steps
- At least one hypothesis score ≥ 40
- Root cause statement names the specific upstream component or change
