# SCIENTIFIC_VALIDATION_AUDIT.md
**SentinelAI — Production Readiness Certification · Phase 6**
Adversarial audit of the evaluation metrics themselves. No runtime changes.

## Objective
Do the metrics measure what they claim? Find circular, proxy, leaky, or biased
metrics before they are used to justify promotion.

## Findings

### F-1 (HIGH) — "RCA improvement" is architecturally unmeasurable in shadow
The Investigation Intelligence stack is shadow-only: authoritative `root_cause`/
`confidence` are byte-identical ON vs OFF (enforced by the suite). Therefore any
metric of the form "treatment RCA accuracy − control RCA accuracy" is **structurally
zero**, not evidence of value. The Scientific Validation harness already states this
explicitly (`authoritative_rca_delta.note`) and does **not** claim improvement — good.
**Risk if ignored:** a reader could mistake "no regression" for "improvement." The
harness guards against this; keep the framing.

### F-2 (HIGH) — Ground-truth correctness is a keyword-proxy metric
`rca_correct()` and `sentinelbench.scorer` judge correctness by substring/keyword
majority against labeled keywords. This is a **proxy** for semantic correctness:
- False positives: a wrong RCA that happens to contain the keywords scores correct.
- False negatives: a correct RCA phrased differently scores wrong.
**Mitigation:** acceptable for automated regression, but promotion decisions must not
rest on keyword scoring alone — a human-adjudicated label set is required (backlog V-1).

### F-3 (MED) — Expert Concordance risks circularity
Tranche 4/5 "independent" re-derivation ranks hypotheses by the *same structured
evidence* the primary selection used (weights assigned by the hypothesis engine). It
deliberately ignores the LLM's base confidence, which breaks the *most* important
circular dependency — but it is not fully independent of the upstream evidence
weighting. **Interpretation:** concordance measures *evidence-ranking agreement*, not
an orthogonal second opinion. Documented as such in the code; do not over-read it as
external validation.

### F-4 (MED) — Confidence calibration is under-powered and can invert on tiny N
On the 3-incident corpus, `evidence_confidence_mae` (22.0) was *worse* than
`raw_confidence_mae` (20.0). This is **not** evidence that reconstruction is worse — N=3
with all-correct labels makes MAE dominated by the arbitrary 78-vs-80 point values.
The harness flags `underpowered`; the metric must not be cited until N ≥ 30 (backlog).

### F-5 (MED) — Selection/benchmark-leakage exposure
The labeled corpus (`eval/ground_truth.json`, 3 incidents) and SentinelBench scenarios
are the *same* fixtures used during development of the tranches. Evaluating on them
risks **benchmark leakage / confirmation bias**. The harness enforces a held-out
protocol structurally (unlabeled → NOT MEASURED) but there is currently **no held-out
partition** because the corpus is too small to split. Blocker for any statistical claim.

### F-6 (LOW) — Deterministic bootstrap is honest but not a true resample at small N
`bootstrap_ci` uses a caller-seeded LCG (replayable, correct choice for a deterministic
platform). At N<30 it correctly self-labels `underpowered`. No overclaim.

## Bias checklist
| Bias | Present? | Control |
|---|---|---|
| Circular metric | Partial (F-3) | evidence-only re-derivation ignores LLM prior; documented |
| Proxy metric | Yes (F-2) | keyword scoring; flagged, human labels required |
| Leaky / benchmark leakage | Yes (F-5) | dev fixtures == eval fixtures; needs held-out corpus |
| Confirmation bias | Guarded | NOT MEASURED where unlabeled; verdict caps at "more evidence" |
| Selection bias | Yes | 3 hand-picked incidents; not representative |
| Replay bias | Low | replay pins model output; determinism verified |
| Confidence inflation | Guarded (F-4) | underpowered flag; not cited at small N |

## Recommended validation improvements (no runtime changes)
1. **V-1** Build a human-adjudicated label set (≥500, ≥20/class) with a train/eval/
   held-out split; forbid eval fixtures from appearing in development.
2. **V-2** Add a semantic (not keyword) correctness adjudication step for promotion-grade
   scoring.
3. **V-3** Gate every statistical claim on `n_labeled ≥ 30` and `held_out=True` — already
   partially enforced; make it a hard error in the report builder.

## Verdict
The evaluation framework is **honest and self-limiting** — it refuses to claim
improvement it cannot support and marks gaps NOT MEASURED. Its metrics are **fit for
regression and safety gating, NOT yet fit for promotion decisions** until F-2/F-5 are
resolved with a real labeled, held-out corpus. No metric currently overclaims.
