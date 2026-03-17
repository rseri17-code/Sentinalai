---
skill: missing-data-investigation
description: >
  Investigate missing or degraded telemetry: no metrics, data gaps, stale
  dashboards, null values in data pipelines. Distinguishes between
  "service is broken" and "observability is broken".
playbook: missing_data
incident_types: [missing_data]
max_calls: 5
---

# Missing Data Investigation Skill

## When to Use

Activate when the incident summary contains: `degraded`, `missing data`,
`partial`, `data gap`, `stale data`, `no metrics`, `telemetry gap`, `null values`.

## Investigation Steps (ordered)

### Step 1 — Fetch Incident (`ops_worker.get_incident_by_id`)

### Step 2 — Search Error Logs (`log_worker.search_logs`)
Query: `error connection {service}`
Look for: metric exporter errors, OTEL collector failures, agent connection
errors, database write failures from the metrics pipeline.

### Step 3 — Check Golden Signals (`apm_worker.get_golden_signals`)
Look for: golden signals still available (observability infra OK, data gap
is in specific pipeline) vs all signals missing (observability layer broken).

### Step 4 — Check Events (`metrics_worker.get_events`)
Look for: metric agent restart events, collector crash events, scrape
failure events, storage backend errors.

### Step 5 — Check Changes (`log_worker.get_change_data`)
Look for: OTEL config changes, metric scrape interval changes, retention
policy changes, storage migration events.

## Hypotheses to Score

| Hypothesis                    | Key evidence signal                                |
|-------------------------------|----------------------------------------------------|
| otel_collector_failure        | Collector crash/restart event, no metrics after    |
| metric_agent_misconfiguration | Config change + gap start time correlated          |
| storage_backend_failure       | Storage write errors in collector logs             |
| network_partition_to_metrics  | Collector can't reach storage, golden signals OK   |
| scrape_endpoint_removed       | Scrape target disappears from config               |

## Success Criteria

- Determined: is the service broken or is observability broken?
- Gap start time identified from last good data point
- Specific component in the telemetry pipeline identified as source
