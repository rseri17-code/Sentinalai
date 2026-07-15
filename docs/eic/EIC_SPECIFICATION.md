# Enterprise Investigation Challenge (EIC) — Specification
**The engine-agnostic benchmark for enterprise incident investigation.**
SWE-bench / MMLU / ImageNet, but for RCA. Produce-only, deterministic, replayable.

> Design invariant: **the benchmark is not internal to SentinelAI.** SentinelAI competes
> against the benchmark; it does not define it. The scorer reads a neutral Task + Submission
> and grades every engine identically — a human Principal SRE, Dynatrace Davis, a research
> agent, a future SentinelAI, or GPT/Claude/Gemini.

## Package
`sentinel_core/eic/` — `benchmark.py` (engine-agnostic core: schemas, scorer, leaderboard),
`adapter.py` (the ONLY SentinelAI-coupled file; other engines supply their own). The core
imports nothing from the SentinelAI runtime and reads no `_*` internal keys.

## 1. Investigation Tasks (`make_task`)
Engine-agnostic. Categories: kubernetes · database · deployment · authentication · dns ·
middleware · network · storage · cloud · multi_cause · cascading_failure. Each task:
- `incident` — summary, service, severity, timestamp.
- `telemetry` — opaque evidence blobs keyed by name (the engine reads what it wants).
- `ground_truth` — root_cause (+keywords), root_cause_service, necessary_evidence,
  decisive_evidence. **The hidden answer key.**
- `traps` — `distractor_evidence` (looks relevant, isn't) + `false_hypotheses`
  (plausible-but-wrong causes that a good investigator rules out).
- `task_hash` — deterministic sha256 for reproducibility.

## 2. Difficulty Levels
single_cause → competing_hypotheses → missing_telemetry → contradictory_evidence →
cross_service → cascading_failure → unknown_root_cause (novice → expert). Difficulty names
the *investigative* challenge, not just the domain.

## 3. Submission (`make_submission`) — what ANY engine emits
Neutral: `engine`/`engine_version`, `root_cause`, `localized_service`, `hypotheses[]`,
`ruled_out[]`, `evidence_used[]` (ordered acquisition sequence), `decisive_evidence[]`,
`confidence`, `proof`, `replay_hash`. No engine-specific fields.

## 4. Scoring (`score_submission`) — ten dimensions ∈ [0,1] (or NOT_MEASURED)
| Dimension | Weight | What it measures |
|---|---|---|
| rca_correctness | 0.30 | RCA matches ground truth (keyword/substring) |
| localization | 0.15 | localized service matches the true origin |
| false_lead_avoidance | 0.12 | fraction of `false_hypotheses` correctly ruled out |
| decisive_evidence_latency | 0.10 | how early the decisive evidence was collected (0 if never) |
| evidence_efficiency | 0.10 | precision of collected evidence vs necessary set |
| distractor_avoidance | 0.08 | steered clear of `distractor_evidence` traps |
| hypothesis_quality | 0.05 | the true cause was among considered hypotheses |
| confidence_calibration | 0.05 | 1 − \|confidence − correctness\| |
| explainability | 0.03 | proof present and grounded in named evidence |
| replayability | 0.02 | a stable replay hash accompanies the submission |
**EIC score** = weighted mean over *measured* dimensions (renormalized), reported with
`coverage`. A dimension a task can't test (e.g. no traps) is NOT_MEASURED and excluded —
a high score at low coverage is not a clean win.

## 5. Reuse of IQS
The EIC deliberately *does not* import the Gold-Standard IQS (which reads SentinelAI
internals). It re-implements the same metric *philosophy* on the neutral submission so the
scoring is engine-agnostic. IQS remains the internal instrument; EIC is the external one.

## Worked example (deterministic)
On the `EIC-DB-001` task: a strong investigator (correct RCA, localized to `db`, ruled out
the DNS + deploy false hypotheses, collected the decisive `db_pool_metrics` first, avoided
the `dns_probe` distractor) scores **0.99**; a weak one (wrong DNS RCA, chased the
distractor, no proof) scores **0.07**. Same scorer, same task, no engine-specific logic.

## Longitudinal leaderboard (`leaderboard`)
Ranks engines by mean EIC score with bootstrap CI + per-category and per-difficulty
breakdowns; `release` tags a historical entry. Re-runnable every release to produce a
historical leaderboard — the basis for any "SentinelAI is improving over time" claim,
grounded in an engine-independent measure.

## Non-negotiables honored
No runtime modification · no new investigation features · no architectural change · no
Wave 3 · no authority promotion · no retrieval · deterministic · replayable ·
engine-agnostic · regression-safe · produce-only. IQS and the Gold Dataset are untouched.
