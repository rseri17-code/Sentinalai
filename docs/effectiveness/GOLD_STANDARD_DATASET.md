# Gold Standard Investigation Dataset + Investigation Quality Score (IQS)
**The authoritative benchmark SentinelAI must beat before any capability promotion.**
Engine: `sentinel_core/investigation_value/gold_standard.py` (produce-only, offline,
deterministic, replayable, removable; composes existing outputs; no runtime/authority/
Wave-3/retrieval/reasoning change).

## Purpose
Every completed investigation becomes an immutable benchmark artifact that supports
deterministic, reproducible comparison across four vantage points:
**authoritative runtime · Investigation Intelligence shadow stack · human investigator ·
validated postmortem.** The dataset is the bar every future promotion must clear.

## The Gold Standard Investigation Record (`gold_record`)
An immutable artifact (deterministic sha256 `record_id`) capturing, composed from existing
outputs plus optional human/postmortem ground truth:
timeline & hypotheses · eliminated hypotheses (+reasons) · evidence collected & the
acquisition **sequence** · decisive evidence & importance ranking · localization ·
counterfactual (+residual) · **confidence evolution** (raw → calibrated → evidence-derived)
· verification status · investigation completeness · next-best evidence · operator block
(interventions, corrections, agreement) · remediation · **validated RCA** · **postmortem
outcome** · replay hash. No wall-clock is read; the incident timestamp is the only time
source.

## Deterministic quality metrics (`record_metrics` → `evaluate_dataset`)
Each is computed deterministically, reported with sample size + bootstrap CI (caller-seeded)
+ limitations, and is `NOT_MEASURED` where ground truth is absent.

| Metric | Definition (higher = better) | Needs labels |
|---|---|---|
| hypothesis_efficiency | 1 / hypotheses considered to a confirmed winner | no |
| evidence_efficiency | signal density: useful ÷ collected evidence | no |
| unnecessary_evidence_avoided | 1 − (unattached evidence ÷ collected) | no |
| decisive_evidence_latency | 1 − normalized position of first decisive item | no (needs true order) |
| false_lead_avoidance | 1 − (hypotheses carrying refuting evidence ÷ total) | no |
| localization_accuracy | localized service matches validated cause (substring-safe for short names) | yes |
| confidence_calibration | 1 − \|evidence_confidence − correctness\| | yes |
| investigation_completeness | evidence-category coverage (T4) | no |
| operator_agreement | authoritative RCA vs operator-validated RCA | yes (human) |
| replay_fidelity | a stable replay hash is present | no |

## Investigation Quality Score (IQS)
A single score composed **only from validated (measured) metrics**, with fixed weights
renormalized over the measured subset, and always reported with **coverage**:
- `investigation_quality_score` ∈ [0,1]
- `iqs_coverage` = measured metrics ÷ 10
- **A high IQS at low coverage is not a pass** — both travel together, exactly as the
  Production Trust Index methodology requires.

## Baseline run (current corpus, `eval/gold_standard/evaluation.json`)
With postmortem + human labels + evidence sequence supplied for the 3 ground-truth
incidents:
- **IQS = 0.818 at coverage 1.0** (all 10 metrics measured).
- This is a *harness demonstration*, not a production benchmark — n=3, and the shadow
  outputs are modeled. It proves the evaluator computes every metric deterministically
  end-to-end and yields a single comparable score.

## How this becomes the promotion gate
1. The shadow pilot emits a `gold_record` per real investigation (with operator label +
   postmortem when resolved) into `eval/gold_standard/dataset.json` (append-only).
2. `evaluate_dataset` recomputes the IQS + per-metric CIs every reporting period.
3. **Before any capability promotion**, the candidate (e.g. validation gating, per the
   Decision Boundary Analysis) is A/B-run and scored on the *same held-out gold records*;
   promotion requires a measured IQS (or targeted-metric) improvement with adequate power —
   not architecture, not opinion.

## Guarantees
Additive, deterministic (double-run byte-identical), replayable (deterministic ids +
seeded bootstrap), removable (delete the module + tests + `eval/gold_standard/`), and fully
regression-tested. No runtime path, authority, Wave 3, retrieval, or reasoning was touched.

## Limitations
The IQS measures investigation *quality signals*, several of which require labeled ground
truth (localization, calibration, operator agreement) that only the pilot can supply at
scale. Below n=30 every metric is flagged `underpowered`. The score is a comparison
instrument, not a claim of correctness — it earns meaning only on a powered, leakage-free,
human-labeled corpus.
