# PRODUCTION_TRUST_INDEX_V2.md
**SentinelAI — Production Trust Index, recomputed after blocker closure (`56129ef`)**
Same methodology as v1: score measured dimensions only; unmeasured → coverage, not a guess.

## What changed vs v1
The four verified blockers (B-1/B-2/B-3, F-obs) are closed, moving four dimensions and
lifting the coverage of the measured set.

| # | Dimension | v1 | v2 | Why it changed |
|---|---|---|---|---|
| 1 | Determinism | 1.00 | **1.00** | already max; the pipeline caveat (wall-clock defects) is now *removed* — score holds with stronger backing |
| 2 | Replay | 0.90 | **1.00** | artifact now canonical; 1000-iteration byte-identity certified |
| 3 | Evidence Integrity | 0.90 | 0.90 | unchanged |
| 8 | Investigation Stability | 0.85 | 0.85 | unchanged |
| 11 | Runtime Reliability | 0.85 | **0.90** | silent-failure gap closed for the two swallow sites |
| 12 | Recovery | 0.80 | **0.90** | degraded runs now operator-visible (`_sources_unavailable`, `degraded_investigation`) |
| 14 | Performance | 0.60 | 0.60 | unchanged (load still NOT MEASURED) |
| 15 | Operational Readiness | 0.50 | **0.60** | failure observability is a real operability gain |
| 4,5,6,7,9,10,13 | Citation / Grounding / Localization / Calibration / Operator-agreement / Benchmark / Chaos | NOT MEASURED | **NOT MEASURED** | still gated on labeled corpus + harnesses |

## Result
- **Measured dimensions:** 8 of 15 (unchanged set) → **Coverage = 0.53**
- **PTI (mean of measured)** = (1.00 + 1.00 + 0.90 + 0.85 + 0.90 + 0.90 + 0.60 + 0.60) / 8
  = **0.84** (was 0.80)

## Interpretation
> **PTI 0.84 at 0.53 coverage** (was 0.80 at 0.53).

The measured trust surface rose on **objective evidence** — canonical replay (byte-identity
proven), determinism defects removed (repo scan FORBIDDEN=0), and failure observability
added. Coverage is unchanged: the closure program was scoped to *defects*, not to the
*evidence gaps* (calibration, quality, benchmark, chaos, load) that only a labeled corpus
and load/chaos harnesses can close — that is the 90-day shadow pilot's job.

**Still not "84% ready."** A 0.53-coverage index means the decisive value dimensions remain
unmeasured. The gain is that the *defective* half is now *sound*, which is exactly what a
read-only pilot requires before it is worth accumulating the evidence for the rest.

## Certification matrix delta
| Subsystem | v1 | v2 |
|---|---|---|
| Five-phase pipeline | CONDITIONALLY CERTIFIED (D-1/D-2) | **CERTIFIED** (wall-clock defects removed) |
| Replay | CONDITIONALLY CERTIFIED (D-3) | **CERTIFIED** (canonical, byte-identity proven) |
| `_call_worker` / worker layer | CONDITIONALLY CERTIFIED (silent loss) | **CONDITIONALLY CERTIFIED** (2 swallows fixed; optimistic discovery fallback remains, tracked) |
| Learning stores | CONDITIONALLY CERTIFIED (drift) | CONDITIONALLY CERTIFIED (drift characterisation still pending, C-2) |
| Calibration / quality / operability | NOT CERTIFIED | NOT CERTIFIED (evidence gaps, unchanged) |

## Verdict
**READY FOR 90-DAY SHADOW PILOT (read-only).** Trust rose on measured evidence; the four
blockers are closed; the remaining path is measurement, not code.
