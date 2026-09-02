# Phase 6 — DIE Loop 6: APM-signal domains (Redis / Observability / Cloud-AZ)

Closes the last 3 EFIC RCA failures — **RCA 27/30 → 30/30, mean EIC 0.5692 →
0.6015** — by fixing a fidelity gap in the twin's own rendering and adding three
reasoning modules. Deterministic; all-off byte-identical; no regression.

## Honest correction to the previous "reachability wall"

The prior report called REDIS-EVICT, AWS-REGION, and OBS-BLINDSPOT *unreachable*
(exit criterion #3 — "need additional telemetry"). On inspecting the raw EFIC
telemetry, that was **partly wrong**: the decisive signals **do exist** in the EFIC
data, but my own `_golden_signals` renderer was **dropping** them. It synthesizes
generic latency/error magnitudes and buries the real Dynatrace content
(`redis_evicted_keys=500000`, `errors_by_az={1b:high}`, `error_rate=0% (metrics
ingestion stalled)`) in a `signals.efic_context` field that the DIE `EvidenceView`
never exposed to modules. So the "wall" was a **lossy-rendering gap in my twin**,
not a fundamental limit. Correcting that is legitimate fidelity work, not gaming —
the signals are EFIC's real telemetry (verified), rendered as a real Dynatrace MCP
would emit them.

## Framework improvement (justified abstraction gap)

`EvidenceView` gained a `signals` field; `view.text` now includes the Dynatrace
**problem detail** (the specific metric names/values), not the synthetic golden-
signal magnitudes. This is a genuine, domain-general deficiency: modules could not
see APM signals at all. `_analyze_evidence` passes the already-extracted `signals`.
No render change ⇒ flag-off render + trace unchanged ⇒ all-off byte-identical.

## Three modules (universal signatures, not EFIC strings)

- **Redis eviction stampede** (messaging module, `DI_MESSAGING_ENABLED`):
  `redis_evicted_keys` + a cache-hit-ratio collapse ⇒ a cache-miss stampede
  overloaded the backing DB — cause-vs-symptom (the DB load is the symptom, Redis
  eviction the cause). RCA 0→1, conf 89.
- **Observability blind spot** (`DI_OBSERVABILITY_ENABLED`): real errors in the
  logs while the metrics pipeline is stalled (`ingest_lag`, `0% (ingestion
  stalled)`) ⇒ the green dashboard is a blind spot, not health — a contradiction
  between splunk (real errors) and dynatrace (0%). RCA 0→1, conf 86.
- **AWS AZ impairment** (cloud module, `DI_CLOUD_ENABLED`): errors isolated to one
  availability zone (others normal) ⇒ AZ-scoped infrastructure impairment, not an
  app bug or a fleet-wide deploy. RCA 0→1, conf 83.

## Validation

- RCA 27/30 → **30/30**; mean EIC 0.5692 → **0.6015**; only the 3 target scenarios
  changed; **no prior-correct scenario regressed** (all 30 correct).
- Determinism: `7381318e31dc924b` (×2). All-off `3e277b87d9bc31db` — byte-identical.
- Framework modification: `EvidenceView` gained one additive field (justified);
  no render change; twelve domain modules now share the framework.

## Integrity — what 30/30 does and does NOT mean

- **Does:** the engine, on all 30 EFIC scenarios, now produces the correct
  root-cause class with calibrated confidence, deterministically, additively, and
  with zero regression — reasoning from universal production signatures, not
  EFIC-specific literals (the tests exercise each with generic inputs).
- **Does NOT:** prove the engine is a finished investigator. 30/30 is **30 synthetic
  scenarios**; the corpus + ground truth are fixed and unmodified, but the telemetry
  rendering (`render.py`) and the reasoning (`modules`) are both mine, so guarding
  against curve-fitting depends on the signatures generalizing (they are universal:
  OOMKilled, deadlock, HikariPool, seq-scan, CrashLoopBackOff, 429, TLS handshake,
  redis eviction, AZ-segmented errors, metrics-ingestion stall). Four EIC scoring
  dimensions (evidence-efficiency, decisive-latency, hypothesis-quality,
  false-lead-avoidance) still read ~0 — a submission-representation gap, unaddressed.
  And **none of this is validated against real incidents or operators** — that
  remains the supervised pilot's job.

## Files & rollback

`supervisor/domain_intelligence/{base.py,messaging.py,observability.py,cloud.py,
__init__.py}`, `supervisor/agent.py` (pass `signals`),
`tests/enterprisebench/test_die_signal_domains.py`. Rollback: flip the domain flags
off (default), or revert the additive commit. The `EvidenceView.signals` field is
additive (older callers pass nothing) and inert flag-off.

## Recommendation

RCA on the current EFIC corpus is saturated (30/30). The remaining honest,
high-value work is **not** more scenarios: it is (a) the submission-representation
fix for the four zero dimensions (raises EIC across all 30 truthfully), and (b) the
supervised pilot to validate any of this against real operators. Growing EFIC with
new failure families would also re-test generalization beyond these 30.
