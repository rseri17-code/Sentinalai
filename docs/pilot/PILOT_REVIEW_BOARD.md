# SentinelAI V1 — Pilot Review Board

**Independent review board** · Google Principal SRE · AWS Distinguished Engineer · Microsoft
Reliability Architect · Enterprise OCC Director · Staff PM · Principal UX Researcher · Principal
AI Systems Engineer.

**Charter:** answer one question — *does SentinelAI make enterprise operations measurably
better?* — using **only observed pilot evidence**. Where there is no evidence, the board is
required to state `NOT_MEASURED` or `NOT DEMONSTRATED` and **not speculate**. Architecture is
frozen; no OIP #6/#7, no new engines, no refactors.

---

## 0. Controlling finding — the pilot has not been executed

The board convened and inspected the evidence sinks that a supervised pilot would populate:

| Evidence source | Expected after a pilot | Observed now |
|---|---|---|
| `pilot_telemetry` operator events (`operator_interaction`, `recommendation_usage`, `operator_feedback`) | ≥1 per operator per incident | **0 events — no log exists** |
| Operator feedback questionnaires (OVP §5) | one per incident | **none** |
| Investigation timelines / MTTI capture | shift timestamps | **none** |
| Replay-usage records tied to operators | per deep-dive | **none operator-attributed** |

**No operators used the platform in this environment; no pilot period has occurred.** Therefore
every *operator-observed* dimension below returns `NOT DEMONSTRATED`, and every *operational-
outcome metric* returns `NOT_MEASURED`. This is not a soft finding — it is the board's central,
reproducible conclusion. The board declines to manufacture adoption, trust, or MTTI numbers from
an empty evidence set.

What **can** be reviewed is the offline, machine-measurable baseline
(`eval/ovp/phase1_measured_baseline.json`), which requires no operator. It is reported in §7 as
*product integrity*, explicitly distinguished from *operational value*.

---

## 1. Executive Summary

> **Does SentinelAI make enterprise operations measurably better? — `NOT_MEASURED`.**
> No supervised pilot has run, so no operational-outcome, adoption, or trust evidence exists.
> What *is* demonstrated, offline and without operators, is **product integrity**: the five OIP
> surfaces are deterministic, every conclusion is verifiable against a frozen corpus, and every
> recommendation is traceable to supporting incidents. Integrity is necessary but **not
> sufficient** to claim operational value. **Recommendation: execute the supervised pilot; do
> not ship, expand, or rank capabilities until operator evidence exists.**

Board lenses (each grounded only in what is observable):
- **SRE / Reliability (Google · AWS · Microsoft):** integrity properties clear the bar for a
  supervised, read-only trial; operational benefit is unproven → `NOT_MEASURED`.
- **OCC Director:** the Daily Operations Brief is *designed* for shift handoff, but **no shift
  has used it** → adoption `NOT DEMONSTRATED`.
- **Staff PM:** cannot rank capability value without usage → §6 is `NOT DEMONSTRATED`.
- **Principal UX Researcher:** zero sessions observed → no friction, wording, or click findings
  can be asserted → §Workflow `NOT DEMONSTRATED`.
- **Principal AI Systems Engineer:** determinism + verifiability + traceability are measured and
  hold; no trust/override behavior observed → trust `NOT DEMONSTRATED`.

---

## 2. Operational Metrics

| Metric | Result | Basis |
|---|---|---|
| MTTI | `NOT_MEASURED` | no operators, no incident timeline |
| Investigation duration | `NOT_MEASURED` | no operator-attributed sessions |
| Time to identify owner | `NOT_MEASURED` | no owner-ID timestamps captured |
| Time to identify evidence | `NOT_MEASURED` | no evidence-open timestamps captured |
| Time to decide next action | `NOT_MEASURED` | no decision timestamps captured |
| Recommendation acceptance | `NOT_MEASURED` | 0 `recommendation_usage` events |
| Replay usage | `NOT_MEASURED` | no operator-attributed replay |
| Repeat investigations | `NOT_MEASURED` | no post-fix recurrence window observed |
| Escalations avoided | `NOT_MEASURED` | counterfactual; needs a control arm |

Sample size for every metric = **0 operators / 0 incidents-in-pilot**. Below any threshold; the
gold corpus (n=3) remains underpowered for outcome inference as well.

---

## 3. Operator Adoption

- Which screens opened first? — `NOT DEMONSTRATED` (0 telemetry events)
- Which were ignored? — `NOT DEMONSTRATED`
- Which became daily workflow? — `NOT DEMONSTRATED`

No adoption can be inferred without a single recorded operator interaction.

---

## 4. Operator Trust

| Signal | Result |
|---|---|
| Confidence in recommendations | `NOT DEMONSTRATED` |
| Evidence trust | `NOT DEMONSTRATED` |
| Replay usage (trust proxy) | `NOT DEMONSTRATED` |
| Verification usage | `NOT DEMONSTRATED` |
| Operator confidence | `NOT DEMONSTRATED` |
| Recommendation overrides | `NOT DEMONSTRATED` |

The platform *exposes* the trust affordances (`verifiable`, `confidence`, evidence lifecycle,
replay), and the runbook makes `verifiable` the primary trust signal — but whether operators
**trust** them is a behavioral claim with no observations behind it.

---

## 5. Workflow Friction

Friction, unnecessary clicks, confusing terminology, duplicated information, missing context —
all **`NOT DEMONSTRATED`**. A UX finding requires an observed session; none exist. The board
will not infer friction from static output inspection, as that would not be operator evidence.

---

## 6. Product Value — capability ranking

**`NOT DEMONSTRATED`.** The board is asked to rank Operational Health · Incident Trends ·
Application Health · Service Reliability · Daily Operations Brief by measured value, "supported
by evidence." With zero usage there is no value signal to rank on. The board **refuses to
publish a ranking that would imply evidence it does not have.**

*Pre-registered hypothesis only (NOT a finding, NOT a roadmap input):* the OVP anticipated the
Daily Operations Brief and Service Reliability would show the most value. This is a hypothesis to
**test** in the pilot, not a conclusion. It carries no backlog weight (§Backlog).

---

## 7. Product Strengths (offline, machine-measured — integrity, not value)

These require no operator and are reproducible via
`python3 eval/ovp/measure_phase1_baseline.py`:

| Strength | Measure | Value |
|---|---|---|
| Determinism | recompute byte-equality across all 5 surfaces + full regression | **CONFIRMED** |
| Verifiability | fraction of units carrying an R1 corpus stamp | **1.0** |
| Recommendation traceability | fraction of actionable items citing supporting incidents | **1.0** (16/16, after the pilot-readiness D-1 fix) |
| Honest evidence lifecycle | `used`/`unavailable`/`error` surfaced, not hidden | present |

These are genuine, but the board stresses: **integrity ≠ operational value.** A deterministic,
verifiable recommendation that no operator reads or trusts has not improved operations.

---

## 8. Product Weaknesses

| Weakness | Evidence | Board position |
|---|---|---|
| Operational value entirely unproven | §2 all `NOT_MEASURED` | Blocks any "ready for production" claim |
| Corpus underpowered (n=3 gold / n=6 EIC) | gold `underpowered:true` ×10 | Blocks statistical conclusions; power via pilot |
| No operator-attributed telemetry captured yet | empty `pilot_telemetry` log | Pilot execution + capture is the prerequisite for every other finding |

None of these is a code defect. All are consequences of the pilot not having run.

---

## 9. Product Defects

**No new defects observed** — because no operator sessions exist to observe them. The one prior
verified defect (**D-1**, `service_health_decline` empty-evidence traceability gap) was fixed
during pilot readiness; the board confirms measured traceability is now **1.0** and the
regression test guards it.

| Defect | Severity | Evidence | Operator impact | Affected workflow | Reproduction | Minimal fix | Status |
|---|---|---|---|---|---|---|---|
| D-1 | Medium | baseline 15/16 pre-fix | one recommendation type was untraceable | investigate-first triage | pre-fix `investigate_first` `service_health_decline` had `evidence:[]` | attach declining service's incidents | **FIXED / verified 1.0** |

No Critical / High / new Medium / Low defects are demonstrable from current evidence.

---

## 10. Recommended Backlog (evidence-backed only)

Roadmap discipline: an item enters implementation only if **observed → reproduced → measured →
affects operational outcomes**. Against that bar:

| # | Item | Evidence | Class | Enters implementation? |
|---|---|---|---|---|
| B-1 | **Execute the supervised pilot and capture operator telemetry** (populate the empty `pilot_telemetry` log; run OVP §1). | §0 empty sinks | Program action (not a code change) | Prerequisite — unblocks everything |
| B-2 | **Power the labelled corpus to n≥30** before any outcome claim | gold `underpowered:true` | Program action | Gate, not code |

**No usability, wording, prioritization, or evidence-presentation backlog items are proposed** —
the board has **zero observed operator sessions**, so any such item would be speculation, which
the charter forbids. The backlog is deliberately empty of product-code changes.

---

## 11. Release Board verdict

| Question (success criteria) | Verdict |
|---|---|
| Reduces investigation effort? | `NOT_MEASURED` |
| Reduces MTTI? | `NOT_MEASURED` |
| Improves operational decisions? | `NOT_MEASURED` |
| Improves shift handoffs? | `NOT_MEASURED` |
| Increases confidence in investigations? | `NOT DEMONSTRATED` |
| Increases trust in AI-assisted operations? | `NOT DEMONSTRATED` |

**Board decision: `NOT_MEASURED` overall — remain in supervised-pilot readiness; execute the
pilot next.** The product is *ready to be evaluated* (integrity proven, defect fixed,
instrumentation in place, runbook + checklist published) but has *not yet been evaluated*. No
architecture change is warranted (no verified production defect); no capability may be ranked,
shipped, or expanded until the pilot yields operator evidence.

> **Final rule honoured:** operators determine the roadmap; evidence determines priorities.
> Today there is no operator evidence, so the only sanctioned next action is to run the pilot —
> not to build, rank, or refactor. The board will reconvene when telemetry and feedback exist.
