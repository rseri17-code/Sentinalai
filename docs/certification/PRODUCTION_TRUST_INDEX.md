# PRODUCTION_TRUST_INDEX.md
**SentinelAI — Production Readiness Certification · Phase 9**
A single, automatable certification score. Every dimension is scored from objective
evidence; unmeasured dimensions are **excluded from the score and reported as coverage**,
never guessed to 0 or 1.

## Design
- 15 dimensions, each scored in [0,1] from a named evidence source.
- **Production Trust Index (PTI)** = mean of *measured* dimensions.
- **Coverage** = measured dimensions ÷ 15. A high PTI at low coverage is explicitly
  *not* a pass — both numbers travel together.
- Recomputable every release from the deterministic reports + regression output.

## Scored dimensions (current evidence)
| # | Dimension | Score | Evidence |
|---|---|---|---|
| 1 | Determinism | **1.00** | 1000 repeats → 1 hash; flag-OFF no-op PASS (DETERMINISM_REPORT) |
| 2 | Replay | **0.90** | replay short-circuit re-analyses pinned evidence; `tests/replay` (58 tests) green; end-to-end replay-agreement at scale NOT re-measured here |
| 3 | Evidence Integrity | **0.90** | RC-A…RC-L guarantees present + tested (165 stabilization tests); append-only, redaction, framed hashing |
| 4 | Citation Accuracy | **NOT MEASURED** | anti-hallucination present (`CITATION_ANTI_HALLUCINATION_ENABLED`); no at-scale precision measurement |
| 5 | Grounding | **NOT MEASURED** | `_grounding` produced per-investigation; unvalidated vs labels at scale |
| 6 | Localization | **NOT MEASURED** | deterministic T3 localization present; not validated vs ground-truth service at scale (N=3) |
| 7 | Confidence Calibration | **NOT MEASURED** | underpowered (N=3, MAE inverted); not citable until N≥30 (SCIENTIFIC_VALIDATION_AUDIT F-4) |
| 8 | Investigation Stability | **0.85** | deterministic T5 stability signal, verified reproducible; benefit unvalidated |
| 9 | Operator Agreement | **NOT MEASURED** | no operator-labeled corpus |
| 10 | Benchmark Agreement | **NOT MEASURED** | SentinelBench present; N=3, underpowered, leakage risk (F-5) |
| 11 | Runtime Reliability | **0.85** | fail-fast phase boundary + graceful in-phase degradation; 5665 tests green |
| 12 | Recovery | **0.80** | early-return degradation paths (empty/meta/gate/deadline) return partial results with receipts |
| 13 | Chaos Resilience | **NOT MEASURED** | no investigation-chaos harness executed (CHAOS report is design + existing-test analysis) |
| 14 | Performance | **0.60** | reasoning overhead measured (p99 0.34ms); concurrency/worst-case load NOT MEASURED |
| 15 | Operational Readiness | **0.50** | shadow-only, gates present & failing G1; no runbooks/on-call/alerting validated |

## Result
- **Measured dimensions:** 8 of 15 → **Coverage = 0.53**
- **PTI (mean of measured)** = (1.00 + 0.90 + 0.90 + 0.85 + 0.85 + 0.80 + 0.60 + 0.50) / 8
  = **0.80**

## Interpretation
> **PTI 0.80 at 0.53 coverage.**

The measured half of the system scores **high (0.80)** — determinism, evidence
integrity, replay, and runtime reliability are strong. But **almost half the trust
surface is unmeasured** — everything that requires a labeled corpus (calibration,
localization, benchmark/operator agreement, grounding, citation accuracy) or a load/chaos
harness (performance-under-load, chaos resilience, operational readiness).

**A PTI of 0.80 must not be read as "80% ready."** With coverage at 0.53, the honest
statement is: *the parts we can measure are trustworthy; the parts that decide production
value are not yet measured.* Certification requires raising **coverage**, not just the
score — the Longitudinal Shadow Plan (Phase 8) is the mechanism.

## Automation
This table is computed from `eval/scientific_validation/report.json` + the determinism/
operability experiments + regression output. Wire it into the nightly pipeline so every
release emits `(PTI, coverage)` as a tracked pair.
