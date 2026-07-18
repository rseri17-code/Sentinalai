# OVP — Product Readiness Checklist
For a broader **read-only** rollout. Each item: Owner · Status (✅/⚠️/❌) · Evidence. Runtime
authority / Wave 3 remain out of scope (separate readiness program).

## Operational readiness
- [ ] Runtime deterministic (R1) — same incident + `corpus_version` → byte-identical. **✅** `test_same_corpus_version_same_result`.
- [ ] Hermetic replay (R1) — replay reads recorded corpus, writes nothing. **✅** R1 report.
- [ ] Evidence-grounded confidence (R2) — no double-count; `_confidence_provenance`. **✅** R2 report.
- [ ] No silent evidence loss (R2) — every source has a terminal state. **✅** `_evidence_lifecycle`.
- [ ] Graceful degradation — dependency failure never aborts an investigation. **✅** crash-safe (`_call_worker`).
- [ ] Concurrency — per-investigation Frozen Corpus isolation. **✅** `test_concurrent_investigations_isolated`.
- [ ] Load/worst-case latency at target concurrency. **❌ NOT MEASURED** — build a load harness (pre-rollout).

## Governance
- [ ] Runtime authority OFF; Wave 3 OFF; two-gate retrieval (audit-only). **✅** red-team CLAIM 13 held.
- [ ] Change-control: corpus snapshots + artifacts are append-only, `corpus_version`-stamped. **✅** R1.
- [ ] Ground-truth labeling protocol with leakage controls. **✅ designed** (rubric 04) — execution pending.
- [ ] Data handling / redaction on evidence + receipts. **✅** RC-A redaction (verify per-tenant).

## Observability
- [ ] Every investigation emits receipts + `_evidence_lifecycle` + `_confidence_provenance` + `corpus_stamp` + `_replay_verification`. **✅** R1/R2.
- [ ] Dependency-health signals (source availability, worker-failure rate). **✅** F-obs/R2.
- [ ] Determinism / replay-stability metric emitted per period. **✅** shadow_pilot.

## Monitoring & alerting
- [ ] Regression watch on primary KPIs with reason/first/last/action. **✅** `shadow_pilot.regression_watch`.
- [ ] Alert on: determinism→REVIEW, safety agreement < 0.95, verified-incorrect > 0, corpus drift. **✅ designed** (plan 01).
- [ ] Dashboards for the three scorecards. **⚠️ spec only** (03) — wire to a viewer.

## Supportability
- [ ] Runbook: capture→use→persist→replay lifecycle; frozen-corpus/replay failure modes. **✅** R1 report §concurrency + this checklist.
- [ ] On-call playbook for a paused pilot (flags OFF = instant read-only rollback). **✅** plan 01 rollback.
- [ ] Escalation for dependency outages surfaced via `_sources_unavailable`. **⚠️ define owners**.

## Documentation
- [ ] Operator guide (how to read a shadow investigation). **✅** `docs/shadow_pilot/06`.
- [ ] KPI definitions + scorecards + rubric. **✅** this OVP set.
- [ ] Truth Reconciliation + R1/R2 reports as the canonical record. **✅** committed.

## Rollout
- [ ] Read-only shadow pilot in pilot services (90-day). **✅ planned** (01).
- [ ] Go/No-Go criteria defined + gated on measured evidence. **✅** (01).
- [ ] Staged expansion (pilot services → broader read-only) on GO. **✅ planned**.
- [ ] No expansion to authority/Wave 3 without the separate readiness program. **✅** enforced.

## Readiness summary
**Runtime contracts: READY (R1+R2).** Blocking gaps before a broader rollout are **operational,
not correctness**: load/worst-case latency (NOT MEASURED) and dashboard wiring (spec only), plus
executing the labeling protocol to reach the 500/20 corpus. None require new engines.
