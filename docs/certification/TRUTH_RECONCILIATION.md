# SentinelAI — Truth Reconciliation
**Reconciles every accumulated review into one canonical production-readiness position.**
No code changed. Source of authority: the code (`dba716a`), then the reports.

> Headline: the reports do **not** contradict on *facts*. They contradict on *scope labels*
> and *severity weighting*. On determinism specifically, the earlier certifications already
> documented the defect the red-team "found" — but a later report (Trust Index v2) then
> over-scored it. The red-team's severity is canonical; the over-score is corrected here.

## A. Contradiction ledger
Each row: the two positions (with citations), the classification, and the canonical resolution.

### 1. Engine determinism — NON-CONTRADICTION
- DETERMINISM_REPORT: reasoning engine CERTIFIED (1000 repeats → 1 hash).
- Red-team: agrees the engine is deterministic given fixed evidence.
- **Resolution:** TRUE and uncontested. The *reasoning engine*, on fixed evidence with frozen
  learning stores, is byte-deterministic.

### 2. Pipeline determinism — SCOPE DIFFERENCE + a WEIGHTING ERROR (the real one)
- FAILURE_MODE_ANALYSIS: "Determinism of the **live path**: NOT CERTIFIED."
- DETERMINISM_REPORT: "MATERIAL CAVEAT — pipeline determinism ≠ engine determinism … holds only
  with fixed learning stores … CONDITIONAL — stateful pipeline"; opened backlog **C-2**.
- **PRODUCTION_TRUST_INDEX_V2: scored Determinism = 1.00**, claiming "the pipeline caveat … is
  now removed" — but that only removed the **wall-clock** defects (B-1/B-2/B-3); the
  **learning-store mutation** (C-2) was never fixed.
- Red-team: determinism FALSIFIED (PB-1), learning stores mutate corpora read back by analysis.
- **Classification:** the certification and red-team agree on the *fact* (live pipeline
  non-deterministic across a mutating corpus). The genuine error is **PTI v2's 1.00 score**,
  which conflated "wall-clock fixed" with "pipeline deterministic" and dropped its own open C-2.
- **Resolution:** Pipeline determinism = **NOT certified / FALSIFIED** across a mutating corpus.
  PTI v2's Determinism input is corrected from 1.00 → treat as **failing** until C-2/PB-1 fixed.
  The red-team did not discover a new fact here; it correctly **re-rated a documented caveat
  from "conditional/backlog" to "blocker."**

### 3. Replay — SCOPE DIFFERENCE (two different properties)
- DETERMINISM_REPORT / B-3: replay **artifact** is canonical (`sort_keys`), byte-stable. TRUE.
- Red-team (F6): replaying pinned evidence still consults the **live mutating** pattern registry
  + knowledge graph, so the **result** is not reproducible. TRUE.
- **Resolution:** Both hold at their scope. Canonical claim: *"replay reproduces the analysis
  byte-for-byte only given pinned evidence **and** frozen learning corpora; the stored artifact
  is canonical; result reproducibility over time is NOT guaranteed."* Same root cause as #2.

### 4. Evidence loss — COMPLETENESS GAP (partial fix, not a flip)
- FAILURE_MODE_ANALYSIS found silent-swallow; F-obs **fixed 2 of 6** await paths + a worker-error
  sweep.
- Red-team: 4 await paths still log-only, plus `raw_response` malformed wrapping and the gateway
  "all-connected" fallback remain silent.
- **Resolution:** Not a contradiction — F-obs was explicitly a *partial* fix. The claim "evidence
  cannot silently disappear" was never fully true. Genuine open defect **PB-3**.

### 5. Confidence ↔ evidence — NEW GENUINE DEFECT (previously unexamined)
- No prior report audited `compute_confidence`. Red-team: double-count (`confidence.py:64` &
  `:74`) + retrieval boost + hardcoded refs + LLM override (PB-4).
- **Resolution:** Real defect in an area no earlier report examined — no contradiction, a coverage
  gap. Verified from source in this reconciliation.

### 6. IQS — NEW GENUINE DEFECT (self-introduced)
- GOLD_STANDARD presented IQS as the promotion gate. Red-team: `evidence_efficiency` and
  `unnecessary_evidence_avoided` are complements, both weighted (SB-1).
- **Resolution:** Real double-count I introduced; fix before IQS is used to gate anything.

### 7. ODE — NEW GENUINE DEFECT (self-introduced)
- ODE doc presented discovery as statistically sound. Red-team: topology/operational/temporal
  miners have no negative-opportunity denominator + no multiple-comparison correction (SB-2).
- **Resolution:** Real spurious-correlation risk I introduced; fix before ODE output is trusted.

### 8. Overall readiness number — METRIC/AXIS DIFFERENCE (+ inherits #2)
- Certification: "READY for a read-only shadow pilot"; PTI 0.84@0.53; Scientific Validation:
  "READY_AFTER_MORE_SHADOW_EVIDENCE"; Independent Review: "safe, measure don't build."
- Red-team: ~30%, NOT production ready.
- **Resolution:** These measure **different things and mostly agree**: every prior verdict said
  *not ready to promote / read-only only*, and the red-team agrees the **shadow stack is safely
  isolated**. The divergence is that the red-team judged the **marketed product claim**
  ("deterministic/replayable/auditable investigator"), which is falsified, while the earlier
  verdicts judged the **shadow pilot's safety**, which holds. Both are correct on their axis.
  The PTI 0.84 is over-stated only because of the #2 determinism input error.

### 9. Shadow / Wave-3 isolation — AGREEMENT
- Independent Review + certification: shadow stack safe, additive, isolated.
- Red-team: CLAIMS 5, 12, 13, 14 **HELD** under adversarial source review.
- **Resolution:** Genuinely sound. Uncontested by any report.

### 10. "5792 tests pass" vs "not ready" — NON-CONTRADICTION
- **Resolution:** The suite exercises single-run behavior; it does not test cross-corpus
  determinism, replay-over-time, or the confidence arithmetic. Passing tests ≠ claims true — a
  test-coverage gap, not a contradiction.

## B. Classification summary
| Kind | Items |
|---|---|
| **Genuine defects (must fix)** | PB-1 pipeline determinism (learning stores), PB-2 replay non-hermetic, PB-3 evidence silent loss (partial), PB-4 confidence double-count/inflation, SB-1 IQS double-count, SB-2 ODE spurious |
| **Scope / assumption differences (both true)** | #1 engine vs pipeline determinism, #3 artifact vs result reproducibility, #8 shadow-safety vs product-claim axes |
| **A real reporting error (corrected here)** | #2 PTI v2 scored Determinism 1.00 while its own C-2 caveat was open → corrected to failing |
| **Non-contradictions** | #9 shadow isolation (agreement), #10 tests-pass vs not-ready |

**Net:** exactly **six genuine defects**, all traceable to two roots — (R1) default-ON learning
stores mutating corpora that analysis reads back (drives PB-1, PB-2, and the #2/#3 disagreements),
and (R2) confidence/metric arithmetic that counts the same signal twice (PB-4, SB-1). PB-3
(evidence observability) and SB-2 (ODE statistics) are independent. Nothing else is a real
contradiction.

## C. The single canonical production-readiness position
> **SentinelAI is a well-engineered, safely-isolated investigation platform whose SHADOW stack
> and Wave-3 gating are production-grade, but whose AUTHORITATIVE runtime is NOT yet the
> deterministic, replayable, evidence-grounded investigator it is described as. It is approved
> only for a READ-ONLY shadow pilot; it is NOT approved for authority, and the
> "deterministic/replayable/auditable" claim must be withdrawn until R1 is fixed.**

Precise, defensible statements (each survives adversarial source review):
1. The reasoning engine is deterministic on fixed evidence with frozen learning stores. ✅
2. The live pipeline is **non-deterministic across a mutating corpus**; replay is **non-hermetic**.
   The determinism/replay claims hold **only in a frozen-corpus / replay-isolation mode that does
   not exist yet**. ❌ (R1)
3. Confidence is **not** a pure function of current evidence (double-count + boosts + override). ❌ (R2)
4. Evidence can be **silently dropped** at ≥4 sites. ❌ (PB-3)
5. Shadow Tranches 1–5 are strictly additive and never authoritative; produce-only eval is
   isolated; Wave 3 needs two gates and is audit-only even when fully on. ✅
6. IQS and ODE contain statistical defects and must not be used to gate promotion until fixed. ❌
7. ~8.5k LOC (orphans + shadow apparatus) has no runtime/authoritative consumer. (Debt, not a
   correctness blocker.)

Confidence in this canonical position: **high** — every ✅/❌ above is verified from source in
either the red-team pass or this reconciliation, and the one genuine reporting error (PTI v2) is
identified and corrected.

## D. Remediation ordering (do NOT execute yet — this reconciliation only)
Fix by root cause, hardest-and-most-fundamental first:
1. **R1 — determinism/replay (PB-1, PB-2):** add a frozen-corpus / replay-isolation mode so
   `_analyze_evidence` does not read back mutating learning stores during an investigation or a
   replay. This alone restores the core auditability guarantee and resolves ledger #2/#3.
2. **R2 — confidence arithmetic (PB-4):** remove the double-count, gate/remove the retrieval boost
   and LLM override, replace hardcoded `evidence_refs`.
3. **PB-3 — evidence observability:** extend F-obs to all await paths + `raw_response` + gateway
   discovery.
4. **SB-1 / SB-2 — eval statistics:** drop one complementary IQS metric; add negative-opportunity
   denominators + multiple-comparison guard to ODE.
5. **Debt:** delete the ~2,381 LOC orphans; quarantine the produce-only apparatus out of the
   runtime image.

Only after R1/R2 land with tests would the platform move from "extensively engineered" to
"operationally validatable" — and only then should the determinism/replay claims be reinstated.

## E. Correction to the record (accountability)
Two earlier self-authored statements are hereby corrected:
- **PRODUCTION_TRUST_INDEX_V2 Determinism = 1.00** is withdrawn; the correct value is *failing*
  pending R1. The PTI should be recomputed (it drops materially).
- **DETERMINISM_REPORT verdict "CERTIFIED … + replay"** should read **"engine-only; live pipeline
  and result-level replay NOT certified"** — the caveat was present but the top-line verdict
  under-weighted it.
The red-team did not overturn the facts of the earlier reports; it corrected their *severity*.
This reconciliation adopts the red-team severity as canonical.
