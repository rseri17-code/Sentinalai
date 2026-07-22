# SentinelAI — Operational Improvement Engine

Closes the loop: **measure → understand → prioritize → improve → measure again.**
Consumes the telemetry already collected (operator timelines, external-tool
escapes, decision quality, workflow interactions) and produces an ROI-ranked,
evidence-backed improvement backlog aimed only at reducing operator MTTI. It
modifies no engine, no replay, no telemetry — pure additive analysis — and it
**invents nothing**: every backlog item traces to an observed signal, and with
insufficient pilot data the whole report is `NOT_MEASURED`.

## Executive Improvement Report (structure)

`GET /api/v1/improvement-report` → `agui/improvement_engine.analyze()` yields:

| Section | Source signal |
|---|---|
| Operator segment medians | per-session `compute_operator_mtti` |
| Top external-tool escapes | `external_tool_escapes` (count · time away · reasons) |
| Recommendation trust | `decision_quality` acceptance rate |
| Repeated evidence lookups | count of re-opened evidence per session |
| Bottlenecks (classified) | see root-cause table |
| ROI-ranked backlog | seconds-saveable = observed frequency × observed time |

## Root cause of friction (Phase 3) — each rule is signal-bound

| Observed signal | Root-cause class | Improvement | Effort |
|---|---|---|---|
| external_tool_opened (logs/metrics) | missing_evidence | surface the evidence in-product | medium |
| repeated evidence re-opens | poor_navigation | reduce clicks for this step | low |
| slow time_to_understanding vs confidence | missing_context | add context to the RCA view | medium |
| low recommendation acceptance (<0.5) | missing_recommendation | improve recommendation trust | high |

Effort is a **declared** per-improvement tier, reported alongside impact — it is
never multiplied into the seconds-saveable figure. Impact comes only from
measured time.

## ROI prioritization (Phase 4)

`seconds_saveable = observed_frequency × observed_time_cost` (e.g. an escape to
Splunk 6× at 40 s each ⇒ 240 s saveable). The backlog is ranked by this measured
value, descending. Trust-type friction (low acceptance) carries `null` seconds —
it is real evidence but not a time delta, and is not faked into one.

## Improvement tracking (Phase 5)

`compare_before_after(before, after)` diffs the two reports' operator-segment
medians. If nothing measurably dropped → **`NO_IMPACT`** (never a soft pass).

## Validation

- Investigation engine, replay, evidence, confidence, determinism, and the
  existing telemetry are **unchanged** — the engine only reads recorded events.
- Tests: `tests/agui/test_improvement_engine.py` (10) — NOT_MEASURED paths,
  friction detection, root-cause classification, ROI ranking by observed
  seconds, evidence-on-every-item guard, effort-not-multiplied guard, and
  before/after IMPROVED vs NO_IMPACT.
- Full regression: see the commit message.

## Current output on real data

**`NOT_MEASURED`.** The operator-events log is empty — no pilot has run — so the
live `improvement-report` returns `status: NOT_MEASURED` with the session count.
The analysis logic is proven with synthetic test fixtures (test data, clearly
labelled), but **no friction finding, bottleneck, or backlog item is presented
as real** until operator sessions exist.

## Remaining gaps

Everything downstream of real operator data: actual bottlenecks, escape reasons,
acceptance trends, and measured before/after ROI. All `NOT_MEASURED` today. The
capability to turn pilot observations into a prioritized, evidence-backed backlog
is built; the observations require a pilot.

## Final rule enforced

The engine cannot recommend work that "seems useful" — it can only surface work
that an observed operator signal supports. No signal, no item. That is the
guardrail that keeps the improvement loop honest.
