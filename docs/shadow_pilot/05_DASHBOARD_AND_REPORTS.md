# Operational Health Dashboard + Report Templates
**Deliverables 6, 9, 13**

## Deliverable 9 — Operational Health Dashboard Specification
A read-only dashboard rendered from the produce-only scorecards (no runtime coupling).

Panels (each computed, each with sample size):
1. **Readiness Gates** — G1–G11 pass/fail grid with blocking reasons + next actions.
2. **Production Trust Index** — `(PTI, coverage)` pair with a coverage bar; PTI is greyed
   until coverage ≥ 0.8.
3. **RCA Accuracy** — labeled accuracy with CI, per incident class.
4. **Calibration** — evidence-confidence MAE vs correctness (labeled only).
5. **Shadow ↔ Authoritative Agreement** — safety line (target ≥ 0.95).
6. **Dependency Health** — source availability, worker failure rate, `unavailable_by_source`.
7. **Determinism / Replay** — PASS/REVIEW + replay-hash stability.
8. **Regression Watch** — active regressions with reason/first/last/action.
9. **Trends** — per-metric slope/verdict across weeks.

Data source: the JSON emitted by `production_scorecard`, `chaos_observation`,
`regression_watch`, `longitudinal_trends`. Refresh cadence: per reporting period. The
dashboard displays `NOT_MEASURED` verbatim; it never fills gaps.

## Deliverable 6 — Weekly Certification Report (template)
```
SentinelAI Shadow Pilot — Certification Report — Week <N> (<period>)
Investigations: <n>    Labeled: <k>    Commit: <sha>    Model: <ver>

Gatekeeper: <ALL_GATES_PASS|GATES_FAILING>    Wave-3: <READY|NOT_READY>
Production Trust Index: <pti> at <coverage> coverage
Determinism: <PASS|REVIEW>    Replay stability: <rate>

Metrics (value | n | ci95 | note):
  RCA accuracy ............ <..>
  Calibration error ....... <..>
  Shadow agreement ........ <..>
  Evidence validation ..... <..>
  Citation coverage ....... <..>
  Completeness ............ <..>
  Decision quality ........ <..>
  Source availability ..... <..>

Open regressions: <list or none>
Open gate failures: <G-list + blocking reasons>
Remaining certification risks: <list>
Readiness trend: <improving|flat|degrading per metric>
```

## Deliverable 13 — Weekly Executive Report (template)
```
SUBJECT: SentinelAI Shadow Pilot — Week <N> Executive Summary

Verdict this week: <NOT READY — accumulating evidence | READY (gates pass)>
Are we becoming more trustworthy? <yes/no, with the trend that proves it>

Headline (measured):
  • RCA accuracy: <x>% (n=<k>, CI <lo–hi>)
  • Safety (shadow↔authoritative): <x>%
  • Determinism/replay: <PASS/rate>
  • Corpus toward the 500/20 gate: <total> / <per-class>

What changed vs last week: <regressions or improvements, evidence-backed>
What's blocking Wave-3: <the failing gates, in plain language>
Decision asked of leadership: <none | approve continued pilot | investigate regression>

Caveats: sample sizes, unmeasured dimensions, leakage controls in force.
```
