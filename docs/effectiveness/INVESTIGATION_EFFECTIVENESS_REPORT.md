# Investigation Effectiveness Report (IEP)
**Produce-only value measurement · HEAD after `07c5125` · Full regression clean**
Engine: `sentinel_core/investigation_value/investigation_effectiveness.py` (offline,
deterministic, composes existing shadow outputs; no runtime/authority/Wave-3 change).

> The question is not "what did Investigation Intelligence produce?" but **"what
> measurable operational improvement would it have created?"** — answered from evidence,
> with `NOT_MEASURED` wherever ground truth is absent.

---
## 0. Reality Audit (Phase 0)
Every composed input verified present on completed investigations, nothing recomputed:
T1 `_hypothesis_graph` + `_elimination_narrative`; T2 `_adaptive_investigation`;
T3 `_causal_investigation.localization`; T4 `_investigation_validation`;
T5 `_decision_intelligence`. All are shadow-only and never touch root_cause/confidence.

## 1. Investigation Effectiveness Report (summary)
Run on the current labeled corpus (3 incidents, `eval/effectiveness/report.json`):
- **Shadow divergence from authoritative RCA: 0.0** — on every incident the shadow's
  independent winner *agreed* with the authoritative answer.
- **Localization-gain rate: 1.0** — T3 always localized deeper than the symptom service.
- **RCA-benefit verdict: INCONCLUSIVE** (0 labeled *divergent* outcomes at the 30-sample
  power floor).
- **Promotion: NO_TRANCHES.**

## 2. Counterfactual Investigation Report (Phase 1)
Per investigation, authoritative vs shadow re-derivation: `rca_relation`
(identical/divergent), `localization_gain`, `validation_would_gate`, and — only with
labels — `ground_truth_direction` (shadow_improved / shadow_worse / same). **On the
current corpus every case is `identical` with `direction = NOT_MEASURED`.** The honest
reading (consistent with the Independent Architecture Review): a shadow that agrees with
the authoritative answer contributes explanation, **not a different answer** — and there is
no evidence yet that it would produce a *better* answer.

## 3. Operational Benefit Attribution (Phase 2)
Per-tranche benefit level from measurable signal presence/strength:
| Tranche | Typical level (this corpus) | Evidence |
|---|---|---|
| Hypothesis (T1) | MAJOR | ≥2 hypotheses + elimination + winner survived disconfirmation |
| Adaptive (T2) | MODERATE | recommended next-best evidence (effort focus) |
| Causal (T3) | MAJOR | localized deeper than symptom + eliminated chains |
| Validation (T4) | MINOR | only ever *confirmed* (never gated a weak conclusion here) |
| Decision (T5) | MAJOR | identified decisive evidence |
**Caveat:** these measure *signal strength*, not proven RCA improvement.

## 4. Tranche ROI Analysis (Phase 3 + 5)
`decision_attribution` — how often each tranche produced a *differentiating* signal
(could change a decision) vs was decorative:
| Tranche | Verdict (n=3) |
|---|---|
| Hypothesis | CONSISTENTLY_MATTERS |
| Adaptive | CONSISTENTLY_MATTERS |
| Causal | CONSISTENTLY_MATTERS |
| Decision | CONSISTENTLY_MATTERS |
| **Validation** | **DECORATIVE_ON_CORPUS** (only confirmed; never gated) |
Benefit scores carry sample size + bootstrap CI + explicit limitations. All are
**underpowered (n<30)** — indicative, not conclusive.

## 5. Investigation Benefit Dashboard (Phase 3)
Panels (from `benefit_score` / `decision_attribution`): per-tranche benefit value+CI,
shadow-divergence rate, localization-gain rate, validation-gate rate, ground-truth
direction distribution. Every panel shows `n` and flags `underpowered`. Absent data →
`NOT_MEASURED`, never zero-filled.

## 6. Operator Value Assessment (Phase 4)
Each item explicitly tagged Measured / Projected / Not Measured:
| Would II have… | Status | Basis |
|---|---|---|
| reduced evidence review | **Projected** | T5 decisive-evidence + T2 next-best focus exist; effort delta unmeasured |
| improved localization | **Measured (signal)** | localization-gain rate 1.0 — but "correct localization" needs labels |
| prevented wrong RCA | **NOT MEASURED** | divergence 0.0; no case where II would have changed the answer |
| reduced investigation time / MTTI | **NOT MEASURED** | no timing counterfactual on real incidents |
| prevented unnecessary escalation | **NOT MEASURED** | requires operator outcomes |

## 7. Scientific Effectiveness Report (Phase 6)
**Verdict: INCONCLUSIVE.** Reason: 0 labeled *divergent* outcomes; the RCA-benefit
direction is `NOT_MEASURED` below the 30-sample power floor. Required additional evidence:
≥30 labeled outcomes (≥20/class), a held-out leakage-free corpus, and — critically —
incidents where the shadow *diverges* from the authoritative answer (a stack that always
agrees can never be shown to *improve* anything). Limitation: the stack is
non-authoritative, so even a future YES bounds *potential* benefit if promoted, not
realized benefit today.

## 8. Runtime Promotion Recommendations (Phase 8)
**Promote: NO_TRANCHES.** Fail-closed by the existing gate philosophy — no runtime
authority without measured, powered benefit evidence, which does not exist. *If* a future
powered corpus yields a YES verdict, the engine ranks the lowest-risk consistently-mattering
tranche first (Validation — it gates weak conclusions **without changing the winner**),
under read-only advisory gating, A/B-measured, flag-OFF rollback. Not now.

## 9. Retirement Recommendations (Phase 7)
**RETAIN_PENDING_EVIDENCE for all five tranches** — the corpus (n=3) is far too small to
retire anything; a `DECORATIVE_ON_CORPUS` signal at n=3 means "measure more", not "retire".
The one to *watch*: Validation (T4) has been decorative-on-corpus (only confirmed, never
gated). If, on a powered corpus, it never gates a weak conclusion, it becomes a
simplify/retire candidate. No retirement is justified today.

## 10. Final Verdict
> **Investigation Intelligence's operational value is currently UNPROVEN (INCONCLUSIVE).**

It produces coherent, differentiating *signals* (localization depth, decisive evidence,
hypothesis elimination) that are consistent and deterministic — but on the available
evidence it **agrees with the authoritative answer 100% of the time**, so there is **no
measured improvement in RCA, MTTI, localization correctness, or operator effort**, and **no
tranche has earned runtime authority**. This is not a negative result — it is an
*unmeasured* result, and the engine says so rather than inferring value. The single thing
that would change this verdict is the shadow pilot producing a labeled corpus that includes
incidents where the shadow *diverges* from the authoritative answer and is *shown to be more
correct*. Until then: keep all five tranches, promote none, run the pilot.
