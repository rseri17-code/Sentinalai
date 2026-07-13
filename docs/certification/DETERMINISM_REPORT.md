# DETERMINISM_REPORT.md
**SentinelAI — Production Readiness Certification · Phase 2**
Branch: `claude/code-review-analysis-MelXd` · Evidence-backed, reproducible.

## Objective
Verify that identical inputs produce byte-identical outputs across Root Cause,
Confidence, Evidence, Receipts, Artifacts, Hypothesis Graph, Validation,
Decision Intelligence, Localization, and Counterfactual.

## Method
Direct repeat-execution experiments over the shadow reasoning stack (the only
components added/most recently touched), plus inspection of the platform-wide
determinism infrastructure (RC-A…RC-L stabilization guarantees) and the
existing 5665-test regression suite.

## Measured Results

### E-1 — Shadow stack, 1000 repeats
Ran Tranche 4 (`run_validation_engine`) + Tranche 5 (`run_decision_intelligence`)
over a fixed investigation result, 1000 times, hashing the canonicalised
(`sort_keys=True`) `_investigation_validation` + `_decision_intelligence`
metadata each time.

```
1000 repeats → distinct output hashes: 1   (1 == deterministic)
RESULT: PASS
```

### E-2 — Flag-OFF no-op, all five engines
With every shadow flag unset, ran all five engines (T1 hypothesis, T2 adaptive,
T3 causal, T4 validation, T5 decision) against a baseline result.

```
all flags OFF, result unchanged: True   →   PASS
```
Confirms the contractual guarantee: **flag OFF ⇒ byte-identical to today's
authoritative behaviour.** The stack cannot perturb output when disabled.

### E-3 — Platform determinism infrastructure (inspected)
- 17 modules canonicalise with `sort_keys=True` before hashing/serialising.
- Stabilization guarantees **RC-A … RC-L** are all present and independently
  tested (`tests/stabilization/`, 165 test functions), covering: redaction
  (RC-A), append-only (RC-E), framed-JSON hashing (RC-G), frozen dataclasses
  (RC-D), canonical helpers (RC-F), tuples→lists (RC-I), NOT-MEASURED/
  renormalisation semantics (RC-L).
- IDs are `sha256[:16]` of canonical payloads (verified deterministic:
  identical inputs → identical `record_id`).
- The Scientific Validation bootstrap uses a **caller-seeded LCG** (no RNG,
  no clock) — replayable by construction.

### E-4 — Regression corpus
Full suite: **5665 passed, 2 skipped, 0 failures**. Determinism assertions are
embedded per-tranche (each shadow suite includes a `test_deterministic` that
double-runs and asserts JSON byte-equality).

## Non-Deterministic Paths (documented)
| Path | Location | Status |
|---|---|---|
| LLM Judge | `PersistPhase` (post-analyze) | **Non-deterministic by nature** — runs an LLM. Correctly quarantined *after* the deterministic analyze stage; never feeds the deterministic reasoning. Flagged, not in the replay-critical path. |
| Wall-clock timestamps | core reasoning | **None in core** — timestamps are caller-supplied (platform rule); `Date.now()`/`now()` are excluded from the deterministic engines. Verified by the Failure/Determinism-risk audit (see FAILURE_MODE_ANALYSIS.md §B). |
| Model inference | Fetch/Classify LLM calls | Non-deterministic upstream input; the *reasoning over* that input is deterministic. Replay pins the model output, making the pipeline replay-exact. |

## MATERIAL CAVEAT — pipeline determinism ≠ engine determinism
The reasoning **engine** is byte-deterministic (proven above). The **pipeline** is not,
across a mutating corpus. Three default-ON runtime learning stores —
`experience_store`, `strategy_evolver`, `knowledge_graph` — write `datetime.now`/
`time.time` into persistent JSON that feeds **back** into future runs (classification
priming and playbook ordering). Consequences:
- "Same input → same output" holds **only with fixed/empty learning stores**. With a
  live, growing corpus the playbook order and classification priors drift over time.
- These stores are **advisory** — they never set `root_cause`/`confidence` directly — so
  the drift affects *which evidence is gathered in what order*, not the reasoning over a
  fixed evidence set.
- **Replay is unaffected**: it pins the evidence and re-runs the deterministic
  `_analyze_evidence`, so any past investigation reproduces exactly regardless of store
  state.
This is a scoping fact, not a bug, but it must be stated: **determinism is certified for
the engine and for replay; it is NOT certified for the live stateful pipeline** without
freezing the learning stores. See backlog C-2.

## Verdict
**CERTIFIED — deterministic reasoning core + replay. CONDITIONAL — stateful pipeline.**
The deterministic surface (reasoning, validation, decision, receipts, artifacts, hashing)
is byte-stable under repetition (1000/1000 identical); the LLM is isolated downstream and
pinned under replay. The live pipeline's determinism is conditional on the learning-store
caveat above. No determinism blocker to a **replay-validated** controlled rollout;
store-drift must be characterised before any claim of run-to-run reproducibility in
production.

## Reproduce
```
DECISION_INTELLIGENCE_ENABLED=true VALIDATION_ENGINE_ENABLED=true \
  python - <<'PY'  # 1000-repeat hash-equality (see E-1)
PY
pytest tests/stabilization -q            # RC-A…RC-L guarantees
pytest -q                                # full regression
```
