# EIC — Human Benchmark, Vendor Benchmark & Publication Standard
**Deliverables 4, 5, 6, 7 · design-only protocols for the engine-agnostic benchmark**

## 4. Human Benchmark — Principal SRE as the gold reference
The human investigation is the reference bar the automated engines are measured against.
- **Independent reviewers.** ≥3 Principal-level SREs investigate each task *blind* to each
  other and to any engine output, each producing a standard EIC Submission.
- **Consensus process.** Ground truth is set by the *validated postmortem*, not by the
  reviewers; reviewer submissions are scored against it like any engine. The human
  *reference score* is the median reviewer EIC score per task.
- **Disagreement handling.** Where reviewers diverge on RCA, the disagreement is recorded
  as a task-level `human_dispersion` metric; high dispersion flags an ambiguous task (a
  candidate for `unknown_root_cause` difficulty or removal).
- **Operator confidence.** Captured per reviewer (0–100) and compared to their correctness
  to calibrate the human baseline itself.
- **Postmortem reconciliation.** The final validated postmortem supersedes all reviewer
  labels; any task whose postmortem contradicts the consensus is quarantined and re-authored.
- **Fairness.** Reviewers get the same `telemetry` blob an engine gets — no privileged
  access, no extra tooling beyond what the task provides.

## 5. Vendor Benchmark — evaluating external systems fairly
- **Same task, same submission schema.** A vendor (Dynatrace Davis, an LLM agent, OpenSRE)
  runs on the identical `telemetry` and returns a standard EIC Submission via its own thin
  adapter (the analogue of `eic/adapter.py`). The scorer is unchanged.
- **No vendor-specific optimization.** Tasks are authored against generic telemetry
  semantics (logs/metrics/traces/events), never a vendor's proprietary signal names. A
  vendor may map task telemetry into its own model, but may not receive the ground truth,
  traps, or a task-tuned configuration.
- **Held-out set.** A sealed held-out partition prevents training/tuning to the benchmark;
  vendors are scored on tasks they have never seen.
- **Adapter disclosure.** Each engine's adapter is published with its results so the mapping
  from task telemetry → submission is auditable (no hidden ground-truth leakage).
- **Environment parity.** Deterministic, offline replay: every engine sees the same frozen
  task bytes (`task_hash`), so results are reproducible and comparable across runs.

## 6. Longitudinal Benchmark — leaderboards over time
- Every engine re-runs the full suite each release; `leaderboard(..., release="YYYY.MM")`
  stamps a historical entry.
- Per-engine trend of mean EIC score (with CI) across releases is the *only* admissible
  basis for "engine X improved" — it is engine-independent and leakage-controlled.
- Regressions are visible per category/difficulty, not just in aggregate.

## 7. Publication Standard
Designed so results could be released as a technical report or used in architecture review:
- **Reproducibility.** Every result ships with: task_hash, engine + version, adapter source,
  seed, and the full per-dimension scores — a third party can recompute the EIC score
  byte-for-byte.
- **Statistical honesty.** Every leaderboard figure carries sample size, a bootstrap CI, and
  an `underpowered` flag below n=30; NOT_MEASURED is reported verbatim, never imputed.
- **Coverage transparency.** The `coverage` of each engine's score is published alongside it;
  claims are qualified by which dimensions were actually testable.
- **Task provenance.** Each task records category, difficulty, and (for real incidents) a
  sanitized postmortem reference; synthetic tasks are labeled as such.
- **Immutability + versioning.** The benchmark suite is versioned (`EIC_SCHEMA_VERSION`);
  tasks are append-only; a task is never silently edited — a changed task is a new task_id.

## Success criterion
The EIC is authored so it could still grade an investigation engine **five years from now**,
and could grade engines that **do not yet exist** — because it depends only on the neutral
Task/Submission contract, not on SentinelAI's internals. It is designed to **outlive
SentinelAI itself**.
