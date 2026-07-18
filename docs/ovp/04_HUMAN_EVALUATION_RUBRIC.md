# OVP — Human Evaluation Rubric
Structured operator review of each shadow investigation. Read-only; the operator resolves the
incident normally, then reviews SentinelAI's investigation. Scores feed KPI #7 (operator
trust) and the human-labeled ground truth.

## Review protocol
1. Operator resolves the incident and records the validated root cause + remediation
   (this becomes ground truth, independent of SentinelAI's output).
2. Operator opens SentinelAI's shadow investigation (root cause, confidence + provenance,
   evidence lifecycle, hypotheses/elimination, localization, counterfactual).
3. Operator scores the six dimensions below (1–5) **blind to SentinelAI's confidence value**
   for the trust/usefulness scores, then records the correctness verdict.

## Scoring dimensions (1 = poor, 5 = excellent)
| Dimension | 1 | 3 | 5 |
|---|---|---|---|
| **Usefulness** | added nothing | confirmed what I knew | surfaced something I'd have missed |
| **Clarity** | confusing | followable | immediately understandable |
| **Trust** | would not rely | would sanity-check | would act on it |
| **Actionability** | no next step | vague direction | concrete, correct next action |
| **Explanation quality** | assertion only | some evidence | evidence-grounded, decisive evidence named |
| **Investigation completeness** | major gaps | mostly covered | nothing important missing |

## Correctness verdict (ground-truth label)
`ROOT_CAUSE_CORRECT` · `ROOT_CAUSE_PARTIAL` · `ROOT_CAUSE_INCORRECT` · `UNKNOWN`
(feeds `shadow_pilot` / `scientific_validation`). Plus: false_positive (flagged a cause that
wasn't real), false_negative (missed the real cause), missing_evidence (list).

## What operators check against R1/R2 signals
- **Confidence provenance** (`_confidence_provenance`): is the confidence justified by the
  evidence line items, or inflated? Flag any mismatch.
- **Evidence lifecycle** (`_evidence_lifecycle`): were any sources `unavailable`/`error`? If
  so, was the low confidence appropriately a data-gap rather than a wrong answer?
- **Reproducibility**: (spot check) does replay of this investigation reproduce it? (should).

## Aggregation
- Operator-trust KPI = mean(trust) with 95% CI; report per class.
- Inter-operator agreement (≥2 reviewers on a sample): report dispersion; high dispersion
  flags an ambiguous incident (candidate for exclusion or `unknown` difficulty).
- A dimension mean < 3.0 in any class is a **red flag** surfaced in the weekly report.

## Cadence & sampling
Every labeled incident gets a correctness verdict; a stratified sample (≥ 30/class over the
pilot) gets the full six-dimension review to bound reviewer effort while retaining power.

## Bias controls
Reviewers score before seeing SentinelAI's numeric confidence; ground truth comes from the
postmortem, not the review; incidents used in tranche development are excluded from the
labeled eval set.
