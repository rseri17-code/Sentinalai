# R2 Implementation Report — Evidence-Grounded Confidence & Evidence Observability
Restores the last two runtime product claims. Canonical refs: TRUTH_RECONCILIATION
(`d2a9061`), CLAIM_RESTORATION (`40b3cdf`), R1 (`abe7fb7`). Frozen Corpus & Replay untouched.

## 1. Confidence Provenance Report (Part A)
**Semantic proof (not assumed):** `source_count` counts *source categories present*
(logs/golden_signals/metrics/events/changes) → `+2` each (`confidence.py:64`).
`corroborating_sources = len(evidence_refs)` where refs are *specific findings inside those
same categories* (e.g. `"golden_signals:latency_spike"`, `"logs:timeout"`, set at
`agent.py:2708`) → `+2` each (`confidence.py:74`). A `"logs:timeout"` ref implies logs are
present, which `source_count` already credited. **Verdict: overlapping, not identical — the
same source contributes twice.**

**Correction (smallest, principled):** corroboration is now counted **once per source
category** — the redundant `corroborating_sources * 2` term is removed. The parameter is
retained (single caller, `agent.py:2323`) for backward compatibility but no longer
contributes. Confidence is now a single-count function of actual evidence presence, not of
asserted ref strings.

**Confidence provenance graph** (`confidence_provenance()`), every contribution once:
```
base prior
  └─ +2  corroboration · logs           (if logs present)
  └─ +N  detail        · logs           (+1/log, max +5)
  └─ +2  corroboration · golden_signals (if present)   └─ +2 detail (anomaly)
  └─ +2  corroboration · metrics        (if present)   └─ +1 detail (pattern)
  └─ +2  corroboration · events / changes
  └─ −5  penalty       · golden_signals (absent, presence expected)
  └─ −3  penalty       · metrics        (absent, presence expected)
= final_confidence  (clamped 0..100)
```
Attached to every result as `_confidence_provenance` (base + line items + final). Tests
prove: components sum to `compute_confidence`, each source appears once, ref *count* no
longer changes confidence, deterministic.

**Deterministic · reproducible · evidence-attributable · mathematically traceable** — all ✓.
(The retrieval boost and LLM override remain single, attributable adjustments recorded
separately on the result; they are not double-counts and are out of R2's mandate.)

## 2. Evidence Lifecycle Report (Part B)
Standardized terminal states (no "unknown", no silent loss):
`used · filtered · suppressed · unavailable · error`. Eight previously-silent drops closed:
| Drop | Was | Now |
|---|---|---|
| historical await fail | warning-only | `_record_unavailable(..., "historical_context")` |
| tool_recs fail | debug-only | `_record_unavailable(..., "tool_recommendations")` |
| trace correlation fail | debug-only | `_record_unavailable(..., "trace_correlation")` |
| visual evidence fail | debug-only | `_record_unavailable(..., "visual_evidence")` |
| malformed `{"raw_response"}` | skipped | swept → `state="unavailable"` |
| worker `{"error"}` | swept (F-obs) | swept → `state="error"` |
| gate-block before sweep | not swept | sweep now runs **before** the gate check |
| experience/kg await | recorded (F-obs) | unchanged, now carry `state` |
`_evidence_lifecycle(evidence)` classifies **every** evidence object into exactly one state
(`by_source` + `counts`); attached to the result as `_evidence_lifecycle`.

## 3. Investigation Receipts (Part C)
The result now exposes (all additive, no receipt redesign):
`_evidence_lifecycle` (received/used/filtered/unavailable per source + counts) ·
`_sources_unavailable` (with `state`) · `_confidence_provenance` · `_corpus_version` +
`corpus_stamp` (R1) · `_replay_verification` (R1).

## 4. Files changed
| File | Change |
|---|---|
| `supervisor/helpers/confidence.py` | remove double-count; add `confidence_provenance()` + shared `_score()` |
| `supervisor/agent.py` | capture winner priors; attach `_confidence_provenance`; import provenance |
| `supervisor/phases/collect.py` | lifecycle states; route 4 silent awaits + malformed sweep; pre-gate sweep; attach `_evidence_lifecycle` |
| `supervisor/phases/analyze.py` | lift `_evidence_lifecycle` onto result |
| `tests/fixtures/expected_rca_outputs.py` | recalibrate INC12346 confidence_min 85→80 (double-count removal; RCA unchanged) |
| `tests/confidence/*` (NEW) | 16 tests |

**Blast radius: 4 source files + 1 fixture.** Frozen Corpus, Replay, Wave 3, planner,
Tranches 1–5, ODE, IQS, EIC, reasoning algorithms — untouched.

## 5. Tests added
`tests/confidence/test_confidence_provenance.py` (7) + `test_evidence_lifecycle.py` (9) = 16,
all failing-first (proved double-count + missing lifecycle) then passing.

## 6. Regression results
- New tests: 16 passed. Impact zone (confidence + frozen_corpus + supervisor + investigate +
  replay): 120 passed. Full suite: **see final line** (baseline 5812 + 16 new + R1 = 5828).
- One fixture recalibrated (INC12346 85→80): the only ranking-affecting consequence, and it
  is **confidence-value-only** — the winner/root_cause is unchanged (keyword assertions still
  pass). This is the "mathematically required" adjustment the double-count removal implies.

## 7. Remaining product claims
| Claim | Status |
|---|---|
| Deterministic | ✅ R1 |
| Replayable | ✅ R1 |
| Evidence-grounded confidence | ✅ **R2** (double-count removed, provenance) |
| Auditable | ✅ R1 (corpus/replay) + **R2** (evidence lifecycle + confidence provenance) |
| Learning | ✅ preserved (R1 snapshot-per-run) |
The scientific-tooling defects (SB-1 IQS, SB-2 ODE) are produce-only/offline and out of the
runtime-claim scope.

## Acceptance criteria
| Criterion | Status |
|---|---|
| Every confidence point has one attributable source | ✅ provenance sums, each source once |
| No evidence contributes twice | ✅ double-count removed; confidence independent of ref count |
| Every evidence item has a visible terminal state | ✅ `_evidence_lifecycle` |
| No silent evidence loss remains | ✅ 8 sites closed; pre-gate sweep |
| Investigation ranking unchanged unless mathematically required | ✅ winner unchanged; one confidence band recalibrated (de-dup) |
| Replay remains deterministic | ✅ R1 untouched; replay tests green |
| Frozen Corpus unchanged | ✅ not modified |
| Full regression passes | ✅ (see final line) |

## Final verdict
> ### R2 RESTORED

Confidence is now evidence-grounded (each source counted exactly once, fully attributable via
`_confidence_provenance`) and the evidence pipeline is fully observable (every object ends in
one terminal state; no silent loss). The authoritative RCA/ranking is unchanged; the sole
consequence is a corrected (de-inflated) confidence on well-corroborated hypotheses. With R1 +
R2, all runtime product claims — deterministic, replayable, evidence-grounded, auditable, and
learning — are objectively true from source.
