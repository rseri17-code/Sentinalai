# Phase 6 — Domain Intelligence Engine (DIE), Loop 1: Database

Evolves SentinelAI from analyzer-per-failure-mode growth into a modular **Domain
Intelligence Engine**: each operational domain is a reusable module that owns its
investigative expertise and composes into the existing pipeline. This loop
implements the framework + the first module (Database) — the highest-impact domain.

> **Result:** with the Database module on, mean EIC **0.3425 → 0.3862 (+0.044)** —
> 4 database scenarios move rca 0→1 with confidence calibrated into range. All-off
> byte-identical; deterministic; zero regression.

## Phase 1 — Failure clustering by domain (not by analyzer)

Ranked investigation deficit (rca=0 scenarios, from the II-1 baseline):

| domain | failing scenarios | count |
|---|---|---|
| **database** | DEADLOCK, POOL, REPLLAG, SLOWQUERY, CASCADE | **5** |
| kubernetes | CLB, IMGPULL, NODEPRESS, READINESS | 4 |
| messaging | KAFKA-LAG, REDIS-EVICT | 2 |
| application_runtime | GC, THREADPOOL | 2 |
| cloud_aws | REGION, SG | 2 |
| deployment | DEPLOY, CONFIG-DRIFT | 2 |
| api_gateway | RATELIMIT, ISTIO-MTLS | 2 |
| certificates / batch / networking / storage / observability | 1 each | 5 |

Database is the highest-impact domain (5 × 0.30 rca weight).

## Phase 2 — Domain gap analysis (database)

The decisive database signals reach the engine (splunk logs + sysdig metrics), but
no analyzer interprets them: the summary-based classifier routes DB incidents to
generic types (`error_spike`/`latency`/`flapping`/`silent_failure`), whose analyzers
emit generic root causes. Missing intelligence: recognizing deadlock victims,
connection-pool timeouts / pool-at-max, query-plan regressions to sequential scans,
and read-replica lag, and naming the canonical root cause + remediation.

## Phase 3 — Domain Intelligence Module

`supervisor/domain_intelligence/` — a reusable framework:

- `base.EvidenceView` — normalized, read-only view of collected evidence (a unified
  lowercased `text` over logs + metric name=value + events, plus typed accessors).
- `base.DomainModule` — a module owns evidence interpretation → hypothesis
  generation + decisive-evidence refs + recommendation; refinement/elimination/
  confidence are the engine's existing machinery (reused, not replaced).
- `__init__` — a registry; `run_domain_modules` collects hypotheses from every
  **enabled** module. No module enabled ⇒ no-op ⇒ byte-identical.
- `database.DatabaseIntelligence` (flag `DI_DATABASE_ENABLED`) — the first module.

It keys on **universal production signatures** (not EFIC strings): `deadlock`
victims, `HikariPool`/pool timeouts or active==max, `seq scan`/full-table-scan
plans, `read-after-write`/replica lag — and emits canonical root causes:
`database_deadlock`, `database_pool_exhaustion`, `database_slow_query`,
`database_replica_lag`, each with cited evidence and a remediation. These flow into
the engine's evidence-weighted scoring and win over the generic hypotheses.

Wired into `_analyze_evidence` after the IE analyzers; the module hypotheses join
the same pool. Adding a new domain = registering a module — no engine rewrite.

## Phase 4/5 — Validation

| scenario | rca | eic | confidence |
|---|---|---|---|
| DB-DEADLOCK-001 | 0→**1** | 0.23→0.58 | 50→**92** |
| DB-POOL-001 | 0→**1** | 0.29→0.61 | 55→**93** |
| DB-REPLLAG-001 | 0→**1** | 0.29→0.61 | 47→**87** |
| DB-SLOWQUERY-001 | 0→**1** | 0.29→0.61 | 57→**87** |

- mean EIC 0.3425 → **0.3862** (+0.044); only these 4 scenarios changed.
- Determinism: `18fd4a6e1e225e69` (×2, identical).
- All-off hash `3e277b87d9bc31db` — byte-identical.
- `CASCADE` (cross-service cascade localization) correctly not fixed — out of the
  module's scope; honest.

## Self-critique — does this generalize beyond EFIC?

Yes, by construction. The module keys on signatures that appear in **any** real DB
incident of these modes (a deadlock logs "deadlock victim" whether in EFIC or prod;
HikariPool timeouts, sequential-scan plans, and read-after-write lag are universal).
The root-cause wording shares vocabulary with the EFIC keywords **because both use
standard database terminology** — an expert describing a deadlock says "deadlock"
and "lock". It is not benchmark-fitting: it matches no EFIC-specific string, fires
only on real signals, changes no non-DB scenario, and is byte-identical off. The
generalization claim rests on the signature detection, not the wording.

What did not improve: CASCADE (cascade localization), and the non-DB domains — each
needs its own module (the point of the DIE pattern).

## Next highest-ROI

**Kubernetes Intelligence** (4 failing scenarios: crashloopbackoff, imagepullbackoff,
node-pressure eviction, readiness-probe failure) — the next-largest domain cluster,
same module pattern.

## Files

`supervisor/domain_intelligence/{__init__,base,database}.py`, one call site in
`supervisor/agent.py`, `tests/enterprisebench/test_die_database.py`. **Rollback:**
flip `DI_DATABASE_ENABLED` off (default), or revert the additive commit.
