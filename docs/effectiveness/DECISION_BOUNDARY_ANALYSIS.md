# Decision Boundary Analysis
**Produce-only diagnostic · composes existing shadow outputs · no runtime/authority change**
Engine: `sentinel_core/investigation_value/decision_boundary.py`.

> Purpose: not "what else can SentinelAI do?" but **"why does the shadow engine never
> diverge from the authoritative engine, and where — if anywhere — does it have leverage?"**

---
## Phase 0 — The authoritative decision boundaries (code audit)
Traced in `supervisor/agent.py::_analyze_evidence`:
| Boundary | Authoritative logic (file:line) | Shadow signal | Consumed today? |
|---|---|---|---|
| Hypothesis ranking → root_cause | `hypotheses.sort(-base_score); winner=[0]` (2343-2344) | T1/T5 re-rank | **No** |
| Confidence | `confidence = winner.base_score` (2348) + calibrate | T4 `evidence_confidence` | **No** |
| Localization | **none — baseline emits no `root_cause_service`** | T3 `localization` | **No (net-new)** |
| Validation gating | evidence_gates G2/G3/G5, own thresholds | T4 `verification_status` | **No — never consulted** |
| Decision arbitration | none | T5 `decisive_evidence` | **No (net-new)** |

## Why the shadow is identical to the authoritative engine (evidence-backed)
Of the five hypotheses the prompt listed, the analysis supports a specific combination:
1. **Structural (root cause):** the shadow **re-scores the baseline's own ranked candidate
   set** — the tranches consume `_hypotheses` (the base_score-ranked differential) and never
   generate independent candidates. So on the ranking boundary they can only diverge when
   evidence net-support *overturns* `base_score` — rare, because `base_score` already
   encodes the evidence. *(explanations #1 + #5)*
2. **Contractual:** every boundary is **non-authoritative** — the signals are wired to
   nothing, so they cannot influence the outcome even when they differ. *(explanation #2)*
3. **Evidential:** corpus n=3 is small/homogeneous and may not exercise flip cases.
   *(explanation #3)*
4. On the current corpus: **0 ranking divergences** — the baseline's top candidate already
   matches the shadow's evidence-ranked winner.

The prompt's explanation #4 (benchmark not exercising the right cases) is *plausible but
unconfirmed* — it collapses into #3 until a larger, more heterogeneous corpus exists.

## The prioritized promotion table (from `eval/decision_boundary/report.json`, n=3)
| Decision Boundary | Shadow Signal | Type | Potential Benefit | Risk | Recommendation |
|---|---|---|---|---|---|
| Validation gating | T4 | additive gate | (n=3: none fired) | **Very Low** | **SAFE FIRST CANDIDATE** — read-only gating, never changes the winner; A/B once labeled |
| Localization | T3 | net-new | **High** | Medium | SHADOW FIRST — capability the baseline lacks entirely; measure usefulness in pilot |
| Decision arbitration | T5 | net-new | **High** | Medium | SHADOW FIRST — decisive-evidence surfacing; no authoritative counterpart |
| Hypothesis ranking | T1/T5 | corrective | (n=3: 0 leverage) | Medium | MORE EVIDENCE — no divergence yet; label divergences before A/B |
| Confidence | T4 | corrective | (n=3: 0 leverage) | Medium | MORE EVIDENCE — no gap ≥15 observed yet |

## Two distinct answers (deliberately separated)
- **Where is the most leverage?** → **net-new boundaries** (localization, decision
  arbitration). The baseline emits *nothing* there, so the shadow *adds* capability rather
  than correcting it — inherently higher leverage and it can't "flip" a correct answer.
- **What should be promoted FIRST, minimizing risk?** → **Validation gating (T4)**. It is
  the only *very-low-risk* boundary: it can qualify/gate a weak conclusion **without ever
  changing the winner**. That is the safest possible first step into runtime authority.

## The key insight for the roadmap
The corrective boundaries (ranking, confidence) show **zero leverage** on this corpus —
promoting them would change nothing observable, so they are the *wrong* place to start.
The **net-new boundaries carry the real leverage**, because the baseline has no localization
or decisive-evidence output at all. And the **safest first promotion is validation gating**,
which adds a guardrail without touching the answer. This inverts the naive intuition that
you'd promote the "smartest" reasoning (ranking/arbitration) first.

## Recommended sequence (evidence-gated — nothing promoted now)
1. **Run the shadow pilot** to build the labeled corpus (still the binding constraint).
2. **First promotion candidate: validation gating (T4)** — read-only, very-low-risk, never
   changes root_cause; A/B behind a flag on a narrow incident class once ≥30 labeled
   outcomes exist.
3. **Then the net-new capabilities (T3 localization, T5 decisive evidence)** — surface to
   operators (shadow-first), measure whether they reduce MTTI / evidence review, promote
   only if the pilot shows benefit.
4. **Corrective boundaries (ranking, confidence) last** — only if the corpus ever exhibits
   divergences that are shown, against ground truth, to be *improvements*.

## Limitations
Leverage measures where a shadow signal *would* change or add to the authoritative decision
— **not** whether that change is an *improvement*. Improvement still requires labeled
outcomes (the pilot). Nothing here promotes anything; it prioritizes *where to look* so the
eventual A/B promotes the highest-leverage, lowest-risk capability first.
