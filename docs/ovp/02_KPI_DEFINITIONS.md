# OVP — KPI Definitions
Each KPI: calculation · required telemetry · baseline · target · confidence interval · cadence.
Every KPI is computed by an **existing** engine (no new instrumentation). All continuous KPIs
carry a bootstrap CI (`scientific_validation.bootstrap_ci`); comparisons use McNemar/CIs.

| # | KPI | Calculation | Telemetry (source) | Baseline | Target | CI | Cadence |
|---|---|---|---|---|---|---|---|
| 1 | **MTTI** | median(receipt.elapsed_ms) per incident | phase receipts (R1) | human MTTI per class | −25% | bootstrap 95% | weekly |
| 2 | **MTTR** | postmortem resolution_time_ms | postmortem label | current MTTR | no regression | 95% | monthly |
| 3 | **Root-cause precision** | correct / labeled (`rca_correct` vs postmortem keywords) | postmortem + result.root_cause | human first-pass precision | +≥5pts | McNemar p<0.05 | weekly |
| 4 | **First-investigation correctness** | correct on run #1 / labeled | result + postmortem | human first-pass | ≥ human | 95% | weekly |
| 5 | **Evidence completeness** | `_investigation_validation.investigation_completeness` (T4) + `_evidence_lifecycle.counts.used / received` | analyze result (R2) | n/a (new signal) | ≥ 0.80 | 95% | weekly |
| 6 | **Investigation reproducibility** | replay byte-identical AND `corpus_version` stable / total | replay (R1) + corpus_stamp | n/a | 100% | exact | weekly |
| 7 | **Operator trust** | mean human-eval trust score (rubric) | human review (04) | n/a | ≥ 4.0/5 | 95% | weekly |
| 8 | **False-positive rate** | verified-incorrect where verification said proves/supports / total | `scientific_validation.e1` | human false-positive rate | ≤ human | 95% | weekly |
| 9 | **Repeat-incident reduction** | recurring incident classes closed after an ODE discovery was actioned | `ode` recurrence + recurrence_index | trailing repeat rate | −≥10% | 95% | monthly |
| 10 | **Investigation coverage** | incidents investigated / total incidents, per class | per_class_effectiveness | 0 (pre-pilot) | ≥ 90% of sev1–3 | — | weekly |
| 11 | **Confidence calibration** | \|evidence_confidence − correctness\| MAE + calibration bins | `scientific_validation` + `_confidence_provenance` | raw-confidence MAE | evidence < raw; bin err < 0.15 | 95% (n≥30) | monthly |
| 12 | **Operational knowledge growth** | ODE `knowledge_count` of significant discoveries this period | `ode.run_discovery` | 0 | net-positive, strengthened > weakened | — | monthly |
| 13 | **Time saved per incident** | human MTTI − SentinelAI MTTI (paired) | receipts + human timing | 0 | > 0, CI excludes 0 | 95% | weekly |
| 14 | **Manual effort avoided** | evidence SentinelAI surfaced that the operator would have collected (decisive+used) / total human evidence steps | `_evidence_lifecycle` + T5 decisive + human log | 0 | measurable > 0 | 95% | monthly |

## Reporting discipline (applies to every KPI)
- **Never estimate.** Absent ground truth → `NOT_MEASURED` (verbatim), never imputed.
- **Sample size + power.** Every figure shows `n`; below the class power floor (30) it is
  flagged `underpowered` and excluded from any promotion decision.
- **Coverage travels with scores.** A composite (IQS) is always reported with its coverage; a
  high score at low coverage is not a pass.
- **Determinism.** All KPI computations are deterministic and replayable (they reuse the
  produce-only engines), so a KPI report can itself be reproduced byte-for-byte.

## Primary vs secondary
- **Primary (drive Go/No-Go):** #1 MTTI, #3 root-cause precision, #5 evidence completeness,
  #6 reproducibility, #8 false-positive rate, #7 operator trust.
- **Secondary (trend/context):** the rest — informative, not gating.
