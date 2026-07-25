# OVP — Enterprise Scorecards & Reporting Templates
Outcome-focused, audience-tuned, computed from existing engines. No implementation detail.

## Enterprise Scorecard (executive / OCC leadership)
One page, outcomes only. Every cell is computed; blanks are `NOT MEASURED`, never guessed.
```
SentinelAI — Enterprise Operations Scorecard — <period>
Incidents investigated: <n>   Labeled: <k>   Coverage: <cov>% of sev1–3

OUTCOME                        SENTINELAI     HUMAN BASELINE    DELTA (CI)
Root-cause precision           <x>%           <y>%              <+z pts> (CI)
Mean time to investigate       <a> min        <b> min           <-c min> (CI)
Time saved / incident          <t> min                          (CI)
Evidence completeness          <e>                               —
False-positive rate            <f>%           <g>%              <delta>
Operator trust (1–5)           <r>                               —
Reproducibility                <100%>                            —

READINESS: <Go / Extend pilot>   GATES PASSING: <G-list>   PILOT DAY: <d>/90
Operational knowledge added this period: <ODE knowledge_count> discoveries
```

## SRE-manager scorecard (operational detail)
Adds per-class breakdowns + the evidence lifecycle + regression watch:
- Per incident class: precision, MTTI, completeness, n (underpowered flagged).
- Evidence lifecycle: used / filtered / suppressed / unavailable / error counts (R2) —
  proves no silent loss.
- Regression watch (`shadow_pilot.regression_watch`): any metric that moved adverse, with
  reason / first / last / affected / recommended action.
- Dependency health: source availability, worker failure rate.

## Platform-engineering scorecard (system health)
- Determinism: PASS/REVIEW; replay stability rate; `corpus_version` drift (should be 0 within
  a run).
- Shadow overhead (p50/p99 latency), throughput, error rate.
- Data quality: fraction of incidents with complete telemetry; malformed-response rate.

## Weekly Certification Report (template)
```
SentinelAI OVP — Weekly Report — Week <N> (<period>)
Investigations <n> | Labeled <k> | Coverage <cov>%
PRIMARY KPIs (value | n | CI | vs baseline):
  Root-cause precision .... 
  MTTI / time-saved ....... 
  Evidence completeness ... 
  Reproducibility ......... 
  False-positive rate ..... 
  Operator trust .......... 
IQS: <score> at <coverage> coverage
Regressions: <list or none>   Open gate failures: <G-list>
Determinism: <PASS>   Silent evidence loss: <0>
What we learned (ODE): <top discoveries, strengthened/weakened>
Readiness trend: <improving | flat | degrading>
```

## Monthly Executive Report (template)
```
SUBJECT: SentinelAI OVP — Month <M> Executive Summary
Is SentinelAI making operations materially better? <yes/no + the trend that proves it>

Headline (measured, with CIs):
  • Root-cause precision vs human: <+x pts>
  • Time saved / incident: <t min>   Coverage: <cov>%
  • False-positive rate: <f>% (vs human <g>%)
  • Operator trust: <r>/5
  • Reproducibility: <100%>   Determinism: <PASS>

Corpus toward the 500/20 gate: <total> / <per-class>
Operational knowledge (ODE): <net discoveries; recurring failure modes; new dependencies>
What's blocking wider rollout: <failing gates in plain language>
Decision requested: <none | approve broader read-only rollout | investigate regression>
Caveats: sample sizes, unmeasured dimensions, leakage controls in force.
```

## Data sources (all existing)
`shadow_pilot.quality_scorecard` / `production_scorecard` · `gold_standard.evaluate_dataset`
(IQS) · `scientific_validation.build_report` (E1/E2 + verdict) ·
`investigation_effectiveness.effectiveness_report` · `ode.run_discovery` · phase receipts +
`_evidence_lifecycle` + `_confidence_provenance` + `corpus_stamp`.
