# SentinelAI — Red-Team Production Audit (Mission Zero)
**Independent adversarial source-only audit. Objective: falsify. Trust only code.**
Four independent red-team tracks + author self-audit. No code changed. HEAD `189608b`.

> Bottom line up front: the platform's two headline guarantees — **deterministic,
> replayable investigations** and **evidence-grounded confidence** — are **both falsified
> from source** under default-ON production configuration. The safety isolation (shadow /
> Wave-3) is genuinely sound. ~8.5k LOC has no runtime or authoritative consumer.

## 1. Executive verdict
**NOT PRODUCTION READY as marketed** ("deterministic, replayable, auditable, evidence-grounded
autonomous investigator"). It is usable as a *read-only advisory that emits an RCA plus an
explanation*, but four of the five foundational claims fail against source. The engineering
hygiene is real (5792 tests pass; shadow/Wave-3 isolation holds), but the properties that
constitute the product's differentiation do not hold in the default runtime.

## 2. Production blockers (falsified core claims)
- **PB-1 · Determinism FALSIFIED (CLAIM 1).** Default-ON learning stores mutate corpora that
  `_analyze_evidence` reads back: pattern registry (`agent.py:2270` match ← `1027` record;
  centroid running-average + `last_seen=now()` persisted, `pattern_registry.py:153-160`),
  strategy evolver (`agent.py:1898/1907` — mutates the *evidence set* via step-skip weights),
  experience/KG priming (`collect.py:246-314` → `_suggested_root_causes`). Same incident run
  twice against a warm corpus can return a different winner/confidence.
- **PB-2 · Replay non-hermetic FALSIFIED (CLAIM 2).** Replay pins *evidence* and re-runs
  analysis (good; artifact is canonical, B-3 holds), but `_analyze_evidence` still consults
  the *live mutating* pattern registry + knowledge graph (`agent.py:2270`, `2371-2384`).
  Replaying identical pinned evidence at T1 vs T2 drifts. Replay does not reproduce the result.
- **PB-3 · Evidence silently disappears FALSIFIED (CLAIM 3).** F-obs calls `_record_unavailable`
  on only **2 of 6** future-await failures; historical/tool_recs/trace/visual failures are
  log-only (`collect.py`), malformed worker responses wrap as `{"raw_response":...}` and are
  skipped by `_scan_worker_errors`, and the gateway discovery fallback reports a **down gateway
  as "all connected"** (`mcp_client.py:975-1002`). Operators cannot distinguish "clean, low
  evidence" from "N sources silently dropped."
- **PB-4 · Confidence disconnected from evidence FALSIFIED (CLAIM 4).** `compute_confidence`
  **double-counts** the same evidence (`helpers/confidence.py:64` `source_count*2` **and** `:74`
  `corroborating_sources*2`, where corroborating = `len(evidence_refs)` pointing at the same
  sources); `evidence_refs` are **hardcoded unverified strings** each worth +2 (`agent.py:2708+`);
  `retrieval_boost` adds up to **+10 from historical similarity** with no new current-incident
  evidence (`agent.py:2381`); the LLM may **override the score to any value** (`agent.py:2511`).

## 3. Scientific blockers
- **SB-1 · IQS double-counts (CLAIM 9 partial-falsified).** `evidence_efficiency` (0.10) and
  `unnecessary_evidence_avoided` (0.05) are exact complements (x and 1−x) of the same quantity —
  its own limitations string admits "complement of signal density" — so one signal carries 15%
  of the IQS twice. `gold_standard.py:298-339`.
- **SB-2 · ODE invents knowledge (CLAIM 11 partial-falsified).** `mine_topology`/`mine_operational`/
  `mine_temporal` pass `_support([1]*len(ids))` — no negative opportunities, so confidence is
  always 1.0 and contradictions always 0; combined with O(n²) pair enumeration and **no
  multiple-comparison correction**, ODE manufactures over-confident spurious correlations at
  scale. `ode/discovery.py:193,285,331`. (evidence/hypothesis/human miners are sound.)
- **SB-3 · Two incompatible "RCA correct" contracts.** `scientific_validation.rca_correct`
  (boolean, majority-keyword) vs `sentinelbench/scorer._root_cause_correctness` (float, hit
  fraction) disagree on the same input — the benchmark and the validator can label the same RCA
  differently.
- **SB-4 · Validation can validate a wrong RCA (CLAIM 8).** `validation_engine` status is a
  keyword/coverage proxy; a wrong RCA with high citation coverage can score "supports." Shadow-
  only today, but the claim as stated is false.

## 4. Architectural blockers
- **AB-1 · Value/authority inversion.** The authoritative RCA is fixed at `agent.py:2343`
  (`hypotheses.sort(-base_score); winner=[0]`) inside a ~511-line analyze phase. Around it sit
  **~2,460 LOC of shadow Tranche engines + ~4,655 LOC of produce-only eval apparatus** that
  never touch the answer. The "intelligence" is ~14× the size of the decision it cannot change.
- **AB-2 · Confidence logic split across 3 calibrators + hardcoded priors** (see dead code).

## 5. Dead subsystems (zero non-test importers → DELETE)
- `intelligence/confidence_calibrator.py` (216 LOC) — dead duplicate of `supervisor/confidence_calibrator.py`.
- `sentinel_core/continuous_learning/` (15 modules, ~1,211 LOC) — self-referential, test-only.
- `sentinel_core/eic/` (~443 LOC) and `sentinel_core/ode/` (~511 LOC) — imported by nothing.
Total ~2,381 LOC pure orphan.

## 6. Orphaned components
- The four dead subsystems above, plus: **4 competing quality composites** (IQS, DQS, PTI, EIC)
  none consumed by runtime; **3 ConfidenceCalibrator classes** (supervisor = live; intelligence
  + continuous_learning = orphan). Flags: ~10 permanently-ON `_ENABLED` flags never toggled.

## 7. Unnecessary subsystems
- Everything in §5. Additionally, the entire produce-only eval stack (`investigation_value/`,
  `eic/`, `ode/`, ~4,655 LOC) is offline analytics with **no proven value** on the current
  3-incident corpus — quarantine to an eval harness; do not ship in the product image.

## 8. Contradictions
- **C-1** Docs/certifications assert "deterministic + replayable"; source (PB-1/PB-2) contradicts.
- **C-2** `_get_change_time_window` comment says "anchor to incident time"; the fix now matches
  the comment (was a contradiction, resolved by B-1).
- **C-3** SB-3: two "RCA correct" definitions disagree.
- **C-4** "Evidence-grounded confidence" vs the hardcoded `evidence_refs` + double-count (PB-4).

## 9. Unsupported claims
CLAIM 1 (determinism), CLAIM 2 (replay), CLAIM 3 (no evidence loss), CLAIM 4 (confidence↔evidence),
CLAIM 6 (localization always correct — NOT MEASURED, keyword/substring-fragile, shadow-only),
CLAIM 8 (validation never validates wrong — false), CLAIM 9 (IQS quality — double-count),
CLAIM 11 (ODE no invented knowledge — spurious at scale). **Held from source:** CLAIM 5 (shadow
additive `_*` only), CLAIM 10 (EIC engine-independent — with minor gameable explainability +
shared matcher), CLAIM 12 (produce-only isolated — no runtime import), CLAIM 13 (Wave-3 needs
two gates + is audit-only even when fully on), CLAIM 14 (shadow-only never authoritative).

## 10. Recommendations to REMOVE
Delete `intelligence/confidence_calibrator.py`, `continuous_learning/`; quarantine `eic/`,
`ode/`, and `investigation_value/` (and the 5 shadow tranche engines) into an offline eval
package outside the product runtime image. Remove ~10 dead always-ON flags. Consolidate 9
`_tokens` / 5 `_sha16` / 3 `_jaccard` / 5 `_clamp` copies into one util each (most vanish with
the shadow deletions).

## 11. Recommendations to simplify
Collapse the 3 ConfidenceCalibrators to one; pick a single "RCA correct" contract; remove one
of the two complementary IQS metrics; decompose `_persist_results` (458 LOC) and `agent.py`
(3,799 LOC).

## 12. Recommendations to delay
Do not promote any capability to authority. Do not enable Wave 3. Do not publish IQS/EIC/ODE
numbers as validation until SB-1/SB-2 are fixed and a real labeled corpus exists.

## 13. Recommendations to reject
Reject any claim of "deterministic" or "replayable" or "evidence-grounded confidence" until
PB-1…PB-4 are fixed. Reject shipping the ~8.5k LOC shadow/orphan apparatus in the production
runtime image.

## 14. Confidence SentinelAI is genuinely production ready
**~30%.** Justification: strong test hygiene and *excellent* safety isolation (Wave-3 two-gate,
shadow additivity, produce-only isolation all held under adversarial source review) — but the
four properties that define the product ("deterministic, replayable, no evidence loss, evidence-
grounded confidence") are each falsified from source under the default configuration. A system
whose central pitch is auditable determinism cannot be called production-ready while its
determinism and replay are broken by its own default-ON learning stores.

## 15. Single highest-risk issue
**The learning stores that are ON by default (pattern registry, strategy evolver, experience/KG)
mutate persisted corpora that `_analyze_evidence` reads back — so the authoritative
investigation is non-deterministic and replay is non-hermetic across a growing corpus
(PB-1/PB-2).** This silently invalidates the platform's core auditability guarantee: the same
incident can be investigated twice with different conclusions, and a stored investigation cannot
be reproduced later — the exact failure an audit/compliance buyer would discover first. It is
the root of the two most damaging falsifications and must be fixed (freeze-corpus/replay-isolation
mode) before any determinism claim is made.
