# Operational Validation Program (OVP) — Validation Plan
**Phase 2 · Enterprise Validation. Measure external outcomes, don't build internals.**
Runtime contracts assumed complete: R1 (determinism/replay, `abe7fb7`) + R2 (evidence-grounded
confidence + observability, `edc11e8`).

> Central question: **does SentinelAI make enterprise operations materially better?**
> This plan answers it with measured outcomes, using only existing capabilities.

## Principle
No new reasoning engines, intelligence modules, evaluation frameworks, or replay systems.
Every KPI below is computed by an engine that already exists:
| Engine (existing) | Supplies |
|---|---|
| Phase receipts + `_evidence_lifecycle` (R2) | MTTI, evidence used/filtered/unavailable |
| `corpus_stamp` + replay (R1) | reproducibility / replay-agreement |
| `_confidence_provenance` (R2) | confidence attribution |
| `sentinel_core/investigation_value/scientific_validation` | RCA correctness, calibration, McNemar/bootstrap CIs, per-class |
| `investigation_value/gold_standard` (IQS) | investigation-quality composite |
| `investigation_value/shadow_pilot` | rolling quality scorecard, safety agreement |
| `investigation_value/investigation_effectiveness` | benefit attribution vs authoritative |
| `sentinel_core/ode` | knowledge growth, recurring failure modes / false leads |

## Scope of validation
Read-only shadow pilot only. SentinelAI runs alongside the human investigation; it takes no
action, holds no authority, and does not gate incident response. Wave 3 stays OFF.

## Success (program level)
The program succeeds when SentinelAI can show, on a powered, leakage-controlled corpus, a
**statistically significant improvement** on at least the primary KPIs (MTTI, root-cause
precision, evidence completeness) **without regression** on safety KPIs (false-positive rate,
verified-incorrect rate), and an **operator-trust** score above the go/no-go threshold.

## Structure
1. **KPI definitions** (`02_KPI_DEFINITIONS.md`) — calculation, telemetry, baseline, target, CI, cadence.
2. **Scorecards + templates** (`03_SCORECARDS_AND_TEMPLATES.md`) — enterprise + weekly/monthly.
3. **Human evaluation rubric** (`04_HUMAN_EVALUATION_RUBRIC.md`).
4. **Product readiness checklist** (`05_PRODUCT_READINESS_CHECKLIST.md`).
5. **Pilot execution + Go/No-Go** (this doc, below).

## Pilot execution plan
- **Incident selection.** All production incidents of severity 1–3 in the pilot services,
  stratified by incident class (kubernetes/database/deployment/authentication/dns/…) so no
  class dominates. Exclude drills/synthetic. Target ≥ 500 labeled, ≥ 20 per class (the
  readiness-gate floor).
- **Duration.** 90 days rolling (the corpus-accumulation horizon; interim reads weekly).
- **Comparison methodology (paired).** For each incident, capture the human (authoritative)
  investigation and SentinelAI's shadow investigation over the **same** telemetry window.
  Compare RCA correctness, localization, MTTI, evidence completeness against the **validated
  postmortem** as ground truth. Replay each investigation to confirm reproducibility.
- **Bias controls.** (a) Ground truth = postmortem, not the SentinelAI output; (b) held-out
  partition never used to tune anything; (c) incidents used during tranche development are
  flagged and excluded; (d) operators label blind to SentinelAI's confidence; (e) leakage
  check: dev fixtures must not appear in the eval set.
- **Statistical significance.** McNemar on paired RCA correctness, bootstrap CIs on continuous
  KPIs (both already implemented in `scientific_validation`); report `underpowered` below n=30
  per class; no claim without a CI.
- **Rollback criteria.** The pilot is read-only, so "rollback" = turn the shadow flags OFF
  (instant, byte-identical restore). Halt the pilot immediately if: a determinism/replay
  regression appears (`corpus_version` mismatch on re-run), safety agreement drops < 0.95, or
  any verified-incorrect RCA is attributable to a shadow signal that reached an operator.

## Go/No-Go criteria (for wider deployment — still read-only, broader footprint)
**GO** only if ALL hold on the held-out, powered corpus:
1. Root-cause precision improvement over the human baseline with a CI excluding 0, per class.
2. MTTI reduction (or time-saved) statistically significant, no MTTR regression.
3. Evidence completeness ≥ target; zero silent evidence loss (`_evidence_lifecycle` shows no
   `unknown`).
4. Reproducibility 100% (replay byte-identical; `corpus_version` stable).
5. Confidence calibration within threshold (n ≥ 30).
6. Operator-trust score ≥ 4.0/5 with false-positive rate ≤ target.
7. Readiness gates G1–G11 pass (`investigation_value.readiness`).
8. Human sign-off recorded.
**NO-GO / EXTEND** if any is unmet or `NOT_MEASURED` — continue the shadow pilot; never infer.
**Authority / Wave 3 remain OUT OF SCOPE** and are governed by the separate readiness program.
