# II-1 — EnterpriseBench-Guided Investigation Intelligence (Loop 1)

One EnterpriseBench-measured optimization loop. The objective is investigation
quality, not features — every change is judged by EB-2, kept only if it improves,
generalizes, and regresses nothing.

## Phase 1 — Measure (baseline)

EB-2 over all 30 EFIC scenarios with the IE domains enabled (DNS+Identity+AWS —
the engine's fullest current capability). Mean EIC **0.3325**; per-dimension means:

| dimension | mean | note |
|---|---|---|
| rca_correctness | 0.167 | 5/30 solved (IE domains) |
| localization | 0.967 | strong |
| false_lead_avoidance | 0.000 | zero across all 30 |
| decisive_evidence_latency | 0.000 | zero across all 30 |
| evidence_efficiency | 0.000 | zero across all 30 |
| distractor_avoidance | 1.000 | strong |
| hypothesis_quality | 0.000 | zero across all 30 |
| confidence_calibration | 0.567 | |
| explainability | 0.517 | |
| replayability | 1.000 | |

## Phase 2 — Diagnose (measured, not assumed)

- **The four zero dimensions are a vocabulary/representation artifact, not purely a
  reasoning gap.** The scorer compares the submission's `evidence_used` (engine
  step-labels: `check_changes`, `dns_evidence`) and `hypotheses` (codenames:
  `dns_resolver_outage`) against ground-truth **MCP source names** and the
  **root-cause sentence**. The engine's decision-intelligence attributes evidence by
  label, and the serialized hypothesis graph drops the winning hypothesis's
  `root_cause` text — so even correctly-solved scenarios score 0 on
  hypothesis_quality. Fixing this is a reporting-fidelity change to the
  SentinelAI→EIC boundary, not an investigation improvement; deferred.
- **The dominant lever is `rca_correctness` (weight 0.30, at 0.167).** 25 failures
  produce generic root causes. Root cause of the failures: the summary-based
  classifier routes most incidents to `error_spike` (default), so the specific
  analyzer never runs. Example: `EFIC-K8S-OOM-001` ("payment pods restarting, 5xx
  rising") classifies as `error_spike` on "5xx", even though its pods were
  **OOMKilled** — the existing `_analyze_oomkill` would produce "memory leak …
  OOMKill", matching the ground truth, if only it ran.

## Phase 3 — Rank by ROI

Highest-ROI clean, general, non-fitting reasoning improvement: **evidence-driven
reclassification** — trust an unambiguous decisive-evidence signal over the alert
title (core SRE behavior), reusing the engine's existing analyzers. The remaining
~22 failures have failure modes with no corresponding analyzer/incident-type
(deadlock, slow_query, config_drift, imagepullbackoff, replica_lag, …) — those
require new per-mode analyzers (architectural work), not a reasoning tweak.

## Phase 4 — Implement ONE optimization

`_reclassify_from_evidence` (behind `II_RECLASSIFY_ENABLED`, default off): after
collection, if the collected events/logs contain an **OOMKilled** marker and the
incident was not already classified `oomkill`, re-route to `oomkill` so the
existing `_analyze_oomkill` runs. Conservative: only definitive, universal
production markers; only re-routes to an incident type whose analyzer exists; never
adds EFIC-specific signatures.

Files: `supervisor/agent.py` (flag + `_reclassify_from_evidence` + one call site in
`_analyze_evidence`). **Rollback:** flip the flag off (default) — or revert the
single additive commit.

## Phase 5/6 — Measure & compare

| | baseline | +reclassify |
|---|---|---|
| mean EIC | 0.3325 | **0.3425** (+0.010) |
| K8S-OOM-001 rca / eic | 0.0 / 0.29 | **1.0 / 0.59** |
| scenarios changed | — | **only EFIC-K8S-OOM-001** |
| determinism | — | `b4cbd2f13871982c` (×2, identical) |
| all-IE-off hash | `3e277b87d9bc31db` | `3e277b87d9bc31db` (byte-identical) |

Improved, deterministic, zero regression → **kept**.

## Honest assessment

The **mechanism** generalizes (any incident whose pods were OOMKilled but whose
alert says otherwise is re-routed); the EFIC corpus happens to contain exactly one
such case, so the corpus-wide gain is small (+0.010). This is not benchmark-fitting
— "OOMKilled → memory exhaustion" is universal SRE knowledge, and the analyzer it
routes to already existed. It does not overfit: it fires on a real signal, changes
no other scenario, and the flag-off engine is byte-identical.

What this loop proves: the biggest remaining EnterpriseBench deficit
(rca_correctness on ~22 scenarios) is **architectural** — those failure modes have
no analyzer — not a reasoning-tuning problem. Per the loop's exit criteria, that is
a STOP condition (remaining failures require architectural work), not a
diminishing-returns-on-tuning condition.

## Next highest-ROI (for a future architectural phase, not this loop)

Add per-failure-mode analyzers for the highest-frequency unanalyzed families
(database: deadlock/slow_query/replica_lag; kubernetes: crashloopbackoff/
imagepullbackoff), each flag-gated and EB-2-validated — this is the only lever that
moves `rca_correctness` materially, and it is engineering (new analyzers), not
reasoning tuning.
