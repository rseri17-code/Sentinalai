---
skill: error-spike-investigation
description: >
  Investigate a sudden increase in error rate. Searches error logs, checks
  golden signals, correlates with deployments and ITSM changes. Use for:
  500 errors, 5xx spike, exception rate increase, panic, crash incidents.
playbook: error_spike
incident_types: [error_spike]
max_calls: 6
---

# Error Spike Investigation Skill

## When to Use

Activate when the incident summary contains: `error spike`, `error rate`,
`exception`, `500`, `5xx`, `502`, `503`, `internal server error`,
`exception rate`, `unhandled exception`, `panic`, `crash`.

## Investigation Steps (ordered)

### Step 1 — Fetch Incident (`ops_worker.get_incident_by_id`)

### Step 2 — Search Error Logs (`log_worker.search_logs`)
Query: `error {service}`
Look for: stack traces, exception messages, panic/crash output, specific
HTTP status codes, NullPointerException, connection refused patterns.

### Step 3 — Check Golden Signals (`apm_worker.get_golden_signals`)
Look for: error rate spike shape (sudden vs gradual), latency co-increase,
saturation changes, correlated services degrading simultaneously.

### Step 4 — Check Changes (`log_worker.get_change_data`)
Correlate error spike onset with deployment timestamp. A deployment
in the 30-minute window before the spike is high-confidence evidence.

### Step 5 — Check ITSM Changes (`itsm_worker.get_change_records`)
Look for: approved change records, emergency changes, config modifications
from CMDB that may explain the error pattern.

### Step 6 — Check Events (`metrics_worker.get_events`)
Look for: infrastructure events (node failure, pod restart, config map update)
that coincide with the error spike onset.

## Hypotheses to Score

| Hypothesis                  | Key evidence signal                              |
|-----------------------------|--------------------------------------------------|
| deployment_regression       | Deploy in window + error spike immediately after |
| dependency_failure          | Downstream service errors in logs                |
| config_change               | ITSM change record + config-related errors       |
| infrastructure_event        | Node/pod event correlating with spike            |
| data_corruption             | Validation errors, deserialization failures      |
| third_party_api_failure     | External API error codes in logs                 |

## Success Criteria

- Error log contains specific exception type and stack trace
- Onset time identified from metrics
- Root cause names either: the specific exception, the deployment, or
  the dependency that failed
