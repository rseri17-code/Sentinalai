# Final 90-Day Success Criteria
**Deliverable 14 · The bar the pilot must clear**

Success is **not** better reasoning, new features, or higher confidence. Success is a
production candidate that can **prove its own readiness from operational evidence** collected
over 90 days. The gate engine (G1–G11), not opinion, decides.

## Hard criteria (all must hold on the held-out corpus)
1. **Corpus (G1):** ≥ 500 labeled investigations, ≥ 20 per incident class, with a
   train/eval/held-out split and enforced leakage controls.
2. **RCA accuracy:** measured on the held-out set with n ≥ 30 per class and a reported CI —
   no `NOT_MEASURED`, no keyword-only proxy for the promotion decision.
3. **Safety:** shadow ↔ authoritative agreement ≥ 0.95 sustained; zero verified-incorrect
   attributable to a shadow signal.
4. **Determinism (every period):** PASS; **replay stability** 100% (byte-identical artifacts).
5. **Calibration (G-cal):** calibration error within the readiness threshold, n ≥ 30.
6. **Regression-clean:** no unresolved high-confidence regression in the final 4 weeks.
7. **Dependency health:** source availability and worker-failure rate within operational
   thresholds; all degradations were operator-visible (F-obs), none silent.
8. **All of G1–G11 pass** with measured evidence — the sole promotion authority.

## Trend criterion
`longitudinal_trends` must show **improving-or-flat** (never degrading) on rca_accuracy,
calibration_error, and shadow_authoritative_agreement across the 90-day window — proving the
candidate is *becoming* more trustworthy, not regressing.

## What success authorizes
Only a **recommendation** to the existing readiness program that a controlled authoritative
experiment may begin — under human sign-off. It does **not** by itself enable Wave 3,
runtime retrieval, or runtime authority; those remain governed by the readiness program.

## Explicit failure conditions (stop and document, don't expand scope)
- Any determinism or replay regression → halt, investigate, do not advance.
- Corpus stalls below the gate → verdict stays `READY_AFTER_MORE_SHADOW_EVIDENCE`.
- Accuracy or safety regresses on the held-out set → `NOT READY`, feed the failure taxonomy.
- Any temptation to "improve" reasoning to hit a number → **rejected as out of scope**; the
  pilot measures the candidate, it does not tune it.

## The one-sentence bar
> After 90 days, SentinelAI can answer "should I be given production authority?" with
> gate-backed, sample-sized, leakage-controlled evidence — and if the honest answer is
> "not yet," it says exactly that.
