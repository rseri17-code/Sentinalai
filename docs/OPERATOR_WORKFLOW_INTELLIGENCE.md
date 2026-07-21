# SentinelAI — Operator Workflow Intelligence

The engine timeline (`agui/mtti.py`) measures **how long the system needed**.
This adds the **operator timeline** — how long the *human* needed — so the gap
between the two can be measured. Additive, backward compatible, reuses the
existing `pilot_telemetry` recorder (no new framework), never synthesizes
interactions or timestamps.

## 1. Executive Summary

SentinelAI can now measure both numbers separately (Phase 6): system MTTI from
engine events, and operator MTTI from recorded operator interactions. Operator
telemetry, external-tool escape analysis, and decision-quality capture are wired
end to end (UI emits → BFF records append-only → endpoint computes). Whether the
operator is actually *faster* remains **NOT_MEASURED** — that needs real
operators; none exist in this environment, and none are fabricated.

## 2. Operator Journey Map

```
alert → notification → investigation_opened → evidence_panel_opened
      → evidence_item_expanded → graph_opened → graph_node_selected
      → confidence_viewed → owner_viewed → recommendation_viewed
      → (external_tool_opened … return) → recommendation_accepted/rejected
      → next_action_started → investigation_completed
```

Each arrow is a measured transition (epoch-ms deltas from `investigation_opened`).

## 3. Operator Event Model

`agui/operator_telemetry.py` — 15 operator milestones (`OPERATOR_MILESTONES`).
Each event carries: `at_ms` (real interaction time), `investigation_id`,
`operator`, `service`, `application`, `entity`, `screen`, `duration_ms`, and for
escapes `tool_name` / `reason` / `time_away_ms`. Events are recorded as
`pilot_telemetry` `operator_interaction` records with the milestone in the
payload — the framework is reused, not duplicated.

## 4. Telemetry Architecture

```
React interaction → recordOperatorEvent() [fire-and-forget, Date.now()]
   → POST /api/v1/investigations/{id}/operator-events  (append-only JSONL)
   → GET  /api/v1/investigations/{id}/operator-mtti     (compute on read)
```

Reuses `pilot_telemetry.append_event` / `load_events` for storage. Telemetry
failures are swallowed in the UI so they can never slow or break the operator's
workflow. Wired emit points today: `investigation_opened` (on open) and
`timeline_opened` / `graph_opened` / `evidence_panel_opened` (on panel view).

## 5. External Tool Escape Analysis (Phase 3)

`external_tool_escapes()` aggregates `external_tool_opened` events by tool:
count, total `time_away_ms`, and the reasons given. This answers *why did the
operator leave, and can SentinelAI eliminate that context switch?* — once real
escapes are recorded. **No escapes are invented**; with no operators the result
is empty, not fabricated.

## 6. Workflow Bottlenecks (Phase 4)

Derivable from the recorded stream: repeated panel opens, long gaps between
milestones, and escape frequency all surface as measured signals. Concrete
bottleneck findings are **NOT_MEASURED** until operator sessions exist — the
instrumentation to find them is now in place.

## 7. Decision Quality Model (Phase 5)

`decision_quality()` counts `recommendation_accepted` vs `recommendation_rejected`
and reports `acceptance_rate` (null when no decisions). Every accept/reject is
evidence. Values are **NOT_MEASURED** today (no operator decisions recorded).

## 8. Updated MTTI Measurement Model (Phase 6 — kept separate)

| System metrics (`/mtti`) | Operator metrics (`/operator-mtti`) |
|---|---|
| time_to_first_evidence | time_to_first_useful_evidence |
| time_to_root_cause | time_to_understanding |
| time_to_owner | time_to_confidence |
| time_to_recommendation | time_to_decision |
| total (engine) | time_to_next_action / total (operator) |

The two are **never combined**. Their difference is the product-improvement
surface: where the system was ready but the operator was still working.

## 9. Validation Results (Phase 8)

- Investigation engine, replay, evidence, confidence, determinism: **unchanged**
  (all additions are new files + additive routes/UI; no engine code touched).
- New tests: `tests/agui/test_operator_telemetry.py` (14) — event model,
  operator-segment math, first-evidence priority, missing-milestone nulls,
  no-negative on skew, escape aggregation, decision acceptance rate, and
  `baseline_delta` NOT_MEASURED / seconds-saved.
- UI: typechecks (`tsc --noEmit`) and builds (`vite build`); `ui/dist` rebuilt.
- Full regression: see the commit message.

## 10. Remaining Evidence Gaps

Everything that requires a live operator: actual operator MTTI values, escape
reasons, decision acceptance, workflow bottlenecks, and the baseline-vs-
SentinelAI delta (Phase 7). All return `NOT_MEASURED` today. The capability to
capture them is built; the data requires a pilot.

## 11. Go / No-Go

> **GO to instrument a supervised pilot.** The operator timeline is now
> measurable end to end and kept separate from system time. **NO-GO on any
> claim that SentinelAI reduces operator MTTI** — that is `NOT_MEASURED` and
> stays so until a controlled pilot supplies baseline and operator data.

Final rule enforced: a future feature ships only if it moves a measured
operator segment — and both the system and operator clocks now exist to judge it.
