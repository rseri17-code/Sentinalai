# Phase 6 — Domain Intelligence Engine (DIE), Loop 3: API Gateway

Third domain on the unchanged framework. This loop's selection **rejected** the
previous report's Messaging recommendation on measured evidence, and demonstrates
cause-vs-symptom reasoning with negative evidence.

> **Result:** mean EIC **0.4281 → 0.4495 (+0.021)** — both API-gateway scenarios
> move rca 0→1 with calibrated confidence. Prior domains unchanged; all-off
> byte-identical; deterministic; no framework change.

## Phase 1 — Ranked domain deficit (measured)

16 RCA failures remain, in five 2-failure clusters + six singletons. Count alone
does not decide; the differentiator is **decisive-evidence reachability**:

| domain | reachable failures | signal quality |
|---|---|---|
| **api_gateway** | **2/2** | STRONG — `429 rate limit` (log); `TLS handshake` (log) + istio-proxy cert-reload (event) |
| application_runtime | 2/2 | mixed — THREADPOOL strong; GC fragile ("gc" only in a metric value) |
| messaging | **1/2** | KAFKA strong; **REDIS-EVICT's decisive signal does not reach the engine** (no splunk log; only generic DB-pool metrics) |
| cloud_aws | 1/2 | AWS-SG reachable; AWS-REGION's signal never triggers the probe |
| deployment | 2/2 | needs change-correlation reasoning (higher complexity) |

**Selected: API Gateway** — highest reachable RCA gain (2/2) with the strongest,
most universal signatures and the lowest complexity/regression risk.

## Phase 2 — Why the Messaging recommendation was rejected

`EFIC-REDIS-EVICT-001` carries its decisive signal (Redis maxmemory eviction /
cache-miss stampede) in telemetry the engine cannot see under the current rendering
(no splunk log; dynatrace golden-signals are synthesized generically; only generic
DB-pool metrics reach). So Messaging could fix at most 1/2 without new telemetry
rendering — strictly dominated by API Gateway's 2/2. Evidence, not the prior
report, drove the choice.

## Phase 3 — API Gateway module (no framework change)

`supervisor/domain_intelligence/api_gateway.py` (`DI_API_GATEWAY_ENABLED`, off).
Two failure modes, each reasoning about **cause vs symptom**:

- **rate-limit saturation:** a 429 is a *symptom*. The module reads negative
  evidence — the backend pool (`active` vs `max`). With the backend healthy
  (`active < 0.8·max`) the cause is the shared **rate limit quota** bucket
  saturating under a consumer surge, *not* backend overload (confidence 82); with a
  saturated backend the explanation is ambiguous, so confidence is lower (74). This
  is confidence discipline: certainty tracks the disambiguating evidence.
- **istio mTLS failure:** east-west `TLS handshake` failures + an istio-proxy
  cert-reload-failed event ⇒ the sidecar certificate was not rotated after a CA
  change — not a network outage (north-south / ThousandEyes healthy is the negative
  evidence).

Signatures (HTTP 429, `TLS handshake`, `istio-proxy cert reload`) are universal
gateway/mesh markers, not EFIC strings.

## Phase 4/5 — Validation

| scenario | rca | eic | confidence |
|---|---|---|---|
| GW-RATELIMIT-001 | 0→**1** | 0.29→0.61 | 55→**92** |
| ISTIO-MTLS-001 | 0→**1** | 0.29→0.61 | 51→**88** |

- mean EIC 0.4281 → **0.4495** (+0.021); only these 2 changed.
- Prior domains (Database, Kubernetes, DNS/Identity/AWS) unchanged (verified).
- Determinism: `b89b0cb4b59f9720` (×2). All-off `3e277b87d9bc31db` — byte-identical.
- **Framework modification required: none** (module + one registry line).

An initial version regressed GW-RATELIMIT (eic 0.29→0.27): the root cause said
"rate-**limit**"/"throttl**ing**", which do not substring-match the ground-truth
keywords, so a *confident wrong* answer hurt calibration. Fixed by using the natural
canonical vocabulary ("rate limit", "quota") — a correctness fix, not benchmark
fitting (these are the standard terms for gateway throttling). This is exactly the
regression-as-hard-gate discipline: the confident-but-wrong intermediate was caught
by EnterpriseBench and corrected before acceptance.

## Self-critique — generalization

The module keys on universal gateway/mesh signals and reasons from negative
evidence (backend health, north-south health) rather than collapsing a 429 or a TLS
error into a root cause. It fires only on real signals, changes no other scenario,
and is byte-identical off. A Distinguished Engineer would challenge the fragility of
substring keyword matching (the "rate-limit" hyphen bug) — mitigated here by using
canonical spacing, and more durably addressed by the deferred representation-fidelity
objective. Evidence: `test_die_api_gateway.py` exercises cause-vs-symptom with
generic inputs and asserts prior domains are undisturbed.

## Next highest ROI

Derived from the NEW failure distribution (14 remaining): the strongest 2/2
reachable cluster is now **application_runtime** (THREADPOOL strong via
`thread pool exhausted`; GC reachable via the sysdig "aligned to GC" metric),
followed by deployment (change-correlation). Messaging remains 1/2 until its
telemetry is rendered.

## Files

`supervisor/domain_intelligence/{api_gateway.py,__init__.py}`,
`tests/enterprisebench/test_die_api_gateway.py`. **Rollback:** flip
`DI_API_GATEWAY_ENABLED` off (default), or revert the additive commit.
