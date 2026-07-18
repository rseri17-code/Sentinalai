# Shadow Pilot Runbook + Operator Guide
**Deliverables 11, 12**

## Deliverable 11 — Shadow Pilot Runbook

### Preconditions (verified before day 0)
- HEAD at the production-closure commit; regression clean (5682+ passing).
- All four blockers closed (B-1/B-2/B-3, F-obs); determinism + replay certified.
- Runtime authority OFF, Wave 3 OFF, runtime retrieval OFF. **These stay OFF for all 90 days.**

### Daily operation (offline, produce-only)
1. For each completed investigation, build an `observation_record` (result + incident
   metadata + repo commit + model + period bucket + replay hash).
2. Append the record to the append-only observation store (JSONL).
3. Operators label resolved incidents per the Labeling Framework (Deliverable 3).

### Weekly operation
1. `quality_scorecard(week_observations, period="YYYY-Www")`.
2. `chaos_observation(week_observations)` for dependency health.
3. `regression_watch(last_week_scorecard, this_week_scorecard)`.
4. `production_scorecard(scorecard, GateInputs(...))` — compute `(PTI, coverage)` + gates.
5. Emit the Weekly Certification Report + Executive Report.

### Monthly operation
1. `longitudinal_trends([weekly_scorecards])` for the month.
2. `bucket_by` per class/service/severity/model/commit → per-dimension scorecards.
3. Re-evaluate G1–G11 with the month's accumulated `GateInputs`.

### Incident response (pilot-level, not runtime)
- **Determinism REVIEW** → halt scoring, investigate the non-determinism, do not advance gates.
- **Regression (confidence=high)** → follow `recommended_action`; do not recommend Wave 3.
- **Dependency outage** (source_availability drop) → escalate to the dependency owner;
  investigations continue degraded (F-obs makes this visible).

### Rollback
Delete `shadow_pilot.py` + its tests; discard the observation store. Zero runtime impact.

## Deliverable 12 — Operator Guide

### What SentinelAI gives you (read-only)
For every investigation: a root cause + confidence, plus the shadow reasoning — why this
cause won (T5 arbitration), why others lost, decisive evidence, decision stability,
counterfactual, causal localization, and **which sources were unavailable** (F-obs).

### How to read a shadow investigation
1. **Root cause + verification status** — `proves/supports` means evidence-backed;
   `suggests/insufficient/contradicts` means treat with caution.
2. **Sources unavailable** — if present, the investigation ran degraded; low confidence may
   be a data-gap, not a wrong answer.
3. **Decision stability** — `stable=false` means the conclusion hinged on one fact.
4. **Explainability** — read "why others lost" before trusting the winner.

### What to label
After you resolve the incident, record: was the root cause CORRECT / PARTIAL / INCORRECT,
the true cause, what actually fixed it, resolution time, and any evidence SentinelAI missed.
Your label is the evidence that decides whether SentinelAI earns production authority.

### What NOT to expect during the pilot
SentinelAI takes **no action** and has **no authority**. It observes and explains. It will
not remediate, will not write to ITSM, and will not retrieve memory at runtime. If it is
wrong, nothing breaks — label it INCORRECT and that improves the certification evidence.
