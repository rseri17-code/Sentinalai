# Scorecards, Trend & Regression Engines
**Deliverables 4, 5, 7, 8, 10 · Compute-only, sample-size-aware**

## Deliverable 4 — Continuous Quality Scorecard (`quality_scorecard`)
Every metric is **computed, never estimated**, and self-describes with sample size + CI
(Phase-10 discipline). Absent ground truth ⇒ `NOT_MEASURED`.

| Metric | Source | Requires labels |
|---|---|---|
| rca_accuracy | operator verdict == CORRECT | yes |
| calibration_error | \|evidence_confidence − correct\| | yes |
| shadow_authoritative_agreement | T5 winner ∩ root_cause tokens | no |
| evidence_validation_score | T4 | no |
| citation_coverage | annotate_citations | no |
| investigation_completeness | T4 | no |
| decision_quality | T5 | no |
| expert_concordance | T4 | no |
| decision_stability | T5 stable rate | no |
| degraded_rate / worker_failure_rate / source_availability | F-obs | no |

Each numeric metric → `{value(mean), n, ci95, underpowered(n<30), period, limitations}`.
Each rate → `{value, n, underpowered, period}`. `determinism: PASS|REVIEW`.

## Deliverable 5 — Production Scorecard (`production_scorecard`)
Wraps the quality scorecard with the **readiness gates (the sole authority)** and a
coverage-aware Production Trust Index:
- `production_trust_index` = mean of the *measured* PTI dimensions (8 named dims).
- `pti_coverage` = measured ÷ total. **A high PTI at low coverage is explicitly NOT a
  pass** — both numbers travel together.
- `gatekeeper_verdict` ∈ {ALL_GATES_PASS, GATES_FAILING}; `wave3_recommendation` ∈
  {READY, NOT_READY} — derived *only* from `evaluate_gates(...).all_passed`.

## Deliverable 7 — Longitudinal Trend Engine (`longitudinal_trends`, `bucket_by`)
Trends each metric across an ordered list of period scorecards using
`effectiveness._trend` (slope + direction + verdict, dead-band). **Trends only from
measured values; no smoothing that hides regressions** — a period with `NOT_MEASURED` is
skipped, not interpolated. `bucket_by` groups observations by incident_class / service /
severity / model / commit / period for per-dimension scorecards (Phase 4 dimensions).

## Deliverable 8 — Regression Watch Engine (`regression_watch`)
Compares a baseline scorecard to a current one. Each metric has a desired direction and a
minimum adverse delta; a breach emits a regression carrying **reason · first_occurrence ·
last_occurrence · affected_investigations · confidence (high if n≥30 else low_sample) ·
recommended_action**. Determinism/replay regressions are categorical (any drop from PASS
is flagged with `halt pilot`).

## Deliverable 10 — Production Trust Index Methodology (v3)
Same rule as certification v1/v2: **score measured dimensions only; report coverage
separately**. The shadow pilot recomputes `(PTI, coverage)` every reporting period from the
production scorecard. The methodology's honesty guarantee: coverage rises only as the
labeled corpus and measured dimensions grow — the pilot cannot "talk its way" to a high
score with a small sample because underpowered metrics are flagged and gate promotion is
delegated entirely to G1–G11.
