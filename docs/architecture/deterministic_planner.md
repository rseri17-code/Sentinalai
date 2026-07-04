# Deterministic Planner — Architecture Documentation

**Status:** Landed at branch `claude/code-review-analysis-MelXd`.
**Feature flag:** `ENABLE_PLANNER` (default OFF — byte-identical runtime when disabled).

The deterministic planner is the LLM-free intermediate layer between
Decision Intelligence and future skill execution. It consumes the
already-persisted intelligence corpus and emits a structured, ordered
plan of investigative intents.

## Non-goals

- Not an agent.
- Not an LLM planner.
- Not autonomous execution.
- Not aware of concrete tools (kubectl, sysdig, prometheus, elastic, otel, …).

## Architecture diagram

```
    Runtime pipeline (unchanged)
    ────────────────────────────
    FETCH → CLASSIFY → COLLECT → ANALYZE → PERSIST
                                              │
                                              ▼
                             ┌──────────────────────────────┐
                             │  Intelligence Runtime        │
                             │  (POST_PERSIST stage)        │
                             │                              │
                             │  resolution_memory      100  │
                             │  investigation_store    200  │
                             │  intelligence_context_persister 800 │
                             │  enterprise_knowledge_graph 900 │
                             │  planner                950  │◀── THIS MISSION
                             └──────────────────────────────┘
                                              │
                                              ▼
                              ctx.phase_receipts
                                              │
                                              ▼
                          ┌──────────────────────────────┐
                          │  Planner runner              │
                          │  (deterministic, LLM-free)   │
                          │                              │
                          │  IntelligenceContext         │
                          │      ↓                       │
                          │  DecisionContext             │
                          │      ↓                       │
                          │  KnowledgeGraph              │
                          │      ↓                       │
                          │  PlanContext                 │
                          │      ↓                       │
                          │  PlannerBuilder.build()      │
                          │      ↓                       │
                          │  InvestigationPlan           │
                          └──────────────────────────────┘
                                              │
                                              ▼
                     receipt.metadata["intelligence"]["planner"]
```

## Planner lifecycle

1. **Intake** — the runner receives `ctx.phase_receipts`, a tuple of
   already-finalised phase receipts (fetch + classify + collect + analyze).
2. **Reconstruct** — the runner rebuilds the canonical models from receipts:
   IntelligenceContext → DecisionContext → KnowledgeGraph. Nothing here
   queries a store.
3. **Envelope** — the runner constructs an immutable `PlanContext`:
   `(service, incident_type, decision_context, knowledge_graph, receipts,
   current_confidence, target_confidence)`.
4. **Derive goals** — `derive_goals(plan_context)` applies deterministic
   rules over incident_type keywords + DecisionContext signals to derive
   a tuple of `InvestigationGoal` objects.
5. **Select capabilities** — for every goal,
   `select_capabilities_for_goal(goal_type)` returns the applicable
   `Capability` objects from the canonical catalog. Duplicates are
   dropped.
6. **Order + budget** — steps are sorted by
   `(-expected_confidence_gain, -priority, step_id)`. The planner stops
   appending steps once cumulative confidence reaches `target_confidence`
   or `max_steps` is hit.
7. **Dependencies** — `compute_dependencies(steps)` derives step-level
   prerequisite edges (e.g. `compare_historical_failures` depends on
   `collect_historical_incidents`).
8. **Return** — an `InvestigationPlan` is emitted on receipt metadata.
   No I/O. No LLM. No side effects.

## Goal model

An `InvestigationGoal` represents *what* must be proven:

| Field | Purpose |
|-------|---------|
| `goal_id` | Deterministic sha256[:16] of `(goal_type, description)`. |
| `goal_type` | One of `GoalType`. |
| `description` | Human-readable one-line summary. |
| `priority` | 1-1000, higher = more important; tie-breaker. |
| `completion_criteria` | Strings the caller inspects to verify success. |
| `failure_criteria` | Strings that signal the goal has failed. |
| `expected_confidence_gain` | 0-100, per-goal projection. |

**Extending goal types**: append to `GoalType`; add rules in
`planner_rules.derive_goals`. No consumer needs to change.

## Capability model

A `Capability` represents *what type* of investigation satisfies a goal:

| Field | Purpose |
|-------|---------|
| `capability_id` | `"cap:" + capability_type` — deterministic. |
| `capability_type` | One of `CapabilityType`. |
| `description` | Human-readable summary. |
| `satisfies_goal_types` | Which goal types this capability contributes to. |
| `typical_evidence_yield` | Evidence keys typically produced. |
| `typical_confidence_gain` | 0-100 assumed. |
| `typical_runtime_ms` | Rough runtime estimate for the latency budget. |

**Extending capability types**: append to `CapabilityType`; add a catalog
entry in `planner_rules._capability_catalog`. No consumer changes.

## Skill Registry (data-only)

`SkillRegistry` maps `capability_id` → tuple of possible skill-name
strings. Nothing here executes anything. The default is `MappingProxyType`
(immutable). Callers extend via `SkillRegistry.extend()` which returns a
NEW instance.

```python
{
    "cap:collect_pod_lifecycle":     ("kubectl_pods",  "sysdig_pods"),
    "cap:collect_logs":              ("elastic_logs",  "loki_logs",  "kubectl_logs"),
    "cap:compare_historical_failures":("resolution_memory_read", "pattern_intelligence_read"),
    ...
}
```

**The planner never returns skill names.** It returns capability ids
only. A future execution layer resolves skills from the registry.

## Extension guide

### Add a new goal type
1. Append `GoalType.MY_NEW_GOAL = "my_new_goal"` in
   `sentinel_core/models/goal.py`.
2. In `supervisor/deterministic_planner/planner_rules.py::derive_goals`,
   add a rule that fires when the goal should be derived.
3. Optionally add capability catalog entries whose
   `satisfies_goal_types` include `"my_new_goal"`.

### Add a new capability type
1. Append `CapabilityType.MY_CAP = "my_cap"` in
   `sentinel_core/models/capability.py`.
2. Register it in `planner_rules._capability_catalog` with the goal
   types it satisfies and typical evidence yield / confidence gain /
   runtime estimate.
3. Add a skill mapping to `DEFAULT_SKILL_REGISTRY` — or leave empty and
   allow future callers to `SkillRegistry.extend()`.

### Add a new dependency edge
Append a `(dependent_capability, prerequisite_capability)` tuple to
`_CAPABILITY_DEPS` in `planner_rules.py`. Only edges where BOTH
endpoints appear in the plan are emitted.

## Feature-flag strategy

- **`ENABLE_PLANNER`** — default OFF. When off, `planner_runner` returns
  `status="skipped"` without touching anything. Bypass proven byte-
  identical.
- **`ENABLE_INTELLIGENCE_RUNTIME`** (master) — when off, the entire
  runtime is bypassed; the planner runner is never invoked.

## Future execution model

The planner is deliberately *disconnected* from execution. A future
"skill runtime" milestone will:

1. Consume the `InvestigationPlan` from receipt metadata.
2. For each `PlanStep`, resolve `capability_id` → concrete skill via
   `SkillRegistry.skills_for(capability_id)`.
3. Invoke the concrete skill (kubectl, sysdig, prometheus, MCP tool,
   remediation) — a separate feature flag gates execution.
4. Update evidence + emit new receipts.
5. Feed the updated intelligence back into the planner via
   `PlanContext(completed_goals=…)` for the next round.

This lets Sentinel evolve from *analysis-only* → *guided investigation*
→ *autonomous operations* without ever refactoring the planner core.

## Files

| Path | Purpose |
|------|---------|
| `sentinel_core/models/goal.py` | InvestigationGoal + GoalType |
| `sentinel_core/models/capability.py` | Capability + CapabilityType |
| `sentinel_core/models/plan.py` | PlanStep + InvestigationPlan |
| `sentinel_core/models/plan_context.py` | PlanContext |
| `sentinel_core/models/planner.py` | Re-export facade |
| `supervisor/deterministic_planner/__init__.py` | Package facade |
| `supervisor/deterministic_planner/planner_registry.py` | SkillRegistry |
| `supervisor/deterministic_planner/planner_rules.py` | Deterministic rules |
| `supervisor/deterministic_planner/planner_builder.py` | PlannerBuilder |
| `supervisor/deterministic_planner/planner_runtime.py` | Runtime adapter |
| `tests/test_planner_models.py` | Canonical-model tests |
| `tests/test_planner_builder.py` | Deterministic-transform tests |
| `tests/test_planner_activation.py` | Runtime activation tests |

## Notes

- The directory is `supervisor/deterministic_planner/` (not
  `supervisor/planner/`) to avoid clashing with the pre-existing
  `supervisor/planner.py` (the agentic LLM planner). Mission constraint
  "Do NOT modify existing runtime modules" is preserved.
- The planner registers via `supervisor.intelligence_modules.install_default_modules`
  — the established seam. `supervisor/agent.py` is not touched.
