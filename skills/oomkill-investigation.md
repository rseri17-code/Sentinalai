---
skill: oomkill-investigation
description: >
  Investigate an OOMKill incident. Checks memory metrics, OOM logs, Kubernetes
  events, and heap usage patterns to identify the root cause of container
  memory exhaustion. Use for: OOMKilled, out of memory, heap exhaustion,
  container killed incidents.
playbook: oomkill
incident_types: [oomkill]
max_calls: 5
---

# OOMKill Investigation Skill

## When to Use

Activate when the incident summary contains: `oomkill`, `oom`, `out of memory`,
`killed`, `memory pressure`, `container killed`, `cgroup`, `heap exhaustion`.

## Investigation Steps (ordered)

### Step 1 — Fetch Incident (`ops_worker.get_incident_by_id`)
Retrieve the canonical incident record. Required before all other steps.

### Step 2 — Search OOM Logs (`log_worker.search_logs`)
Query: `OOMKilled {service}`
Look for: kernel OOM killer logs, container exit code 137, Java heap space
errors, Go runtime: out of memory messages.

### Step 3 — Check Memory Metrics (`metrics_worker.query_metrics`)
Metric hint: `memory_usage_bytes`
Look for: memory usage approaching container limit, sudden spikes,
gradual memory growth (leak pattern), no memory headroom.

### Step 4 — Check Events (`metrics_worker.get_events`)
Look for: Kubernetes OOMKilled events, container restart events,
node memory pressure events, eviction events.

### Step 5 — Search Memory Logs (`log_worker.search_logs`)
Query: `{service} heap OR memory`
Look for: GC pressure logs, heap dump triggers, large object allocations,
memory pool exhaustion warnings.

## Hypotheses to Score

| Hypothesis              | Key evidence signal                                  |
|-------------------------|------------------------------------------------------|
| memory_leak             | Gradual ramp in memory metrics, no plateau           |
| traffic_spike           | Sudden memory spike correlating with request surge   |
| large_payload           | Logs show unusually large object allocations         |
| container_limit_too_low | Memory usage consistently near limit without leak    |
| cache_unbounded_growth  | Cache metrics growing without eviction               |

## Success Criteria

- Memory metrics showing clear growth or saturation pattern
- OOM event timestamp correlating with memory metric crossing threshold
- Hypothesis identifies whether this is a leak, limit, or traffic problem
