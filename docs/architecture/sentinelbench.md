# SentinelBench — Synthetic Incident Evaluation Harness

**Status:** Landed at branch `claude/code-review-analysis-MelXd`.
**Location:** `tests/synthetic/`
**External dependencies:** none — pure Python stdlib.

SentinelBench is an offline, deterministic, CI-friendly evaluation
harness that scores SentinelAI's RCA quality, evidence completeness,
red-herring resistance, confidence calibration, decision-trace quality,
runtime cost, and MTTI against a corpus of synthetic incident
scenarios.

## Non-goals

- Not a production runtime.
- Not a training loop.
- Not an integration test for `investigate()`.
- Not a benchmark of external tooling (kubectl, sysdig, prometheus, LLMs).

## Architecture

```
┌────────────────────────────┐       ┌────────────────────────────┐
│  scenarios/*.json          │       │  investigation_output      │
│  (versioned corpus)        │       │  (optional; from callers)  │
│                            │       │                            │
│  ┌────────────────────┐    │       │  { root_cause, ...        │
│  │  Scenario schema   │◀───┤       │    confidence, ... }       │
│  │  (schemas.py)      │    │       │                            │
│  └────────────────────┘    │       └─────────────┬──────────────┘
│           ▲                │                     │
│           │ validation     │                     │
│           │                │                     ▼
└───────────┼────────────────┘   ┌────────────────────────────────┐
            │                    │  Scoring (scoring.py)          │
            │                    │                                │
            │                    │   root_cause_match       ×0.30 │
            └────────────────────┼─▶ evidence_completeness  ×0.15 │
                                 │   red_herring_resistance ×0.10 │
                                 │   confidence_calibration ×0.10 │
                                 │   decision_trace_quality ×0.15 │
                                 │   runtime_cost_score     ×0.10 │
                                 │   mtti_score             ×0.10 │
                                 │                                │
                                 │        Σ = overall_score       │
                                 └────────────────┬───────────────┘
                                                  │
                                                  ▼
                                    ┌─────────────────────────┐
                                    │  ScoreCard (frozen)     │
                                    │  render_report → JSON   │
                                    └─────────────────────────┘
```

## Scenario schema

Every scenario JSON has the following fields (validated at load time):

| Field | Purpose |
|-------|---------|
| `scenario_id` | Matches the filename stem. |
| `title` | One-line human summary. |
| `incident_input` | What would normally arrive at `investigate()`. |
| `mocked_evidence_sources` | The offline stand-in for Splunk/Prom/k8s data (documentation only). |
| `expected_root_cause` | Ground-truth RCA sentence. |
| `required_evidence` | List of evidence keys that MUST be reported. |
| `red_herrings` | Evidence / RCA fragments that should NOT drive the reported RCA. |
| `expected_confidence_range` | `[lo, hi]` where a well-calibrated confidence lands. |
| `expected_decision_signals` | Decision-context signals the investigation is expected to raise. |
| `expected_mtti_budget_ms` | Upper bound on mean-time-to-identify. |
| `expected_runtime_cost_budget` | Upper bound on runtime-cost proxy (tool calls, LLM turns, etc.). |
| `tags` | Free-form labels for filtering. |
| `mock_investigation_output` | **Optional.** Pre-baked "ideal investigation output" used when no external investigation output is supplied — this is what makes CI runs deterministic without invoking the real `investigate()`. |

## Scoring dimensions

Each dimension returns a float in `[0.0, 1.0]`; the weighted overall is
computed with the default weights below (sum = 1.0).

| Dimension | Weight | Rule |
|-----------|:------:|------|
| `root_cause_match` | 0.30 | Token-set Jaccard between expected and reported RCA. |
| `evidence_completeness` | 0.15 | Fraction of `required_evidence` keys present in reported keys. |
| `red_herring_resistance` | 0.10 | `1 - (fraction of red-herring tokens appearing in the reported RCA)`. |
| `confidence_calibration` | 0.10 | 1.0 if `reported ∈ [lo, hi]`; else linear 1/50 falloff per point outside. |
| `decision_trace_quality` | 0.15 | Fraction of `expected_decision_signals` present in reported signals. |
| `runtime_cost_score` | 0.10 | 1.0 if `actual ≤ budget`; else linear 1/budget falloff. |
| `mtti_score` | 0.10 | 1.0 if `actual_ms ≤ budget_ms`; else linear 1/budget falloff. |
| **`overall_score`** | 1.00 | Weighted sum of the above. |

Custom weights may be passed to `score_investigation` and to
`run_all_scenarios`.

## Runner

```python
from tests.synthetic.runner import run_scenario, run_all_scenarios
from tests.synthetic.report import render_report_json

# Single scenario with pre-baked mock output
card = run_scenario("k8s_pod_crashloop")

# Single scenario with an externally-produced investigation output
card = run_scenario("k8s_pod_crashloop", investigation_output={
    "root_cause": "...", "confidence": 82,
    "evidence_keys": [...], "decision_signals": [...],
    "mtti_ms": 62000, "runtime_cost": 15,
})

# Every scenario in the corpus
cards = run_all_scenarios()

# Selective overrides
cards = run_all_scenarios(investigation_outputs={"k8s_pod_crashloop": {...}})

# Deterministic JSON report (sort_keys=True)
print(render_report_json(cards))
```

## Initial corpus (5 scenarios)

| ID | Root cause |
|----|-----------|
| `auth_token_validation_failure` | IdP rotated JWT signing key; api-gateway JWKS cache stale. |
| `bad_deployment_5xx` | Deployment reduced connection pool from 20 → 5. |
| `database_latency_saturation` | DB pool exhausted (100/100); p99 wait 2.8s. |
| `dns_resolution_failure` | CoreDNS Corefile reload dropped internal zone. |
| `k8s_pod_crashloop` | Container OOMKilled — memory limit below live working set. |

Each ships with a `mock_investigation_output` that scores **1.0** on
every dimension when SentinelBench is run against the mock. This lets
CI assert *"the scoring math is stable and every scenario is well-formed"*
without invoking `investigate()`.

## Sample report

See `docs/architecture/sentinelbench_sample_report.json`. Full corpus,
default weights, mock outputs. Overall mean, min, max all 1.0.

## Isolation guarantees (tested)

- No import of `requests`, `httpx`, `urllib3`, `boto3`, `openai`,
  `anthropic`, `kubernetes`, or `prometheus_api` in any SentinelBench
  module (verified by test).
- No import of `supervisor.agent` (verified by test).
- No network access. No filesystem writes outside `tmp_path` in tests.

## Extension model

### Add a new scenario
1. Create `tests/synthetic/scenarios/<scenario_id>.json`.
2. Populate every required field (see schema above).
3. Include a `mock_investigation_output` that scores 1.0 on every
   dimension (so the CI regression tests keep the corpus green).
4. `pytest tests/synthetic/test_synthetic_schemas.py` will discover
   it automatically via parametrisation and validate the schema.

### Add a new scoring dimension
1. Add a `score_<dimension>` function in `scoring.py`.
2. Append its key + weight to `DEFAULT_WEIGHTS` (make sure weights
   still sum to 1.0).
3. Extend `ScoreCard` with the new field.
4. Extend `score_investigation` to compute it.
5. Extend `render_report` per-dimension mean.

### Add a new scoring weight profile
Pass `weights=` explicitly to `score_investigation`, `run_scenario`, or
`run_all_scenarios`. Missing keys inherit `DEFAULT_WEIGHTS`.

## Benchmark roadmap

| Milestone | Description |
|-----------|-------------|
| **Corpus expansion** | Grow to 25+ scenarios spanning k8s, cloud, network, auth, storage, deployment, cache, message-bus, TLS, secret-rotation, upstream-provider incidents. |
| **Replay from real receipts** | Import anonymised historical investigation receipts as scenarios; use `_intelligence.json` artifacts as expected-decision-signal ground truth. |
| **Planner effectiveness scoring** | New `planner_score` dimension — did the deterministic planner pick capabilities that would satisfy the scenario's `required_evidence`? Scored against the `InvestigationPlan` receipt metadata. |
| **KG / topology-aware scoring** | New `blast_radius_accuracy` and `topology_correctness` dimensions using the `KnowledgeGraph` receipt metadata against the scenario's ground-truth topology. |
| **MTTI trend tracking** | Persist run summaries as JSONL; compare rolling means; flag regressions in CI. |
| **RCA regression gate for CI** | GitHub Actions gate that fails PRs when overall_mean drops below a configurable threshold or any per-scenario overall drops below its `expected_confidence_range` lower bound. |

## Files delivered

| Path | Purpose |
|------|---------|
| `tests/synthetic/__init__.py` | Package marker + docstring |
| `tests/synthetic/schemas.py` | `Scenario` + `validate_scenario_dict` |
| `tests/synthetic/scoring.py` | 8 scoring functions + `ScoreCard` |
| `tests/synthetic/runner.py` | Loader + orchestrator |
| `tests/synthetic/report.py` | Deterministic JSON report renderer |
| `tests/synthetic/scenarios/__init__.py` | Package marker |
| `tests/synthetic/scenarios/*.json` | 5 initial scenarios |
| `tests/synthetic/test_synthetic_schemas.py` | Schema tests |
| `tests/synthetic/test_synthetic_scoring.py` | Scoring tests |
| `tests/synthetic/test_synthetic_runner.py` | Runner + report tests + isolation checks |
| `docs/architecture/sentinelbench.md` | This file |
| `docs/architecture/sentinelbench_sample_report.json` | Reproducible sample report |
