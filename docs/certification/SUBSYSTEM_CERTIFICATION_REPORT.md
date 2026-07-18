# SUBSYSTEM_CERTIFICATION_REPORT.md
**SentinelAI — Production Readiness Certification · Phase 10**
One status per subsystem. **Nothing is CERTIFIED without objective evidence.**
Statuses: **CERTIFIED** · **CONDITIONALLY CERTIFIED** (works, bounded gap) · **NOT CERTIFIED**.

| Subsystem | Status | Evidence | Known risk / gap | Rollback |
|---|---|---|---|---|
| **Shadow reasoning engines T1–T5** | **CERTIFIED** | 1000/1000 deterministic; flag-OFF no-op PASS; 5665 tests; additive-only, never touch root_cause | benefit unproven (N=3) — but that's an *evidence* gap, not a subsystem defect | flag OFF ⇒ byte-identical |
| **Stabilization guarantees RC-A…RC-L** | **CERTIFIED** | 165 tests; framed hashing, append-only, redaction, canonical helpers | none | n/a |
| **Receipts / artifacts (canonical serializers)** | **CERTIFIED** | `serialization.py:26`, `receipts.py:154` sort_keys; deterministic ids | none in canonical path | flag-gated |
| **Evidence Gates G1–G5** | **CERTIFIED** | deterministic, fail-closed when ON (default), blocks fabricated RCA | it is the *only* fail-closed control | flag OFF (not advised) |
| **Five-phase pipeline (fetch→persist)** | **CONDITIONALLY CERTIFIED** | crash-safe, graceful degradation, receipts per phase | D-1/D-2 wall-clock defects in the analyze/collect helpers; fail-fast at phase boundary | phases are mandatory |
| **`_call_worker` / MCP gateway** | **CONDITIONALLY CERTIFIED** | timeout+retry+breaker; every source returns a dict, never raises | silent evidence loss (no log); optimistic `_ALL_KNOWN_SERVERS` fallback masks outage | n/a |
| **Replay** | **CONDITIONALLY CERTIFIED** | re-analysis reproduces RCA; 58 tests green | **artifact not canonicalised (D-3)** → byte-stability of stored artifact unproven | inert when replay dir unset |
| **Live planner (`get_evolved_playbook` + strategy_evolver)** | **CONDITIONALLY CERTIFIED** | deterministic stable-sort given fixed state | strategy_evolver writes wall-clock; ordering drifts across a mutating corpus | fail-open → base playbook |
| **Learning stores (experience/KG/strategy_evolver)** | **CONDITIONALLY CERTIFIED** | advisory only; never set root_cause/confidence | write `now()` into persistent JSON that primes future runs → pipeline non-determinism | flags default ON; can freeze |
| **Confidence calibration** | **NOT CERTIFIED** | calibrator present; passthrough by default | calibration vs correctness underpowered (N=3, MAE inverted) | flag OFF → passthrough |
| **Investigation quality signals (localization/grounding/citation)** | **NOT CERTIFIED** | produced deterministically per investigation | unvalidated vs ground truth at scale (N=3) | shadow / advisory |
| **SentinelBench** | **NOT CERTIFIED (as E2E validator)** | scorer deterministic | **scores synthetic fixtures, not live `investigate()` output** (runner.py:25) — not end-to-end evidence | offline harness |
| **Operational readiness (load/concurrency/runbooks)** | **NOT CERTIFIED** | reasoning overhead measured (p99 0.34ms) | concurrency/worst-case/queue/chaos NOT MEASURED | n/a |
| **`intelligence/confidence_calibrator.py`** | **NOT CERTIFIED (dead)** | zero runtime importers, test-only | delete candidate | delete |

## Certification tally
- **CERTIFIED:** 4 (shadow engines, stabilization, canonical serializers, evidence gates)
- **CONDITIONALLY CERTIFIED:** 5 (pipeline, worker layer, replay, planner, learning stores)
- **NOT CERTIFIED:** 5 (calibration, quality-vs-truth, SentinelBench-as-E2E, operability, dead module)

## Interpretation
The **deterministic, additive core is certified.** What is *not* certified splits into two
kinds: (a) **defects** — D-1/D-2/D-3 determinism gaps, the dead module; fix these; and
(b) **evidence gaps** — calibration, quality, operability; these need corpus + harnesses,
not code. No subsystem is unsafe; several are unproven.
