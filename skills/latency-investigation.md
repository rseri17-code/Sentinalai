---
skill: latency-investigation
description: >
  Investigate elevated latency or SLA breach. Combines APM golden signals,
  latency metrics, and log correlation to pinpoint the slow component.
  Use for: high latency, slow response, p99 breach, SLA breach incidents.
playbook: latency
incident_types: [latency]
max_calls: 5
---

# Latency Investigation Skill

## When to Use

Activate when the incident summary contains: `latency`, `slow`, `response time`,
`p95`, `p99`, `sla breach`, `degraded performance`, `high latency`.

## Investigation Steps (ordered)

### Step 1 — Fetch Incident (`ops_worker.get_incident_by_id`)

### Step 2 — Search Latency Logs (`log_worker.search_logs`)
Query: `latency OR slow {service}`
Look for: slow query logs, lock wait times, downstream call latency logged
at request level, GC pause times, connection pool wait times.

### Step 3 — Check Golden Signals (`apm_worker.get_golden_signals`)
Look for: which golden signal degraded first (latency vs error vs saturation),
whether it's gradual ramp or sudden step, which service tier shows the symptom.

### Step 4 — Check Latency Metrics (`metrics_worker.query_metrics`)
Metric hint: `response_time_ms`
Look for: p50/p95/p99 divergence (p99 spike without p50 movement → tail
latency from outlier calls), sustained elevation vs spike pattern.

### Step 5 — Check Changes (`log_worker.get_change_data`)
Look for: database schema migration, index removal, query plan changes,
code deployment adding N+1 queries, traffic routing changes.

## Hypotheses to Score

| Hypothesis               | Key evidence signal                                   |
|--------------------------|-------------------------------------------------------|
| database_slow_query      | DB query duration in logs, lock waits                 |
| downstream_degradation   | Slow logs attributing to specific dependency          |
| gc_pressure              | GC pause duration in logs, heap near limit            |
| connection_pool_wait     | Pool wait time metrics, connection queue depth        |
| deployment_regression    | Latency step change coinciding with deploy            |
| traffic_surge            | Request rate increase preceding latency increase      |

## Success Criteria

- Latency onset time identified from metrics
- Slow component identified (own service vs dependency)
- p50 vs p99 shape analyzed to distinguish outlier vs systemic cause
