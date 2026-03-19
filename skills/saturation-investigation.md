---
skill: saturation-investigation
description: >
  Investigate resource saturation: CPU throttling, disk full, thread pool
  exhaustion, file descriptor exhaustion, inode limits. Use for: cpu, disk
  full, thread exhaustion, resource limit, saturation incidents.
playbook: saturation
incident_types: [saturation]
max_calls: 6
---

# Saturation Investigation Skill

## When to Use

Activate when the incident summary contains: `cpu`, `saturation`, `exhaustion`,
`disk full`, `cpu throttle`, `inode`, `file descriptor`, `thread exhaustion`,
`resource limit`.

## Investigation Steps (ordered)

### Step 1 — Fetch Incident (`ops_worker.get_incident_by_id`)

### Step 2 — Check Golden Signals (`apm_worker.get_golden_signals`)
Look for: saturation signal > 80%, which resource is saturated (CPU, memory,
network I/O, disk I/O), whether saturation preceded error/latency increase.

### Step 3 — Check CPU Metrics (`metrics_worker.query_metrics`)
Metric hint: `cpu_usage_percent`
Look for: CPU near 100%, throttling periods, gradual ramp vs sudden spike,
which container/pod is consuming most CPU.

### Step 4 — Search CPU Logs (`log_worker.search_logs`)
Query: `cpu OR thread {service}`
Look for: thread pool exhaustion messages, I/O wait logs, blocking call logs,
CPU-bound computation logs (tight loops, regex on large payloads).

### Step 5 — Check Changes (`log_worker.get_change_data`)

### Step 6 — Check ITSM Changes (`itsm_worker.get_change_records`)
Look for: resource limit changes, horizontal pod autoscaler config changes,
quota modifications, infrastructure resizing events.

## Hypotheses to Score

| Hypothesis              | Key evidence signal                                      |
|-------------------------|----------------------------------------------------------|
| cpu_spike_from_traffic  | CPU increase proportional to request rate               |
| cpu_limit_too_low       | CPU throttling at container limit, not raw usage         |
| runaway_thread          | Single thread consuming > 80% CPU                       |
| disk_full               | Disk usage > 95%, write errors in logs                  |
| fd_exhaustion           | "too many open files" error in logs                     |
| thread_pool_exhaustion  | Thread pool queue depth growing, task rejection logs     |

## Success Criteria

- Specific resource identified (CPU, disk, FD, thread)
- Saturation onset time from metrics
- Whether saturation is from load growth, leak, or limit mismatch
