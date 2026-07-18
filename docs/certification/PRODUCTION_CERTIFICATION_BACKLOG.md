# PRODUCTION_CERTIFICATION_BACKLOG.md
**SentinelAI — Production Readiness Certification · Phase 12**
Ordered strictly by production-risk reduction. No feature work — every item raises trust.
Severity: **P0** (blocker) · **P1** (pre-rollout) · **P2** (pre-Wave-3) · **P3** (hygiene).

---
## P0 — Determinism defects in the live path (BLOCKERS)

### B-1 · Wall-clock change-query window (`agent.py:2094`, D-1)
- **Why it matters:** breaks the product's core "same input → same output" guarantee and
  replay fidelity — a change-evidence query window is derived from `datetime.now()`.
- **Evidence:** verified at `agent.py:2094-2098`; comment claims incident-anchoring, impl
  uses wall-clock.
- **User/ops impact:** the same incident replayed later can surface different change
  evidence → different RCA. Undermines auditability.
- **Fix:** derive the window from `incident.created_at` (fixed look-back), never `now()`.
- **Test strategy:** unit test asserting identical `time_window_hours` for the same
  incident at two mocked wall-clock times; add to `tests/test_determinism.py`.
- **Rollback:** single-function revert; behaviour is internal.
- **Effort:** S (½ day). **Deps:** none. **Success:** window invariant to wall-clock.

### B-2 · Wall-clock `incident_hour` in DNA fingerprint (`agent.py:3277`, D-2)
- **Why:** `now().hour` feeds the Incident-DNA fingerprint → pattern-match → primed
  hypotheses → can change ranked output.
- **Evidence:** verified `agent.py:3277`.
- **Impact:** same incident at a different hour → different priming → possible RCA drift.
- **Fix:** derive hour from the incident timestamp.
- **Test:** fingerprint invariance across mocked clocks.
- **Rollback:** single-line revert. **Effort:** S. **Deps:** none. **Success:** fingerprint
  invariant to wall-clock.

### B-3 · Canonicalise replay artifact + RCA report (`replay.py:81`, `rca_report.py:79`, D-3/D-4)
- **Why:** the byte-identical-output claim is not enforced on the replay artifact or the
  human report — only internal serializers sort. Replay is the determinism backbone.
- **Impact:** two runs can store byte-different artifacts for identical logical content →
  weakens replay as evidence.
- **Fix:** route both through the existing canonical JSON (`sort_keys=True`).
- **Test:** byte-equality of the replay artifact across two identical runs.
- **Rollback:** revert serializer call. **Effort:** S. **Deps:** none. **Success:** artifact
  byte-identical across runs.

---
## P1 — Pre-rollout (measure before controlled rollout)

### C-1 · Concurrency / load harness (operability NOT MEASURED)
- **Why:** behaviour at 100/500/1000 concurrent investigations, queueing, worst-case
  latency is unknown.
- **Evidence:** PRODUCTION_OPERABILITY_REPORT — reasoning overhead measured (p99 0.34ms),
  end-to-end load NOT MEASURED.
- **Fix:** deterministic load harness driving `investigate()` against fixtures at N-fan-out.
- **Test:** latency percentiles + error rate vs concurrency curve.
- **Rollback:** harness is offline/test-only. **Effort:** M. **Success:** p99 and failure
  rate characterised to 1000 concurrent.

### C-2 · Characterise learning-store drift (pipeline determinism caveat)
- **Why:** 3 default-ON stores write `now()` into persistent JSON that primes future runs
  → pipeline non-determinism across a mutating corpus.
- **Evidence:** SYSTEM_REALITY §global caveat; DETERMINISM_REPORT caveat.
- **Fix:** add a "frozen-store" mode for replay/determinism runs; measure drift magnitude.
- **Test:** same incident across two store states → quantify RCA/ordering delta.
- **Rollback:** mode is opt-in. **Effort:** M. **Success:** drift bounded + documented;
  replay unaffected (already true).

### C-3 · Fault-injection harness for dependencies + timing-based evidence-set (D-5)
- **Why:** `_execute_playbook` timing-based future cancellation can change the evidence set
  run-to-run; no systematic dependency-failure test asserts graceful degradation.
- **Fix:** deterministic fault-injection; gate loop-escalation cancellation off in
  deterministic/replay mode.
- **Test:** evidence-set stability under identical inputs; degradation assertions per source.
- **Effort:** M. **Success:** evidence set invariant in deterministic mode.

### F-obs · Failure observability (silent evidence loss)
- **Why:** `collect.py:218-221`, `262-265` swallow future failures with no log; optimistic
  `_ALL_KNOWN_SERVERS` fallback (`mcp_client.py:975`) masks a down gateway.
- **Fix:** log every swallowed evidence-source failure at WARNING; surface a per-investigation
  "sources_unavailable" list so low-confidence-from-outage is distinguishable from
  low-confidence-from-clean-signal.
- **Test:** assert a WARNING + metadata entry on injected source failure.
- **Effort:** S. **Success:** every dropped source is visible to operators.

---
## P2 — Pre-Wave-3 (validation evidence)

### V-1 · Labeled, held-out corpus (≥500, ≥20/class)
- **Why:** every quality/calibration/benchmark claim is underpowered at N=3; dev fixtures ==
  eval fixtures (leakage).
- **Evidence:** SCIENTIFIC_VALIDATION_AUDIT F-2/F-5; readiness gate G1 failing.
- **Fix:** accumulate via the Longitudinal Shadow Plan; enforce a train/eval/held-out split.
- **Effort:** L (90-day shadow). **Success:** G1 passes on a leakage-free corpus.

### V-4 · SentinelBench: score live output, not synthetic fixtures
- **Why:** `runner.py:25 _build_fixture_result` scores synthetic results — not evidence of
  the live pipeline.
- **Fix:** add a mode that runs SentinelBench scenarios through `investigate()`.
- **Effort:** M. **Success:** bench scores reflect real `investigate()` output.

### V-2 · Semantic (non-keyword) correctness adjudication for promotion-grade scoring
- **Why:** keyword matching is a proxy (false pos/neg). **Effort:** M.

---
## P3 — Hygiene (pre-Wave-3, not rollout-blocking)

- **T-1** Delete dead `intelligence/confidence_calibrator.py` (test-only). Effort S.
- **T-2** Consolidate duplicate `_tokens`/`_jaccard`/`_clamp`/`_round`; single `stable_id()`
  for 31 sha256 sites. Effort M. (Determinism-relevant.)
- **T-3** Decompose `_persist_results` (458 LOC) + thin `agent.py` (3,799 LOC) within-module.
  Effort M.
- **T-4** Remove 9 cross-package private imports; de-dup `_redact_params`. Effort S.
- **T-5** Prune ~10 permanently-ON flags. Effort S.

---
## Sequencing
1. **B-1, B-2, B-3** (P0) — small, surgical, unblock the determinism claim. Do first.
2. **C-1, C-2, C-3, F-obs** (P1) — build the harnesses; measure load/drift/faults.
3. **V-1** (P2, 90-day) — runs in parallel with the above via shadow.
4. **T-*** (P3) — hygiene before Wave-3 expansion.

Nothing here adds product features; every item converts an unproven or defective property
into a measured, trustworthy one.
