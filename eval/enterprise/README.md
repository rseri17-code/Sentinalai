# Synthetic Enterprise Validation Platform

A deterministic, continuously-runnable validation corpus of enterprise
investigation tasks — **not a mock demo**. It exercises the real enterprise tool
sources and reuses the existing engine-agnostic EIC benchmark; it introduces no
new evaluation framework and fabricates no results.

## What this is (and is not)
- **Is:** synthetic, content-addressed validation data with a hidden answer key
  (ground truth + operator expectations), plus a loader/validator that grades
  engine submissions via the existing `sentinel_core.eic` scorer.
- **Is not:** production data, operator behavior, or benchmark *results*. No
  engine score is claimed here — the corpus is the answer key. With no
  submissions, `validate()` returns `NOT_MEASURED`.

## Contents
| File | Purpose |
|---|---|
| `build_corpus.py` | deterministic corpus builder (reuses `eic.make_task`) |
| `corpus.json` | the materialized corpus (committed; must match a fresh build) |
| `validate.py` | loader + validator (reuses `eic.score_submission` + `leaderboard`) |

## Coverage
8 enterprise incidents spanning all **13 declared tool sources** — Splunk,
Dynatrace, Sysdig, ServiceNow, CMDB, Kubernetes, AWS, Network, Identity,
Application, Database, Autosys, ThousandEyes — across the classes saturation,
deploy, oomkill, network, identity, batch, cloud, cascade. Each task carries:
signal (incident), context (telemetry across sources), correlation +
hypotheses + decisive/necessary evidence (ground truth), traps (distractors +
false hypotheses), and operator expectations (owner, confidence range,
recommendation).

## Determinism & integrity
- Task hashes are content-addressed (`eic._sha16`); rebuilding is byte-identical
  (no clock, no randomness).
- `corpus.json` is guarded by a test that fails if it drifts from the builder.
- The reused EIC scorer grades an oracle submission high (>0.8) and a wrong one
  low (<0.5); missing dimensions/fields are `NOT_MEASURED`, not zero.

## Usage
```
python3 eval/enterprise/build_corpus.py           # (re)materialize corpus.json
python3 -c "from eval.enterprise.validate import validate; print(validate())"  # NOT_MEASURED w/o submissions
```
To grade an engine, pass `{task_id: neutral_submission}` (see
`sentinel_core.eic.make_submission`, and the `sentinelai_submission` adapter) to
`validate(submissions)`.

## Validation performed (Phase 1 gates)
- Investigation quality: each task well-formed with a graded answer key.
- Determinism: rebuild byte-identical; content-addressed hashes.
- Replay/evidence/confidence: the corpus format is the same neutral shape the
  EIC harness (and the `sentinelai_submission` adapter) already consume, so it
  is compatible with the deterministic engine + replay + evidence/confidence
  contracts without modifying them.
- Operator-workflow compatibility: incidents carry service/owner/severity and
  evidence keys consistent with the existing investigation + OIP inputs.

Tests: `tests/enterprise/test_enterprise_corpus.py` (11).
