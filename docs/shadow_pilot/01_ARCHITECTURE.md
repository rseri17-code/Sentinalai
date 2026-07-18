# 90-Day Shadow Pilot — Architecture
**Deliverable 1 · Produce-only operational evidence collection**

## Purpose
Turn SentinelAI into a continuously self-certifying production *candidate*. Every
completed investigation contributes measurable evidence toward the question
"am I becoming more trustworthy?" — answered from data, never opinion.

## Non-negotiable scope
This is **observation, not capability**. It adds no intelligence, reasoning, agents,
retrieval, or Wave 3; it changes no runtime path (AnalyzePhase, planner, workers, memory,
replay, admission, validation, decision intelligence). It is **additive, deterministic,
replayable, and fully removable**.

## Where it lives
`sentinel_core/investigation_value/shadow_pilot.py` — a pure, offline, produce-only module
imported by **no runtime path** (only by tests and offline reporting). It composes the
outputs that already exist on completed investigations.

## Data flow (all offline, post-investigation)
```
completed investigation result (root_cause, confidence, _investigation_validation,
  _decision_intelligence, _hypothesis_graph, _causal_investigation,
  _sources_unavailable, citation_coverage, degraded_investigation)
        │  + incident metadata (immutable timestamps only)
        ▼
observation_record()  ──► immutable ObservationRecord  (Phase 1)
        │                    determinism_hash, replay_hash, deterministic record_id
        │                    optional operator label (Phase 2)
        ▼
quality_scorecard(observations, period)            (Phase 3)  ── rolling metrics
        │
        ├─ longitudinal_trends([scorecards])        (Phase 4)  ── daily/weekly/monthly + dims
        ├─ regression_watch(baseline, current)      (Phase 5)  ── reason/first/last/action
        ├─ chaos_observation(observations)          (Phase 8)  ── dependency degradation
        ▼
production_scorecard(scorecard, GateInputs)         (Phase 6+7)
        └─ evaluate_gates() ── the SOLE gatekeeper (G1–G11)  ── Wave-3 recommendation
```

## Reused machinery (no duplication)
- `scientific_validation`: `rca_correct`, `bootstrap_ci`, `NOT_MEASURED`, canonical hashing.
- `effectiveness._trend`: slope + direction + verdict with a dead-band.
- `readiness.GateInputs` / `evaluate_gates`: the G1–G11 gate engine — the only authority
  that can recommend Wave 3.

## Determinism guarantees
No clock, no randomness (bootstrap is caller-seeded), sorted iteration, byte-stable JSON,
deterministic sha256 record ids. Observation fields derive from **incident timestamps
only** — never wall-clock — consistent with blocker B-2. The `observed_period` bucket key
is caller-supplied, not read from a clock.

## Removability
Delete `shadow_pilot.py` + its test file: nothing else imports them. Zero runtime impact.

## What it deliberately does NOT do
- It does not label investigations (Phase 2 is a design; operators supply labels).
- It does not grant authority, enable Wave 3, or change confidence/root_cause.
- It does not estimate any metric it cannot compute — absent ground truth ⇒ `NOT_MEASURED`.
