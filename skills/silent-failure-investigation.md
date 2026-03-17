---
skill: silent-failure-investigation
description: >
  Investigate a silent failure: throughput drop with no visible errors,
  zero traffic, stale queues, backpressure. The service appears healthy
  but is not processing work.
playbook: silent_failure
incident_types: [silent_failure]
max_calls: 5
---

# Silent Failure Investigation Skill

## When to Use

Activate when the incident summary contains: `throughput drop`, `throughput`,
`stale`, `silent`, `zero traffic`, `no requests`, `queue backup`, `backpressure`.

## Investigation Steps (ordered)

### Step 1 — Fetch Incident (`ops_worker.get_incident_by_id`)

### Step 2 — Search Service Logs (`log_worker.search_logs`)
Query: `{service}`
Look for: absence of expected log entries (silent = no processing logs),
consumer group lag in Kafka logs, queue depth growing without consumption,
pipeline stuck messages.

### Step 3 — Check Golden Signals (`apm_worker.get_golden_signals`)
Look for: traffic/throughput near zero while error rate stays low (the
"silent" signature), saturation at zero (underload), no latency data
(no requests being processed).

### Step 4 — Search Pipeline Logs (`log_worker.search_logs`)
Query: `pipeline {service}`
Look for: pipeline stall logs, stuck consumer, offset not advancing,
dead letter queue growth, worker pool idle with queue backing up.

### Step 5 — Check Traffic Metrics (`metrics_worker.query_metrics`)
Look for: request rate drop to zero, consumer group lag growing, queue
depth metric, worker utilization at zero vs queue depth.

## Hypotheses to Score

| Hypothesis               | Key evidence signal                                    |
|--------------------------|--------------------------------------------------------|
| consumer_group_stalled   | Kafka/queue consumer lag growing, no processing logs  |
| pipeline_deadlock        | No progress in pipeline logs, workers idle            |
| traffic_routing_failure  | Zero requests despite upstream having traffic         |
| feature_flag_disabled    | Config change disabling processing, near-zero traffic |
| upstream_stopped_sending | Upstream throughput dropped, not the service itself   |

## Success Criteria

- Confirmed: traffic is missing (not just failing) from metrics
- Queue/pipeline depth growing confirmed vs traffic gone upstream
- Root cause identifies where in the pipeline traffic stopped
