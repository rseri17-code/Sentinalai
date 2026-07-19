# OVP Phase 1 — Real Operator Validation

**Role:** Principal SRE validating whether SentinelAI improves enterprise operations.
**Not** implementing features, **not** redesigning architecture — proving value with evidence.

> Central question: **do operators, using the frozen platform, run better operations —
> and do they trust it?** This document *designs* the validation and *executes* the part
> that can be executed offline, reporting `NOT_MEASURED` for everything that genuinely
> requires live operators. Nothing is invented; nothing is scored anew.

Platform under validation is **frozen** (Investigation Engine · R1 runtime · Frozen Corpus ·
Hermetic Replay · Evidence/Confidence Provenance · ODE · OIP #1–#5). No architectural change
is permitted during validation unless a **verified production defect** requires it.

---

## 0. Honest execution boundary (read first)

This environment has **no live operators and no incident-response timeline**. Therefore:

- **Every operator-outcome metric is `NOT_MEASURED`** — MTTI, investigation duration,
  time-to-owner, time-to-evidence, time-to-decide, operator confidence, operator trust,
  recommendation acceptance, repeat-investigation rate. These are *designed and instrumented*
  below but cannot be produced without a pilot. **They are not estimated or simulated.**
- **Platform-side properties that need no human are measured now** from real committed
  artifacts (`eval/ovp/measure_phase1_baseline.py` → `eval/ovp/phase1_measured_baseline.json`).
- Even the machine-measured numbers are **underpowered**: the real corpus is `n = 3` gold
  records / `n = 6` EIC tasks / `16` actionable OIP items — all far below the `n ≥ 30`
  minimum this program sets. They are **provisional**, reported with that flag, never as a pass.

This boundary *is itself a validation finding*: the platform can prove determinism,
verifiability, and traceability offline, but **operational value must be earned in a pilot.**

---

## 1. Pilot Plan

| Parameter | Definition |
|---|---|
| **Duration** | 6 weeks. Weeks 1–2 onboarding + baseline (SentinelAI observed, not consulted); weeks 3–6 assisted (operators may consult OIP surfaces). Two-arm within-subjects. |
| **Participating teams** | 2–3 on-call rotations spanning distinct domains (e.g. payments, edge/CDN, platform). Minimum **6 operators** across seniorities so trust is not read off one persona. |
| **Incident types** | The classes the engine already covers: `saturation`, `timeout`, `deploy/regression`, `network/dns`, `cascade`, `k8s`. Mirrors `eval/eic/tasks/` and `ground_truth.json`. |
| **Inclusion** | P1–P3 incidents with a written human RCA (the authoritative label); ≥1 service owner identifiable; evidence sources reachable at investigation time. |
| **Exclusion** | Security incidents; incidents with no reproducible evidence; incidents where SentinelAI has no corpus coverage (report as coverage gap, don't force a verdict). |
| **Success criteria (program)** | On a powered, leakage-controlled corpus: statistically significant improvement on ≥1 **primary** KPI (MTTI, root-cause precision, evidence completeness) **without regression** on **safety** KPIs (false-positive rate, verified-incorrect rate), **and** operator-trust above the go/no-go threshold (§7). |

**Read-only, no authority.** SentinelAI runs alongside the human investigation; it takes no
action and does not gate response. Wave 3 stays OFF. This matches the frozen contract.

---

## 2. Evaluation Dataset

| Unit | Present now (real) | Minimum before conclusions |
|---|---|---|
| Incidents (labelled) | `ground_truth.json` **n=3** | **≥30** per primary KPI; **≥15** per incident class for per-class claims |
| Gold investigations | `eval/gold_standard` **n=3** (`underpowered=true`) | ≥30 |
| EIC benchmark tasks | `eval/eic/tasks` **n=6** | ≥30 (engine-agnostic arm) |
| Applications | pilot-supplied | ≥5 distinct owners |
| Services | pilot-supplied | ≥15 |
| Recurring failures | ODE recurrence index | ≥5 distinct recurring causes with ≥2 occurrences |

**Leakage control:** freeze the corpus (`corpus_version`) *before* the pilot window; label RCA
independently of SentinelAI output; hold out incidents post-freeze for the assisted arm.
Minimum sample sizes are **gates**, not targets: below them, the KPI reports `NOT_MEASURED`.

---

## 3. Operator Workflow (observed, platform unchanged)

The operator consumes the five surfaces in the order the shift actually needs them. Observation
is captured out-of-band (screen-share notes + timestamps), **never** by modifying the platform.

1. **Daily Operations Brief** — at shift start. *What changed overnight? What do I own first?*
   Observe: does the operator act on the top `highest_priority_actions` item? Do they trust
   `verification_status`?
2. **Operational Health** — triage. *Which service is worst right now, and why?*
   Observe: does `attention_order` match where they'd have looked unaided?
3. **Service Reliability** — deep dive on a flagged service. *Reliable? Improving or degrading?*
   Observe: does `reliability_direction` change their prioritisation?
4. **Incident Trends** — pattern check. *Is this recurring? Getting worse?*
   Observe: does `what_is_recurring` surface something they'd have missed?
5. **Application Health** — owner view / handoff. *Is my app at risk; who owns the driver?*
   Observe: does `owner` + `driving_incidents` speed the handoff?

Each observation records: task, wall-clock start/stop, whether the operator followed the
recommendation, and a one-line reason. This is the raw material for §4 and §5.

---

## 4. Metrics

Primary (P), Safety (S), Experience (E). Baseline = unaided arm; target = assisted arm.

| Metric | Type | Source (existing) | Status now |
|---|---|---|---|
| MTTI | P | shift timestamps vs incident open | `NOT_MEASURED` |
| Investigation duration | P | start/stop capture | `NOT_MEASURED` |
| Time to identify owner | P | owner-ID timestamp vs Application Health | `NOT_MEASURED` |
| Time to identify evidence | P | evidence-open timestamp vs `_evidence_lifecycle` | `NOT_MEASURED` |
| Time to decide next action | P | decision timestamp vs `highest_priority_actions` | `NOT_MEASURED` |
| Root-cause precision | P | RCA vs `ground_truth` (keywords) | provisional `n=3` |
| Evidence completeness | P | `_evidence_lifecycle` used/total | provisional (see §8) |
| Operator confidence | E | survey (§5) | `NOT_MEASURED` |
| Operator trust | E | survey (§5) | `NOT_MEASURED` |
| Recommendation acceptance | P | followed? per observation | `NOT_MEASURED` |
| Repeat-investigation rate | P | ODE recurrence of same cause post-fix | `NOT_MEASURED` |
| False-positive rate | S | verified-incorrect RCA | provisional `n=3` |
| **Recommendation traceability** | S | committed OIP outputs | **measured: 1.0** (§8; gap closed in pilot readiness) |
| **Verifiability** | S | R1 corpus stamp | **measured: 1.0** (§8) |
| **Determinism** | S | recompute byte-equality | **measured: CONFIRMED** (§8) |

Report 95% CIs where n permits; below `n≥30`, report `NOT_MEASURED` (or `provisional`,
`underpowered=true`) — the gold evaluator already does exactly this and its posture is honoured.

---

## 5. Operator Feedback (structured questionnaire)

Per incident (Likert 1–5 + free text), then an exit interview. Kept short to bound cognitive load.

| Dimension | Item |
|---|---|
| Usefulness | "The surface I used changed what I did next." |
| Clarity | "I understood the output without needing internals explained." |
| Trust | "I believed the evidence behind the recommendation." |
| Missing information | "What did you need that wasn't there?" (free text) |
| Confusing outputs | "What was ambiguous or misleading?" (free text) |
| Workflow fit | "It fit how I actually work on shift." |
| Cognitive load | "It reduced, not added to, my mental effort." |

Trust is the go/no-go gate (§7). Free-text drives §6 categorisation. No numeric score is
fabricated pre-pilot; the instrument is defined, results are `NOT_MEASURED` until run.

---

## 6. Gap Analysis (categorised)

Findings are filed as **Proven Strength / Operational Improvement / Product Improvement /
Documentation Improvement / Defect**. No architectural change is proposed unless a **Defect**
requires it. Findings available *now* from offline execution:

| Finding | Category | Evidence |
|---|---|---|
| Output is deterministic and byte-reproducible | **Proven Strength** | recompute equality + full regression |
| Every conclusion carries an R1 corpus stamp (verifiability = 1.0) | **Proven Strength** | §8 verifiability |
| 16/16 actionable items cite supporting incidents (was 15/16) | **Proven Strength** | §8 traceability |
| `service_health_decline` actions shipped with an empty evidence list | **Defect → FIXED** (pilot readiness) | now carries the declining service's incidents; regenerated samples show 16/16 |
| Real labelled corpus is `n=3` — every outcome metric underpowered | **Operational Improvement** | §2; gold `underpowered=true` |
| Operators/timeline absent → no MTTI/trust evidence | **Operational Improvement** | §0 boundary |

The traceability gap traced to `incident_trends._investigate_first`, where the
`service_health_decline` branch set `evidence: []`. It has since been **fixed during pilot
readiness** (declining-service entries now carry the incidents behind them), taking measured
traceability from 15/16 to **16/16**. See `docs/pilot/GO_NO_GO.md` §Defect Review.

---

## 7. Release Recommendation

Classification is **evidence-gated**. On the evidence available now:

> **Requires targeted improvements → then Expanded Pilot.**

Justification, strictly from observed evidence:

- **What is proven (offline):** determinism, verifiability (1.0), and complete
  recommendation traceability (1.0, after the pilot-readiness fix) — the *integrity* properties
  an operator must be able to rely on. These clear the bar for a **supervised pilot**.
- **What is unproven:** every *operational-outcome* and *trust* claim is `NOT_MEASURED`. There is
  no evidence yet that operators are faster, more accurate, or more confident with SentinelAI.
- **Therefore:** not "Ready for limited production" (no outcome evidence), not "Not ready"
  (integrity properties hold). The honest position is **run the pilot in §1** to convert
  `NOT_MEASURED` into measured outcomes, and close the one traceability gap first.

Go/No-Go to expanded deployment requires: primary-KPI improvement significant at `n≥30`, no
safety regression, and mean operator-trust ≥ 4/5.

---

## 8. Executed baseline (machine-measurable, real artifacts)

Produced by `eval/ovp/measure_phase1_baseline.py` → `eval/ovp/phase1_measured_baseline.json`.
Reads only committed OIP outputs + the gold evaluation; calls only existing OIP services.

| Property | Value | Note |
|---|---|---|
| Recommendation traceability | **16/16 = 1.0** | gap closed in pilot readiness (was 15/16). `underpowered=true` by sample size |
| Verifiability (R1 corpus stamp) | **1.0** (all units) | daily-brief `verification_status`: verifiable, 6/6 stamped |
| Determinism | **CONFIRMED** | all 5 services byte-identical on recompute |
| RCA-side (gold IQS) | IQS 0.818 @ coverage 1.0 | **all 10 metrics `n=3`, `underpowered=true`** — provisional |
| Operator outcomes | **NOT_MEASURED ×9** | requires pilot instrumentation (§4) |

**Interpretation:** the platform's *self-integrity* is measurably sound; its *operational value*
is unproven and must come from §1. The measured numbers are provisional by sample size and are
labelled as such — consistent with the gold evaluator's own honesty flags.

---

## Non-negotiables honoured

No features invented · no architecture redesigned · no OIP expansion · no new engines · no new
AI models · no new scoring. This phase measured value; it did not add capability. The single code
artifact added (`eval/ovp/measure_phase1_baseline.py`) is a **produce-only, read-only** harness
imported by no runtime path.

## Success criteria — status

| Question | Answer today |
|---|---|
| Does it improve operations? | **NOT_MEASURED** — pilot required |
| Do operators trust it? | **NOT_MEASURED** — instrument defined (§5) |
| Does it reduce investigation effort? | **NOT_MEASURED** |
| Which capabilities provide the greatest value? | **NOT_MEASURED** — hypothesis: Daily Brief + Service Reliability (§ recommendation to test first) |
| What to improve before broader deployment? | **Measured:** close the `service_health_decline` traceability gap; power the corpus to `n≥30` |

**The highest-value next move is to run the pilot — not to build OIP #6/#7.** Let measured
operator evidence, not architectural preference, decide what gets built next.
