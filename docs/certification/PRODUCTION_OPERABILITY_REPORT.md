# PRODUCTION_OPERABILITY_REPORT.md
**SentinelAI — Production Readiness Certification · Phase 7**

## Objective
Measure investigation latency, resource cost, and behaviour under concurrency,
timeouts, and degradation.

## Measured Results

### Shadow reasoning overhead (measured, 2000 iterations)
Per-investigation added cost of the full Tranche 4 + Tranche 5 reasoning:
```
mean = 0.230 ms   p50 = 0.219 ms   p99 = 0.336 ms   max = 0.593 ms
```
**Sub-millisecond, p99 < 0.34 ms.** The Investigation Intelligence stack adds
negligible latency to an investigation whose dominant cost is external evidence
fetch + LLM inference (seconds). Overhead is not a rollout concern.

### Determinism/CPU profile
The reasoning engines are pure Python arithmetic over small in-memory dicts —
no I/O, no threads, no allocation hotspots. Memory footprint is bounded by the
evidence snapshot + hypothesis graph (tens of KB per investigation).

## NOT MEASURED (honest gaps — require a load harness this audit did not build)
| Metric | Status | Why |
|---|---|---|
| End-to-end investigation latency (fetch→persist) | **NOT MEASURED** | dominated by live Splunk/Dynatrace/LLM calls; no production-like fixture load in this environment |
| Worst-case latency under slow dependencies | **NOT MEASURED** | requires fault-injection harness (see backlog C-3) |
| 100 / 500 / 1000 concurrent investigations | **NOT MEASURED** | no concurrency/load harness exists; the pipeline is synchronous per-investigation |
| Queue behaviour / backpressure | **NOT MEASURED** | orchestration/queueing is out of the reasoning core's scope |
| Circuit breakers / retries | **PARTIAL** | per-dependency handling exists (see FAILURE_MODE_ANALYSIS.md); no global breaker measured |

## Assessment
- **Reasoning core:** operability-CERTIFIED — negligible, bounded, sub-ms cost.
- **End-to-end pipeline under load:** **NOT CERTIFIED — insufficient evidence.**
  Concurrency and worst-case latency are unmeasured. This is the single largest
  operability gap and is the top item in the certification backlog (C-1/C-3):
  build a deterministic load + fault-injection harness before any rollout that
  fans out beyond one investigation at a time.

## Verdict
**CONDITIONALLY CERTIFIED.** The added intelligence is operationally free; the
end-to-end pipeline's behaviour under concurrent production load is unproven and
must be measured before controlled rollout.
