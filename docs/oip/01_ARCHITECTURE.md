# Operational Intelligence Platform (OIP) — Architecture
**Phase 3 · Enterprise Decision Layer. Design only — no implementation, no new reasoning.**
Investigation is a stable subsystem, treated as an API. Everything below **composes existing
services**; nothing is rebuilt.

> Reframe: not "AI RCA", not "incident assistant" → **Enterprise Operational Intelligence**.
> The investigation engine answers *"why did this incident happen?"*. The OIP answers the
> questions leadership actually asks: *what changed across my estate, what's getting risky,
> what did we learn, where should we invest?*

## The one architectural idea
Every investigation already emits an **append-only, content-addressed, replayable artifact**
carrying: root_cause + `_confidence_provenance` (R2), `_evidence_lifecycle` (R2),
`corpus_stamp`/`_corpus_version` (R1), `_replay_verification` (R1), hypotheses/elimination,
causal localization (root vs symptom service), decisive evidence, and receipts (MTTI).
Alongside them: ODE discoveries, IQS scores, shadow-pilot scorecards, scientific-validation
reports. **The OIP is a read/aggregation layer over that corpus.** It performs *no*
investigation and *no* reasoning — it rolls up, trends, and ranks existing evidence into
role-specific decisions.

```
                         ┌──────────────── OIP CONTROL PLANE (read/aggregate only) ─────────────┐
  Investigation API      │ Operational Health · Operational Risk · Knowledge Growth ·           │
  (stable subsystem) ───▶│ Recurring Failure · Engineering Debt · Incident Trend · Change Risk ·│──▶ role surfaces
   emits artifacts       │ Executive Insights · Application Health · Service Reliability ·       │   (OCC/SRE/PE/AO/EM/Exec)
   + ODE/IQS/scorecards  │ Operational Maturity                                                 │
                         └──────────────────────────────────────────────────────────────────────┘
        │  reads only ▲                                          every insight carries:
        └── append-only investigation-artifact + eval store ──── why · evidence · next action · confidence
```

## Data contract (what the platform reads — all existing)
| Source (existing) | Field consumed | Powers |
|---|---|---|
| Investigation artifact | root_cause, causal localization, decisive evidence, receipts (MTTI) | Health, Reliability, Change Risk |
| `_confidence_provenance` (R2) | attributable confidence | every insight's "how confident" |
| `_evidence_lifecycle` (R2) | used/filtered/unavailable/error | Health, data-quality |
| `corpus_stamp` + replay (R1) | reproducibility | audit / trust of every insight |
| `ode.run_discovery` | discoveries, recurrence, false leads, new dependencies | Knowledge Growth, Recurring Failure, Risk |
| `gold_standard` IQS | investigation quality | Maturity, Health |
| `shadow_pilot` / `scientific_validation` | KPIs, calibration, safety | Maturity, Exec confidence |
| Incident metadata (class, service, severity, timestamp) | volume, trend | Incident Trend |

## The control-plane services (design — do NOT implement)
Each is a pure aggregation over the store. **Decision-support contract** — every service answers,
for every insight: **Why does this matter · What evidence supports it · What should be done next ·
How confident is SentinelAI** (with reproducibility + sample size).

| Service | Executive/SRE question | Composes | Output insight |
|---|---|---|---|
| **Operational Health** | Which services are healthy right now? | investigation outcomes + `_evidence_lifecycle` + IQS per service | per-service health score + trend |
| **Operational Risk** | Which services are becoming risky? | ODE failure clusters + incident trend + confidence/completeness decline | ranked risk list, leading indicators |
| **Knowledge Growth** | What did we learn this week? | ODE discoveries longitudinal (strengthened/weakened/new) | new dependencies, decisive-evidence shortcuts |
| **Recurring Failure** | Which failure patterns are increasing? | ODE recurring clusters + recurrence_index + recurring false leads | recurring modes with support/CI |
| **Engineering Debt** | Who fixes symptoms not causes? | causal localization (root ≠ symptom service) + repeat incidents on the same root | teams/services repeatedly patching downstream victims |
| **Incident Trend** | How is incident volume/mix moving? | incident metadata over rolling windows | per-class trend, seasonality |
| **Change Risk** | Which changes caused the most impact? | deployment/change correlation + blast radius from investigations | ranked changes by measured operational impact |
| **Executive Insights** | What must leadership know? | top-line rollup of all above | 5 things + decisions requested |
| **Application Health** | How healthy is *this* app? | per-application rollup of Health + Reliability + Debt | app scorecard |
| **Service Reliability** | Reliability of *this* service? | MTTI/MTTR + investigation outcomes per service | reliability index + SLO context |
| **Operational Maturity** | Are we improving over time? | IQS + KPI trends + knowledge growth longitudinally | maturity curve |

## What the OIP explicitly does NOT do
- No new reasoning/investigation logic (investigation is an API).
- No duplication: Change Risk reuses the investigation's existing change-correlation +
  blast-radius; it does not re-derive them. Risk/Recurring reuse ODE; it does not re-mine.
- No authority, no Wave 3, no action-taking — it is a **decision-support** layer, read-only
  over an append-only, replayable corpus, so every insight is itself auditable and
  reproducible (inherits R1/R2).

## Why this is composition, not new build
Every OIP output is a `GROUP BY service/change/class/time` + rank over fields that already
exist on investigation artifacts and ODE/IQS/scorecard outputs. The platform's value is the
**cross-investigation aggregation and role-framing**, not new intelligence — which is exactly
the non-negotiable.
