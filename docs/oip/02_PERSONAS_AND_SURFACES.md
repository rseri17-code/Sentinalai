# OIP — Personas & Knowledge Surfaces
Six roles ask different questions of the **same** investigation evidence. Surfaces prioritize
**actionability over dashboards**: each item is an insight + evidence + next action + confidence,
not a chart. All powered by the control-plane services (`01_ARCHITECTURE.md`); no new data.

## The decision-support contract (every surface item)
```
INSIGHT: <one sentence, outcome-framed>
  Why it matters: <operational/business consequence>
  Evidence: <investigation artifact ids + ODE discovery id + metric with CI>
  Next action: <concrete, owned>
  Confidence: <0–100 + reproducible? (corpus_version) + sample size>
```

## Personas × questions × primary services
| Persona | Core question | Primary services |
|---|---|---|
| **OCC operator** | What needs attention right now? | Operational Health, Incident Trend |
| **SRE** | Why is my service unreliable and what's the root, not the symptom? | Service Reliability, Engineering Debt, Recurring Failure |
| **Platform engineer** | Which estate-wide patterns/dependencies are emerging? | Knowledge Growth, Operational Risk, Change Risk |
| **Application owner** | How healthy is my app and what should I fix? | Application Health, Engineering Debt |
| **Engineering manager** | Where is my team spending effort vs. value? | Engineering Debt, Operational Maturity, Incident Trend |
| **Executive** | Is operations getting better; where to invest? | Executive Insights, Operational Maturity |

## Knowledge surfaces by cadence
Actionability rises with cadence: daily = triage, quarterly = investment.

### Daily
- **OCC operator:** live health deltas — services whose health/completeness dropped since
  yesterday, each with the triggering investigation + "sources unavailable?" flag.
- **SRE:** new incidents on my services with the shadow investigation (root vs symptom
  localization) + reproducibility stamp.

### Weekly
- **SRE / App owner:** *"symptom vs cause"* report — incidents where I patched a downstream
  victim while the root sat elsewhere (Engineering Debt), with the causal chain.
- **Platform engineer:** *"what we learned this week"* — new ODE discoveries (dependencies,
  decisive-evidence shortcuts, recurring false leads) with recurrence + CI.
- **All:** weekly certification report (OVP `03`) — primary KPIs with CIs, regressions.

### Monthly
- **Engineering manager:** team operational profile — recurring failure modes, repeat-incident
  rate, effort-on-symptoms ratio, MTTI trend.
- **Platform / Risk:** *"services becoming risky"* — leading indicators (rising recurrence,
  declining completeness/confidence) before SLOs breach.
- **Change Risk:** the month's highest-impact changes, ranked by measured blast radius.

### Quarterly
- **Executive:** Operational Maturity curve — is investigation quality (IQS), knowledge growth
  (ODE), and precision improving? Plus the *"which applications deserve engineering investment
  next quarter"* ranking (Application Health × incident trend × debt).
- **Leadership:** operational-knowledge inventory — *"what do we know about this estate now
  that we didn't 6 months ago?"* (ODE longitudinal), and the decisions that knowledge unlocks.

## Design principles for surfaces
1. **Insight-first, chart-second.** Lead with the sentence + action; the chart is supporting.
2. **Every claim is drill-downable** to the investigation artifact (auditable, replayable).
3. **Confidence + reproducibility travel with every item** (inherit R1/R2); low-sample items
   are labeled, never hidden.
4. **Role-scoped, evidence-shared.** The same artifact powers the OCC "needs attention" and the
   exec "invest here" — different framing, one source of truth.
5. **No vanity metrics.** Every surfaced number maps to a decision; `NOT_MEASURED` is shown, not
   filled.
