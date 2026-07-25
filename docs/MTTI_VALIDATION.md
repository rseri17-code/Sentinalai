# SentinelAI — MTTI Validation Program

One question governs every future change: **does it measurably reduce operator
Mean Time To Identify (MTTI)?** This document instruments MTTI so the question
can be answered with evidence, audits the investigation/decision surface against
the six MTTI questions, and states `NOT_MEASURED` wherever a claim requires a
live operator that this environment does not have. Nothing is fabricated.

## MTTI, defined (the investigation is done when the operator can *act*)

| # | Operator must… | Milestone event | Segment |
|---|---|---|---|
| 1 | understand what failed | `investigation.started` | t0 |
| 2 | find decisive evidence | first `tool.responded` / `memory.result` | time_to_first_evidence |
| 3 | identify the fault / root cause | `rca.generated` (fallback `hypothesis.selected`) | time_to_root_cause |
| 4 | identify the owner | available at `rca.generated` | time_to_owner |
| 5 | know the next action | available at `rca.generated` | time_to_recommendation |
| 6 | act | `investigation.completed` | total |

## Phase 4 — MTTI instrumentation (BUILT)

`agui/mtti.py` + `GET /api/v1/investigations/{id}/mtti` + `GET /api/v1/mtti/summary`
compute the timeline **from the investigation's own recorded event timestamps**
(`AGUIEvent.timestamp_epoch_ms`). It adds no clock to the deterministic core —
it reuses existing runtime telemetry — and it fabricates nothing: a milestone
that was never emitted stays `null` and its segment is omitted. The React
investigation view gains an **MTTI panel** (`MttiTimeline`) showing time to
first evidence / root cause / owner / recommendation / actionable. This is the
foundation every later MTTI claim depends on; before it, MTTI was not measured
at all.

## Phase 1 — Investigation quality audit (evidence-cited)

| Dimension | Status | Evidence |
|---|---|---|
| Determinism | **Demonstrated** | byte-identical recompute; full regression green |
| Root-cause precision | **Partially** | gold IQS 0.818 but **n=3, underpowered** (`eval/gold_standard/evaluation.json`) |
| Evidence completeness/relevance | **Instrumented** | R2 `_evidence_lifecycle` used/unavailable; not yet scored at scale |
| Confidence calibration | **Partially** | 0.78 @ n=3, underpowered |
| Owner accuracy | **NOT_MEASURED** | owner derives from incident metadata; no labelled owner set |
| Recommendation quality | **NOT_MEASURED** | requires operator acceptance data |
| False positives / negatives | **NOT_MEASURED** | needs a powered labelled corpus |

The dominant weakness is sample size: correctness metrics cannot be trusted
below `n≥30`. This is a corpus/pilot problem, not a code defect.

## Phase 3 — Decision-acceleration audit (does each surface answer the 6 questions?)

Wired surfaces (post-convergence), against *what / why / evidence / confidence /
owner / next*:

| Surface | what | why | evidence | confidence | owner | next |
|---|---|---|---|---|---|---|
| Investigation view (timeline/graph/evidence) | ✓ | ✓ | ✓ | ✓ (RiskConfidence) | partial | partial |
| Operational Health (wired) | ✓ | ✓ | ✓ (counts) | ✓ | ✓ | ✓ (next_action) |
| MTTI panel (new) | — | — | — | — | — | timing only |

Gap: owner + next-action are strongest in Operational Health but thin on the
raw investigation view. Closing that is a real MTTI lever (fewer context
switches) — logged, not yet built.

## Phase 5 — External-tool escape analysis

**NOT_MEASURED.** Detecting when an operator leaves for Splunk/Dynatrace/
ServiceNow/GitHub requires observed operator behavior. The instrumentation hook
exists (`pilot_telemetry` `operator_interaction`), but no sessions have been
recorded. No escape data is invented.

## Phase 6 — Pilot metrics

**NOT_MEASURED.** Per-investigation baseline-vs-SentinelAI MTTI, recommendation
acceptance, operator confidence/trust (1–5), and "would use again" all require a
controlled pilot with real operators. The capture schema is defined (OVP Phase 1
§5 + `pilot_telemetry`); `mtti/summary` deliberately returns
`baseline_comparison: NOT_MEASURED` because no control arm exists.

## Phase 7 — ROI

The MTTI instrumentation does not itself reduce MTTI — it **makes reduction
measurable**, which is the precondition for every ROI claim. Concrete
seconds-saved / clicks-removed figures are **NOT_MEASURED** without a baseline;
the instrumentation is exactly what will produce them in a pilot.

## Phase 8 — Continuous improvement

Bottleneck / trust / workflow / ROI top-10s require observed operator evidence →
**NOT_MEASURED** today. The one evidence-backed item now: run the pilot and
capture MTTI segments + telemetry (the instrumentation and the OIP surface are
in place to do so).

## Bottom line

MTTI is now instrumented end to end from real investigation events, and the
decision surface is audited against the six questions. Whether SentinelAI
*reduces* MTTI remains **NOT_MEASURED** — it depends on a controlled pilot, the
only thing this environment cannot supply. From here, the merge rule holds:
a change ships only if it moves a measured MTTI segment.
