# Dynamic Investigation Strategy Optimizer

**Status:** Landed at branch `claude/code-review-analysis-MelXd`.
**Location:** `sentinel_core/strategy_optimizer/`.
**External dependencies:** none — stdlib only.

Deterministic recommendation engine that learns the highest-value
investigation strategy from Incident Intelligence Memory, Hypothesis
Intelligence, SentinelReplay, and Planner outputs.

## Non-goals

- Not autonomous execution.
- Not planner replacement.
- Not runtime modification.

## Modules

| Module | Purpose |
|--------|---------|
| `schemas.py` | Frozen `StrategyStep`, `InvestigationStrategy`, `MttiEstimation`, `StrategyRecommendation` + `StrategyRecommendationKind` enum |
| `cost_model.py` | Default per-evidence + per-tool + switching cost tables; deterministic `execution_cost` + `overall_value` formulas |
| `strategy_graph.py` | Co-occurrence graph (capability, evidence, transitions) over a `MemoryRecord` corpus |
| `mtti_estimator.py` | Historical mean + confidence interval + potential improvement |
| `optimizer.py` | `StrategyOptimizer.build_strategy` (per class) + `build_all_strategies` (6 classes) |
| `ranking.py` | `StrategyRanker` + `StrategyClass` enum |
| `recommendation_engine.py` | Deterministic recommendation emission with WHY-evidence |
| `report.py` | 6 report renderers + `render_master_report` + `to_json` |

## Expected Value Model

For each capability `c` observed over `n` records:

- `expected_information_gain = uses_of_c / total_records`
- `historical_success_rate   = successful_uses_of_c / uses_of_c`
- `expected_confidence_gain  = clamp(info_gain × 30 × 100)`  (0-100)
- `expected_mtti_reduction_ms = success_rate × 30_000`
- `execution_cost = evidence_cost + tool_cost + switching_overhead`
- `overall_expected_value = info_gain × (confidence_gain / 100) × success_rate − min(0.5, execution_cost / 20_000)`

Every value is a closed-form transform. Same inputs → byte-identical output.

## Strategy Classes

`StrategyOptimizer.build_all_strategies` emits one strategy per class:

| Class | Sort key |
|-------|----------|
| `best` | `(-overall_value, -success_rate, capability_id)` |
| `fastest` | `(execution_cost, capability_id)` |
| `highest_confidence` | `(-expected_confidence_gain, -success_rate, capability_id)` |
| `lowest_cost` | `(execution_cost + evidence_cost, capability_id)` |
| `highest_success` | `(-success_rate, -overall_value, capability_id)` |
| `balanced` | `(-overall_value, capability_id)` |

## Recommendations

Each `StrategyRecommendation` includes:

- `kind` — from `StrategyRecommendationKind` (recommended_order, skip_evidence, prefer_tool, prefer_capability, avoid_capability, deepen_investigation, shorten_strategy).
- `message` — deterministic human-readable summary.
- `evidence` — tuple of WHY strings (required).
- `priority` — derived deterministically from usage rate + success rate.
- `related_capabilities` — sorted tuple.

Sort order: `(-priority, kind, message)`.

## Reports

| Report | Purpose |
|--------|---------|
| `strategy_report` | All strategies ranked |
| `recommended_strategy` | Top strategy + alternatives |
| `mtti_estimation` | Historical / current / expected / potential improvement |
| `planner_effectiveness` | Per-capability use + success rate |
| `evidence_value_report` | Per-evidence use + usage rate |
| `investigation_efficiency` | Per-record efficiency = (confidence × score) / (1 + runtime_cost) |
| `master_report` | Bundle of all six |

## Reuse of existing subsystems

- **Incident Intelligence Memory** — `MemoryRecord.planner_decisions` + `.evidence_collected` + `.evidence_ordering` + `.confidence` + `.mtti_ms` + `.investigation_score` are the sole inputs. No duplicate storage.
- **Deterministic Planner** — capability ids consumed verbatim; no planner mutation.
- **SentinelReplay** — outputs can be scored into `investigation_score` and stored on `MemoryRecord`; optimizer picks them up automatically.
- **SentinelBench** — `sentinelbench_score` on `MemoryRecord` is available to the optimizer; not currently scored but hookable via extension.
- **Hypothesis Intelligence** — future extension: consume `HypothesisGraph.mtti_contribution_ms` per-hypothesis to refine per-capability MTTI reduction.

## Isolation guarantees (tested)

- No import of `requests`, `httpx`, `urllib3`, `boto3`, `openai`, `anthropic`, `kubernetes`, or `supervisor.agent`.
- No import of `intel_memory` in a way that couples the optimizer to a runtime call; `MemoryRecord` is used only as a data type.
- Deterministic `sort_keys=True` output.
- Planner integration deliberately deferred: recommendations are emitted only; no runtime module is registered.

## Sample

See `docs/architecture/strategy_optimizer_sample.json` — three-record corpus scoring the pod-lifecycle path over DNS.

## Files delivered

`sentinel_core/strategy_optimizer/` — 9 files (~950 LOC library).
`tests/strategy_optimizer/test_strategy_all.py` — 81 tests across 8 test classes.
`docs/architecture/strategy_optimizer.md` — this file.
`docs/architecture/strategy_optimizer_sample.json` — reproducible sample.
