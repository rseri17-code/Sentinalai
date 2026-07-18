# OIP — Phased Roadmap
Each phase delivers **visible enterprise value** and composes only existing services. No new
reasoning/investigation logic; no duplication. Gated on the OVP evidence (read-only pilot).

## Phase 3A — Aggregation & Rollup (make the corpus legible)
**Value:** leadership can see the estate through the investigation corpus for the first time.
- Stand up the **read/aggregation layer** over the append-only investigation-artifact + eval
  store (no writes, no reasoning). A query/rollup service that groups existing artifact fields
  by service / class / change / time.
- Ship the three foundational services that are pure rollups: **Operational Health**,
  **Incident Trend**, **Service Reliability**.
- Ship the **decision-support envelope** (why/evidence/action/confidence + reproducibility)
  as the uniform insight contract.
- Surfaces: OCC daily health deltas; SRE per-service reliability with root-vs-symptom
  localization.
- **Exit value:** "What changed across my estate today?" and "Which services are healthy?"
  answered from real investigations, each drill-downable + reproducible.
- **Depends on:** R1/R2 (done), OVP scorecards (done). **Risk:** low — pure composition.

## Phase 3B — Intelligence & Trend (from state to leading indicators)
**Value:** operations shifts from reactive to anticipatory; teams see cause vs symptom.
- Ship the services that compose ODE + trends: **Operational Risk** (leading indicators),
  **Recurring Failure**, **Knowledge Growth**, **Engineering Debt** (root≠symptom + repeats),
  **Change Risk** (existing change-correlation + blast radius, ranked).
- Surfaces: weekly "symptom vs cause" (SRE/app owner), "what we learned this week" (platform),
  monthly "services becoming risky" + highest-impact changes.
- **Exit value:** "Which services are becoming risky?", "Which teams fix symptoms not causes?",
  "Which changes caused the largest impact?", "What did we learn this week?" — all answered.
- **Depends on:** 3A + a populated ODE corpus (needs the OVP pilot's incident volume).
  **Risk:** medium — value scales with corpus size (honest: thin until the 90-day pilot fills it).

## Phase 3C — Executive Decision Layer (from insight to investment)
**Value:** the platform informs quarterly engineering investment and proves operations is
improving.
- Ship **Executive Insights**, **Application Health**, **Operational Maturity** — top-line
  rollups over 3A/3B outputs + IQS/KPI longitudinal trends.
- Surfaces: quarterly Maturity curve, "which applications deserve investment next quarter"
  ranking, operational-knowledge inventory ("what we know now that we didn't 6 months ago").
- **Exit value:** "Which applications deserve engineering investment next quarter?", "Is
  operations getting better?" — answered with trend + CI, auditable to source investigations.
- **Depends on:** 3A + 3B + longitudinal data (≥1–2 quarters). **Risk:** medium — requires
  sustained corpus + the OVP Go decision for broader read-only rollout.

## Sequencing rationale
3A is shippable immediately (composition over a corpus that already exists) and delivers
day-one legibility. 3B and 3C's *value* — not their code — depends on corpus depth, which the
OVP pilot provides; so the roadmap intentionally paces platform intelligence to evidence
accumulation rather than front-loading services that would report `NOT_MEASURED`.

## What stays out of scope (all phases)
No new reasoning engines, no new investigation logic, no runtime authority, no Wave 3, no
action-taking. Transaction intelligence and topology remain **future contributing inputs** to
the same control plane — added as new *read sources*, not new reasoning — when/if they exist.

## The strategic through-line
Phase 1 built a trustworthy investigation engine; Phase 2 (OVP) proves it improves operations;
Phase 3 (OIP) makes it the layer enterprises *run operations through* — where investigations
stop being one-shot explanations and become the evidence base for how engineering teams decide
what to fix, what's risky, and where to invest. That is the transition from an excellent
engineering project to a platform with lasting enterprise value — achieved entirely by
composing what already exists.
