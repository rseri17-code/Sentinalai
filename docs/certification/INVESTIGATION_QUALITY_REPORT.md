# INVESTIGATION_QUALITY_REPORT.md
**SentinelAI — Production Readiness Certification · Phase 5**
Every quality dimension measured, not asserted. Where ground truth is absent → NOT MEASURED.

## Quality dimensions & their measurable instruments
Each dimension maps to a concrete, deterministic signal already produced by the pipeline.

| Dimension | Instrument (producer) | Measurable today? |
|---|---|---|
| Root-cause precision | `rca_correct()` keyword-majority vs `eval/ground_truth.json` | PARTIAL — N=3 labeled |
| Evidence quality | `evidence_validation_score` (T4) = 0.6·coverage + 0.4·balance | YES (per-investigation) |
| Citation quality / grounding | `citation_coverage`, `hallucination_risk` (annotate_citations), `_grounding` | YES |
| Localization | `_causal_investigation.localization.{root_cause,immediate_cause,symptom}_service` (T3) | YES |
| Decision stability | `_decision_intelligence.decision_stability.{stable,stability_score}` (T5) | YES |
| Counterfactual quality | `_investigation_validation.counterfactual.counterfactual_residual_score` (T4) | YES |
| Hypothesis quality | `_hypothesis_graph` + `_elimination_narrative.survived_disconfirmation` (T1) | YES |
| False-lead handling | T1 refutation + T3 topology-victim rejection + T5 `rejected_with_proof` | YES |
| Completeness | `investigation_completeness_score` over 6 evidence categories (T4) | YES |
| Explainability | `_decision_intelligence.explainability{why_won,why_others_lost,...}` (T5) | YES |
| Confidence calibration | `evidence_confidence` vs correctness MAE (Scientific Validation) | PARTIAL — underpowered N=3 |

## Measured results (labeled corpus, N=3)
From `eval/scientific_validation/report.json`:
- **Root-cause precision:** 3/3 correct on the modeled records (keyword proxy — see
  SCIENTIFIC_VALIDATION_AUDIT F-2).
- **Safety (shadow ↔ authoritative agreement):** **1.0** — the independent re-derivation
  agreed with the authoritative RCA on every case. No disagreement risk observed.
- **Verified-incorrect:** 0. **Failures:** 0.
- **Confidence calibration:** evidence-MAE 22.0 vs raw-MAE 20.0 — **underpowered, not
  citable** at N=3 (see audit F-4).
- **Per-class:** database / kubernetes / authentication each N=1 — no class near the
  20-sample sufficiency floor.

## Determinism of the quality signals
All quality instruments are deterministic (verified: 1000 repeats → 1 hash,
DETERMINISM_REPORT E-1). A given investigation always yields the same quality vector —
so quality trends over time are attributable to inputs, not measurement noise.

## Honest gaps (NOT MEASURED)
- **Root-cause precision at scale** — needs ≥500 labeled, human-adjudicated incidents.
- **Calibration** — needs N ≥ 30 with varied correct/incorrect labels.
- **False-negative rate (missed true cause)** — requires labeled incidents where the
  true cause was outside the generated hypothesis set; the 3-incident corpus cannot
  exercise this.
- **Explainability usefulness** — inherently requires human judgement (read-only pilot).

## Verdict
The pipeline **produces a complete, deterministic quality vector per investigation** —
every dimension the mission lists has a concrete instrument. The instruments are
**certified as deterministic and additive**; the **scores are not yet statistically
meaningful** because the labeled corpus is 3 incidents. Quality *measurement* is
production-ready; quality *evidence* is not. This is the binding constraint the
Longitudinal Shadow Plan is designed to remove.
