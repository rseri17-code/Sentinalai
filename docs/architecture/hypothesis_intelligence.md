# Hypothesis Intelligence

**Status:** Landed at branch `claude/code-review-analysis-MelXd`.
**Location:** `sentinel_core/hypotheses/` (renamed from
`hypothesis_intelligence/` because `intelligence` is forbidden in
sentinel_core module names — see `test_sentinel_core_compatibility.py`).

Captures the hypothesis lifecycle of an investigation: hypotheses
considered, evidence supporting each, evidence refuting each,
confidence movement, ruled-out causes, final confirmed hypothesis,
MTTI contribution. Deterministic, immutable, JSON-safe, LLM-free.

## Modules

| Module | Purpose |
|--------|---------|
| `schemas.py` | `Hypothesis`, `HypothesisEvidence`, `HypothesisTransition`, `HypothesisStatus` enum, deterministic `make_hypothesis_id` |
| `hypothesis_graph.py` | `HypothesisGraph` — immutable container with `by_status` / `confirmed` / `ruled_out` / `supported` / `refuted` accessors |
| `hypothesis_tracker.py` | `HypothesisTracker` — builder for a graph; `propose` / `add_supporting_evidence` / `add_refuting_evidence` / `transition` / `rule_out` / `confirm` / `finalise` / `build_graph` |
| `scoring.py` | `HypothesisScore` + `score_hypothesis` + `score_hypothesis_graph` |
| `report.py` | 4 report renderers + `render_master_report` + `to_json` (deterministic) |

## Lifecycle

```
propose(name)
      │
      ▼
add_supporting_evidence / add_refuting_evidence
      │
      ▼
transition(status, new_confidence, reason)
      │
      ▼
rule_out(reason)   OR   confirm(root_cause, reason)
      │
      ▼
finalise(completed_at) → build_graph() → immutable HypothesisGraph
```

## Reusable by

- **SentinelReplay** — score how well hypotheses were tracked across
  replays.
- **SentinelBench** — assert a scenario's expected confirmed hypothesis
  matches the tracked one.
- **Incident Intelligence Memory** — a `MemoryRecord` can reference the
  hypothesis graph via `receipt_references`; the graph itself lives as
  a JSON artifact.

## Isolation guarantees (tested)

- No import of `requests`, `httpx`, `urllib3`, `boto3`, `openai`,
  `anthropic`, `kubernetes`, or `supervisor.agent`.
- Deterministic serialization; identical input → byte-identical output.
- No mutation of any existing subsystem.

## Sample

See `docs/architecture/hypothesis_intelligence_sample.json` — a
realistic two-hypothesis investigation (one confirmed, one ruled out)
against the `k8s_pod_crashloop` SentinelBench scenario.

## Files delivered

| Path | LOC |
|------|-----|
| `sentinel_core/hypotheses/__init__.py` | 45 |
| `sentinel_core/hypotheses/schemas.py` | 100 |
| `sentinel_core/hypotheses/hypothesis_graph.py` | 70 |
| `sentinel_core/hypotheses/hypothesis_tracker.py` | 160 |
| `sentinel_core/hypotheses/scoring.py` | 55 |
| `sentinel_core/hypotheses/report.py` | 85 |
| `tests/hypothesis_intelligence/test_hypothesis_all.py` | 250 |
| `docs/architecture/hypothesis_intelligence.md` | this file |
| `docs/architecture/hypothesis_intelligence_sample.json` | sample |
