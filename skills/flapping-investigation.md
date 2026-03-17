---
skill: flapping-investigation
description: >
  Investigate an intermittently failing service (flapping). Identifies the
  oscillation pattern, root cause of instability (connection pool, health
  check misconfiguration, sawtooth memory, etc.).
playbook: flapping
incident_types: [flapping]
max_calls: 5
---

# Flapping Investigation Skill

## When to Use

Activate when the incident summary contains: `flapping`, `intermittent`,
`sporadic`, `oscillating`, `bouncing`, `unstable`, `up-down`,
`recovering-failing`.

## Investigation Steps (ordered)

### Step 1 — Fetch Incident (`ops_worker.get_incident_by_id`)

### Step 2 — Search Error Logs (`log_worker.search_logs`)
Query: `error {service}`
Look for: repeating error patterns with regular intervals, health check
failures, connection pool errors cycling between available/exhausted.

### Step 3 — Check Golden Signals (`apm_worker.get_golden_signals`)
Look for: sawtooth error rate pattern, regular recovery-failure cycles,
saturation cycling (pool emptying and refilling), latency oscillating.

### Step 4 — Check Pool Metrics (`metrics_worker.query_metrics`)
Metric hint: `db_connection_pool_active`
Look for: connection pool oscillating between exhausted and recovering,
circuit breaker in half-open cycling, thread pool utilization sawtooth.

### Step 5 — Check Changes (`log_worker.get_change_data`)
Look for: health check timeout value change, connection pool size change,
retry policy change that causes regular retry waves.

## Hypotheses to Score

| Hypothesis                   | Key evidence signal                               |
|------------------------------|---------------------------------------------------|
| connection_pool_sawtooth     | Pool metrics show regular exhaust/recover cycle   |
| health_check_misconfiguration| Health checks failing/passing with exact timeout  |
| gc_induced_flapping          | GC pause duration exceeds health check timeout    |
| retry_wave_amplification     | Retry storm creating regular pressure waves       |
| upstream_instability         | Upstream service flapping, not the target service |

## Success Criteria

- Oscillation period identified from metrics (every N seconds/minutes)
- Root oscillation source identified (own service vs dependency)
- Whether it's self-resolving (sawtooth) or requires intervention
