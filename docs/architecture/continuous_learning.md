# Continuous Learning Engine

**Status:** Landed at branch `claude/code-review-analysis-MelXd`.
**Location:** `sentinel_core/continuous_learning/`.
**External dependencies:** none — stdlib only.
**Feature flag:** `ENABLE_CONTINUOUS_LEARNING` (advisory — library is always importable; `is_enabled()` reads the env var for callers that want the flag semantics).

Deterministic, offline, append-only learning engine that turns
completed investigations into future-investigation improvement inputs.
No online training. No LLM fine-tuning. No neural networks.

## Non-goals

- Not a production runtime.
- Not autonomous remediation.
- Not a self-modifying planner.

## Modules

| Module | Purpose |
|--------|---------|
| `feedback_collector.py` | Immutable `FeedbackCollector` + `FeedbackSignal` + `FeedbackSource`/`FeedbackKind` enums |
| `outcome_memory.py` | Append-only `OutcomeMemory` ledger + `OutcomeRecord` dataclass |
| `confidence_calibrator.py` | `ConfidenceCalibrator` → 5 20-point `CalibrationBin` entries with predicted-vs-actual success rates |
| `evidence_quality.py` | `EvidenceQualityScorer` → per-evidence quality score |
| `strategy_feedback.py` | `StrategyFeedback` → per-capability effectiveness |
| `hypothesis_feedback.py` | `HypothesisFeedback` → per-hypothesis accuracy |
| `causal_feedback.py` | `CausalFeedback` → recurring chain effectiveness (reuses `sentinel_core.causal_graph.ChainDetector`) |
| `service_learning.py` | `ServiceLearning` → per-service reliability profile |
| `false_positive_learning.py` | `FalsePositiveLearning` → false-lead aggregation |
| `learning_engine.py` | `LearningEngine` + `LearningScores` (12 metrics) |
| `learning_cycle.py` | `LearningCycle` + `LearningSnapshot` (append-only, deterministic snapshot id) |
| `report_renderer.py` | 9 JSON renderers + `render_master_report` + `to_json` |

## Learning Model

Every learning cycle produces a `LearningScores` object with 12 metrics:

| Metric | Range | Meaning |
|--------|:-----:|---------|
| `evidence_quality` | 0-1 | Mean quality across evidence keys (success_uses / total_uses) |
| `hypothesis_accuracy` | 0-1 | Mean per-hypothesis accuracy |
| `strategy_effectiveness` | 0-1 | Unweighted mean per-capability effectiveness |
| `planner_effectiveness` | 0-1 | Use-weighted mean per-capability effectiveness |
| `false_positive_rate` | 0-1 | Total false leads / total evidence seen |
| `false_negative_rate` | 0-1 | 1 − (successful investigations / total) |
| `service_reliability` | 0-1 | Mean per-service success rate |
| `root_cause_confidence` | 0-1 | Mean confidence for records with a root cause |
| `replay_agreement` | 0-1 | From REPLAY-source feedback signals; fallback: mean investigation_score |
| `benchmark_agreement` | 0-1 | From BENCHMARK-source feedback signals; fallback: mean sentinelbench_score |
| `learning_confidence` | 0-1 | Mean of the 8 primary metrics |
| `operational_confidence` | 0-1 | `learning_confidence × (1 − false_positive_rate)` |

Every value is a closed-form transform. Same corpus → byte-identical scores.

## Feedback Pipeline

`FeedbackCollector` — immutable-by-convention:
- `add(signal)` and `add_many(signals)` return NEW instances.
- Signal kinds: 13 in `FeedbackKind` covering RCA correctness, false positive/negative, resolution accept/reject, MTTI / confidence overrides, hypothesis accept/reject, strategy approve/reject.
- Sources: OPERATOR, REPLAY, BENCHMARK, SYSTEM.

`OutcomeMemory` — append-only ledger of `OutcomeRecord` entries. Never mutates prior entries. Historical states remain reproducible.

## Append-Only Learning

`LearningCycle.run(records, feedback, sequence, generated_at)` returns a `LearningSnapshot`. Callers accumulate snapshots externally (e.g. in an append-only JSONL). `snapshot_id` is `sha256[:16]` of `(sorted memory_ids, sequence)` — same corpus + same sequence → same id → replayable.

## Confidence Calibration

`ConfidenceCalibrator.calibrate(records)` returns 5 `CalibrationBin` entries partitioning `[0,20), [20,40), [40,60), [60,80), [80,101)`. Each bin reports:
- `predicted_count`, `average_predicted`
- `actual_success_rate` (records with `investigation_score ≥ 0.5`)
- `memory_ids` (sorted)

## Reports

| Renderer | Purpose |
|----------|---------|
| `render_learning_report` | 12 scores + corpus_size |
| `render_confidence_calibration` | 5 calibration bins |
| `render_strategy_learning` | Per-capability effectiveness |
| `render_hypothesis_learning` | Per-hypothesis accuracy |
| `render_causal_learning` | Recurring causal chains |
| `render_service_learning` | Per-service reliability |
| `render_false_positive_report` | False-lead aggregation |
| `render_operator_feedback` | Signals list + by_kind counts |
| `render_continuous_learning_summary` | Latest `LearningSnapshot` |
| `render_master_report` | All 9 bundled |

All emit deterministic `sort_keys=True` JSON via `to_json`.

## Reuse (no duplicate storage)

- **Incident Intelligence Memory** — `MemoryRecord` is the primary input.
- **Cross-Incident Causal Graph** — `CausalFeedback` reuses `ChainDetector` verbatim.
- **Hypothesis Intelligence** — hypotheses read from `MemoryRecord.decision_trace.hypotheses`.
- **Strategy Optimizer** — capability ids read from `MemoryRecord.planner_decisions`.
- **SentinelBench / SentinelReplay** — scores land on `MemoryRecord.investigation_score` and `sentinelbench_score`.
- **KnowledgeGraph / DecisionContext / Planner / runtime** — never touched.

## Isolation Guarantees (tested)

- No import of `requests`, `httpx`, `urllib3`, `boto3`, `openai`, `anthropic`, `kubernetes`, or `supervisor.agent` in any module (isolation test enforces).
- Deterministic `sort_keys=True` serialisation; identical input → byte-identical output.
- No runtime module registered, no `install_default_modules` touched.
- Feature flag purely advisory — no runtime side effects.
- Append-only ledger: historical states remain reproducible.

## Files delivered

`sentinel_core/continuous_learning/` — 13 files (~1200 LOC library).
`tests/continuous_learning/test_learning_all.py` — 79 tests across 9 test classes.
`docs/architecture/continuous_learning.md` — this file.
