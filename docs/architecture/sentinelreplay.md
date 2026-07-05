# SentinelReplay — Offline Continuous Learning Engine

**Status:** Landed at branch `claude/code-review-analysis-MelXd`.
**Location:** `tests/replay/`
**External dependencies:** none — stdlib only.

SentinelReplay sits on top of SentinelBench (`tests/synthetic/`) and
provides offline replay, trend analysis, weakness detection, and
learning recommendations without touching any production runtime.

## Non-goals

- Not a production runtime.
- Not autonomous remediation.
- Not self-modifying code.
- Not a system that mutates production configuration or storage.

## Architecture overview

```
   ┌──────────────────────────────┐         ┌───────────────────────┐
   │  SentinelBench               │         │  ReplayStore          │
   │  (tests/synthetic/)          │         │  {root}/*.json        │
   │  - scenarios/                │         │  ─────────────────    │
   │  - scoring.py                │         │  save / load / list   │
   │  - runner.py                 │         │  date-range filter    │
   └──────────────┬───────────────┘         │  service filter       │
                  │                         │  incident_type filter │
                  ▼                         └────────────┬──────────┘
       ┌────────────────────┐                            │
       │  ReplayRunner      │◀───────────────────────────┘
       │  ──────────────    │
       │  replay_scenario   │
       │  replay_corpus     │
       │  capture_run       │
       │  compare_runs      │
       └──┬───────┬────┬────┘
          │       │    │
          ▼       ▼    ▼
   ┌───────┐ ┌──────────────┐ ┌────────────────────┐
   │trend_ │ │learning_     │ │recommendation_     │
   │analysis│ │engine        │ │engine              │
   └───────┘ └──────────────┘ └────────────────────┘
          │       │    │
          ▼       ▼    ▼
   ┌────────────────────────────────────────────┐
   │  replay_report / heatmap / master_report   │
   │  Deterministic JSON (sort_keys=True)       │
   └────────────────────────────────────────────┘
```

## Replay lifecycle

1. **Capture** — `ReplayRunner.capture_run(run_id, generated_at, ...)`
   runs every scenario in the SentinelBench corpus, scores each with
   optional `investigation_outputs`, and returns a `BenchmarkRun`.
   Stored to disk if a `ReplayStore` is attached.
2. **Replay** — `ReplayRunner.replay_scenario` or `replay_corpus`
   scores against a supplied baseline_run_id from the store and emits
   `ReplayResult` records with a verdict (`new`, `stable`, `improved`,
   `regressed`).
3. **Aggregate** — `render_master_report(runs, results)` produces
   the seven artifact reports (see Outputs below).

## Learning lifecycle

1. Load N historical runs from the store.
2. `LearningEngine.analyze(runs)` scans per-`(scenario_id, dimension)`
   time-series. When the *tail-most* consecutive scores fall at or
   below `weak_threshold` for at least `min_consecutive` runs, a
   `WeaknessRecord` is emitted.
3. `LearningEngine.leaderboard(...)` ranks weaknesses by
   `(count DESC, average_score ASC)`.
4. Dimension → WeaknessType mapping is a closed-form dictionary; no
   heuristics.

## Recommendation lifecycle

1. `RecommendationEngine.recommend(weaknesses)` maps each weakness type
   to a canonical recommendation kind (data-only dictionary).
2. Each `Recommendation` includes:
   - **kind** — one of `RecommendationKind` (Missing Evidence, Recommended
     Collector, Planner Capability, Benchmark Scenario, KG Entity, Topology
     Improvement, Transaction Mapping, RCA Pattern, Investigation Order,
     MTTI Improvement).
   - **message** — deterministic human-readable summary using scenario_id
     + dimension.
   - **evidence** — tuple of `WHY` strings (count, average_score,
     weakness_type, dimension). Never empty.
   - **priority** — deterministic `100 + count*50 + (1-avg)*100`, capped
     at 1000.
   - **related_scenarios** — sorted list of impacted scenario ids.

Recommendations are sorted by `(priority DESC, kind, message)` for
stable ordering across runs.

## Outputs (all deterministic JSON)

| File | Renderer |
|------|----------|
| `replay_report.json` | `render_replay_report(results)` |
| `trend_report.json` | `render_trend_report(runs)` |
| `regression_report.json` | `render_regression_report(runs, threshold)` |
| `heatmap.json` | `render_heatmap_report(runs)` |
| `learning_report.json` | `render_learning_report(runs)` |
| `learning_recommendations.json` | `render_recommendations_report(runs)` |
| `weakness_leaderboard.json` | `render_weakness_leaderboard(runs)` |

`render_master_report(runs, results)` bundles all seven into a single
payload.

## Extension points

| Extension | How |
|-----------|-----|
| **New WeaknessType** | Append to `schemas.WeaknessType`, add a
  dimension mapping in `_DIM_TO_WEAKNESS` (learning_engine) and a kind
  mapping in `_WEAKNESS_TO_KIND` (recommendation_engine). |
| **New RecommendationKind** | Append to `schemas.RecommendationKind`,
  update `_WEAKNESS_TO_KIND` and `_MESSAGE_TEMPLATES` in
  `recommendation_engine.py`. |
| **New trend descriptor** | Add a function in `trend_analysis.py`;
  wire it into a new renderer if needed. |
| **New score dimension** | Add to `scoring.DEFAULT_WEIGHTS` + `ScoreCard`
  (SentinelBench). Replay automatically picks it up via reflection over
  `_DIMENSIONS`. |
| **Different corpus source** | Swap in a different `scenarios_dir` when
  calling `runner.load_all_scenarios(scenarios_dir=...)`. Replay reuses
  SentinelBench's loader verbatim. |

## Isolation guarantees (tested)

- No import of `requests`, `httpx`, `urllib3`, `boto3`, `openai`,
  `anthropic`, or `kubernetes` in any SentinelReplay module.
- No import of `supervisor.agent` in any SentinelReplay module.
- All I/O is scoped to caller-supplied paths; the default `ReplayStore`
  writes nothing until instantiated with a directory.
- Every artifact is a JSON dict with `sort_keys=True`; identical input
  → byte-identical output.

## Future roadmap

| Milestone | Description |
|-----------|-------------|
| **Corpus growth to 25+ scenarios** | Same shape; SentinelReplay picks them up automatically. |
| **Replay of real receipts** | Convert historical `_intelligence.json` / `_decisions.jsonl` artifacts into scenarios with mock_investigation_output derived from actual results. |
| **Planner effectiveness scoring** | New `planner_score` dimension in SentinelBench; new weakness type `PLANNER_CAPABILITY_GAP`. |
| **KG / topology-aware scoring** | Cross-reference `enterprise_knowledge_graph` receipt metadata with scenario-defined expected topology. |
| **MTTI trend regression gate for CI** | GitHub Actions job that fails PRs when `overall_mean` drops below a configurable threshold or when new weaknesses appear in the leaderboard. |
| **Recommendation delivery** | Emit recommendations as GitHub issues (deterministic body); optional, off by default. |

## Files delivered

| Path | Purpose |
|------|---------|
| `tests/replay/__init__.py` | Package docstring |
| `tests/replay/schemas.py` | Canonical dataclasses + enums |
| `tests/replay/replay_store.py` | ReplayStore (JSON-per-run persistence) |
| `tests/replay/replay_runner.py` | ReplayRunner (orchestrator) |
| `tests/replay/trend_analysis.py` | Trend descriptors + regression detection |
| `tests/replay/learning_engine.py` | LearningEngine (repeat-weakness detector) |
| `tests/replay/recommendation_engine.py` | RecommendationEngine |
| `tests/replay/heatmap.py` | Heatmap builder |
| `tests/replay/replay_report.py` | 7 report renderers + master report |
| `tests/replay/test_replay_runner.py` | Runner + store + reports + isolation tests |
| `tests/replay/test_learning_engine.py` | LearningEngine + RecommendationEngine tests |
| `tests/replay/test_trend_analysis.py` | Trend + heatmap tests |
