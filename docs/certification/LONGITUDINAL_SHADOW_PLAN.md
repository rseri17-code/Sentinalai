# LONGITUDINAL_SHADOW_PLAN.md
**SentinelAI — Production Readiness Certification · Phase 8**
A 30/60/90-day shadow validation, reusing existing machinery only. No runtime authority.

## Purpose
The binding constraint on certification is corpus size (3 labeled incidents vs the
500-record / 20-per-class readiness gate). This plan accumulates that corpus **safely**,
in shadow, while the system has zero authority — turning "not enough evidence" into a
dated, measurable trajectory.

## Reused machinery (no new subsystems)
- **Shadow flags** `HYPOTHESIS/ADAPTIVE/CAUSAL/VALIDATION/DECISION_INTELLIGENCE_ENABLED`
  — ON in staging, all additive `_*` metadata, authoritative output untouched.
- **Scientific Validation harness** (`sentinel_core/investigation_value/scientific_validation.py`)
  — `canonical_evaluation_record` + `build_report`, append-only history.
- **Effectiveness / nightly** (`investigation_value/effectiveness.py`, `nightly.py`) —
  per-period trend descriptors.
- **Readiness gates G1–G11** (`investigation_value/readiness.py`) — the promotion bar.
- **Replay** (`tests/replay/`) — daily replay-agreement checks.

## What each completed investigation records (daily)
For every investigation, compare and log one canonical evaluation record:
Operator RCA · Sentinel RCA · ground-truth label · localization · evidence vector ·
confidence · time-to-RCA · decision stability · false-positive / false-negative flags.
All fields already exist on the result dict; the harness composes them deterministically.

## Cadence & reports
| Period | Report | Source | Question answered |
|---|---|---|---|
| Daily | `report.json` + `history.jsonl` append | `build_report` | safety agreement, verified-incorrect, new failures |
| Weekly | trend descriptors | `effectiveness._trend` | is quality drifting? corpus growth rate? |
| Monthly | readiness-gate snapshot | `evaluate_gates` | how many of G1–G11 now pass? |

## Milestones (evidence targets)
| Day | Corpus target | Gate expectation |
|---|---|---|
| 30 | ≥ 150 labeled | G2 (admission precision), replay-agreement measurable; G1 still failing |
| 60 | ≥ 350 labeled, ≥ 15/class | G1 approaching; calibration N ≥ 30 → citable |
| 90 | **≥ 500 labeled, ≥ 20/class** | **G1 passes; statistical power reached** → E1/E2 become significant |

## Success criteria for the shadow period
1. **Safety holds:** shadow-↔-authoritative agreement stays ≥ 0.95 across the window
   (currently 1.0 on N=3). Any sustained drop is a stop signal.
2. **Zero verified-incorrect** attributable to a shadow signal.
3. **Replay stays clean** every day (byte-identical re-analysis).
4. **Corpus reaches 500/20** with a held-out partition never used in development.
5. Calibration MAE (evidence vs raw) resolves with N ≥ 30 — only then is the calibration
   claim admissible.

## Exit
At day 90, re-run the Scientific Validation Program on the accumulated held-out corpus.
Only if the readiness gates pass on a sufficiently-powered, leakage-free corpus does the
verdict advance from READY_AFTER_MORE_SHADOW_EVIDENCE toward a controlled human pilot —
and even then, promotion of any capability to authority requires a separate controlled
authoritative experiment (a shadow-only stack cannot demonstrate RCA benefit).

## Rollback
The plan changes nothing at runtime; "rollback" is simply turning the staging shadow
flags OFF, which restores byte-identical behaviour instantly.
