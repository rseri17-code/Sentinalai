# SentinelAI — Pilot Go/No-Go Assessment

**Scope:** readiness for a *supervised, read-only* operator pilot. Architecture frozen.
**Recommendation:** **GO for supervised pilot** — conditional on the deployment checklist
being signed and outcome metrics being treated as `NOT_MEASURED` until the pilot produces them.

---

## 1. Defect Review

Every issue surfaced during OVP Phase 1, classified by evidence.

| # | Issue | Evidence | Class | Disposition |
|---|---|---|---|---|
| D-1 | `service_health_decline` actions shipped with an **empty evidence list**, so one recommendation type was untraceable | Measured 15/16 recommendation traceability (`phase1_measured_baseline.json`) | **Recommended before pilot** | **FIXED** — `incident_trends._health_decline` now attaches the declining service's incidents; `_investigate_first` inherits them. Traceability now 16/16 = 1.0. Regression test added. |
| D-2 | Real labelled corpus is `n=3` (gold) / `n=6` (EIC) — all outcome metrics underpowered | Gold `evaluation.json` `underpowered:true` across 10 metrics | **Safe to defer** (not a code defect) | Deferred to pilot: corpus is powered to `n≥30` *by running the pilot*. No code change. |
| D-3 | No operators / incident timeline offline → MTTI, trust, acceptance unmeasurable | OVP §0 boundary | **Safe to defer** (environmental) | Deferred: exactly what the pilot exists to measure. Instrumentation added (see §Instrumentation). |

Only **D-1** warranted code. It was evidence-supported, low-risk, and directly improves the
fairness of a pilot that *measures* traceability — so it was fixed test-first. D-2/D-3 are not
defects; forcing code onto them would violate the freeze.

## 2. Instrumentation status

| Required measurement | Mechanism | Status |
|---|---|---|
| Investigation duration | `ModuleResult.elapsed_ms` + phase receipts (existing) | ✅ reused |
| Evidence access | R2 `_evidence_lifecycle` (existing) | ✅ reused |
| Replay usage | replay artifact + `corpus_version` (existing) | ✅ reused |
| Operator interactions | `pilot_telemetry.pilot_event("operator_interaction", …)` | ✅ added (produce-only) |
| Recommendation usage | `pilot_telemetry.pilot_event("recommendation_usage", …)` | ✅ added (produce-only) |
| Operator feedback | `pilot_telemetry.pilot_event("operator_feedback", …)` | ✅ added (produce-only) |

Half the required signals already existed and are reused; only the three operator-side event
types needed a sink, provided by a thin append-only recorder imported by no runtime path.

## 3. Confirmed strengths (measured, offline)

- **Determinism** — all five OIP services byte-identical on recompute; full regression green.
- **Verifiability = 1.0** — every evaluated unit carries an R1 corpus stamp; the Daily Brief
  reports `verification_status` with corpus-stamped counts.
- **Recommendation traceability = 1.0** — after D-1, every actionable item cites the incidents
  behind it (16/16).
- **Honest evidence lifecycle** — `used`/`unavailable`/`error` are surfaced, not hidden;
  `insufficient_history` and `(unresolved)` are shown.
- **No authority / no blast radius** — produce-only surfaces, imported by no runtime path;
  the investigation engine is untouched.

## 4. Known limitations

- **All operator outcomes are `NOT_MEASURED`** — MTTI, time-to-owner/evidence/decision,
  operator confidence/trust, recommendation acceptance, repeat-investigation rate. No offline
  evidence exists; the pilot must produce it.
- **Underpowered corpus** — `n=3`/`n=6` today; conclusions require `n≥30`.
- **Ownership is only as good as its source** — `owner` reflects existing incident metadata;
  garbage in, garbage out.
- **`reliability_direction` needs ≥2 periods** — single-period services report
  `insufficient_history` by design.

## 5. Open risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Operators over-trust an unverifiable output | Medium | Runbook makes `verifiable` the primary trust signal; monitoring watches for verifiability drops |
| Small pilot corpus yields no significant result | Medium | Pre-registered `n≥30` gate; report `NOT_MEASURED` rather than over-claim |
| Ownership metadata gaps route handoffs wrong | Low–Med | Runbook instructs owners to correct the source system; SentinelAI only reflects it |
| Corpus/replay drift mid-pilot | Low | Weekly determinism + verifiability spot-check on the pinned corpus |

## 6. Blocking defects

**None.** D-1 is fixed; D-2/D-3 are environmental and are what the pilot measures. No issue
prevents a supervised, read-only pilot.

## 7. Recommendation

> **GO — start the supervised pilot** per OVP Phase 1 §1, subject to:
> 1. All six deployment gates signed (`DEPLOYMENT_CHECKLIST.md`).
> 2. Outcome metrics treated as `NOT_MEASURED` until the pilot produces them at `n≥30`.
> 3. Trust ≥ 4/5 and primary-KPI improvement without safety regression as the exit gate to any
>    expanded deployment.

Rationale: the platform's **integrity** properties (determinism, verifiability, traceability)
are measurably sound and the one verified defect is fixed — clearing the bar for supervised use.
Its **operational value** is deliberately unproven; the pilot exists to convert assumptions into
measured outcomes. Building OIP #6/#7 now would add capability the evidence does not yet justify.

---

### Regression gate
Full suite must be green at pilot start. Latest run: **5927 passed, 2 skipped, 0 failed**
(5916 baseline + 11 pilot-telemetry tests; the D-1 fix strengthened an existing incident-trends
test). Recommendation traceability and verifiability both **1.0** on the measured baseline.
