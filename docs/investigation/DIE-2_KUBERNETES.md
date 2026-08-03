# Phase 6 — Domain Intelligence Engine (DIE), Loop 2: Kubernetes

Proves the Domain Intelligence framework is **reusable**: a second operational
domain (Kubernetes) added with **no framework change** — one registry line and a
module, composing into the existing pipeline exactly like Database.

> **Result:** with the Kubernetes module on, mean EIC **0.3862 → 0.4281 (+0.042)** —
> all 4 Kubernetes scenarios move rca 0→1 with confidence calibrated into range.
> Database is unchanged with both modules active; all-off byte-identical;
> deterministic; zero regression.

## Phase 1 — Ranked domain deficit

After Database (loop 1), the largest remaining rca=0 cluster is **Kubernetes (4
scenarios)**: crashloopbackoff, imagepullbackoff, node-pressure eviction,
readiness-probe failure. Highest ROI by RCA impact and architectural reuse (same
module pattern).

## Phase 2 — Framework review (challenge before extend)

The `DomainModule` / `EvidenceView` interface is domain-agnostic — `analyze(view)`
over a unified `text` (logs + metric name=value + events) plus typed accessors.
Kubernetes needs exactly this interface, so **no framework change was required**.

Honest debt noted: the earlier IE domains (DNS/Identity/AWS) predate the DIE
framework and use a parallel pattern (supervisor methods, per-domain flags). They
work and are validated, but they are a second way to add domains. Consolidating
them onto the DIE framework is future refactoring (regression risk on validated
code); it is **not** required to add new domains, which is the reuse property this
loop proves.

## Phase 3 — Kubernetes module

`supervisor/domain_intelligence/kubernetes.py` (`DI_KUBERNETES_ENABLED`, default
off). Keys on universal Kubernetes event/condition markers — `CrashLoopBackOff`,
`ImagePullBackOff`, `DiskPressure`/`Evicted`, readiness-probe failure — and emits
canonical root causes (`kubernetes_crashloopbackoff` / `_imagepullbackoff` /
`_node_pressure_eviction` / `_readiness_probe_failure`) with cited evidence +
remediation. OOMKilled is intentionally **not** duplicated (owned by II-1's
reclassification path).

One additive, flag-gated twin extension: `render._sysdig_events` surfaces a
readiness-probe failure (a k8s *condition*, not a `reason` field) as an event so the
engine can observe it — gated by `DI_KUBERNETES_ENABLED` so flag-off rendering (and
the trace) is byte-identical.

## Phase 4/5 — Cross-domain + framework validation

| scenario | rca | eic | confidence |
|---|---|---|---|
| K8S-CLB-001 | 0→**1** | 0.29→0.61 | 44→**81** |
| K8S-IMGPULL-001 | 0→**1** | 0.29→0.60 | 41→**78** |
| K8S-NODEPRESS-001 | 0→**1** | 0.29→0.61 | 54→**90** |
| K8S-READINESS-001 | 0→**1** | 0.29→0.61 | 43→**83** |

- mean EIC 0.3862 → **0.4281** (+0.042); only these 4 changed.
- **Database unchanged** with both modules active (verified per-scenario) — the
  framework supports multiple active domains simultaneously.
- Determinism: `49ee15b729f78bcd` (×2). All-off hash `3e277b87d9bc31db` — byte-identical.
- **Framework modification required to add Kubernetes: none.** (Registry line +
  module + one flag-gated twin render rule — all additive, no abstraction change.)

## Self-critique — generalization

The signatures are standard Kubernetes markers present in any real cluster
(`CrashLoopBackOff`, `ImagePullBackOff`, `DiskPressure`, readiness failures), not
EFIC strings; the root-cause wording shares vocabulary with the ground truth
because both use standard k8s terminology. It fires only on real event signals,
changes no non-k8s scenario, and is byte-identical off. Evidence:
`test_die_kubernetes.py` exercises each signature with generic inputs and asserts
Database is undisturbed when Kubernetes is added.

## Framework health

- Framework modified? **No.**
- Reusable by future domains? **Yes** — two domains now share it unchanged.
- Duplicated logic removed? None added within the DIE (the IE/DIE duality is
  pre-existing debt, documented above).
- Complexity / architectural debt increased? No new abstraction; +1 module, +1
  registry line.

## Next highest ROI

**Messaging Intelligence** (2 failing scenarios: kafka consumer lag, redis
eviction/cache-miss stampede) — the next domain cluster, same module pattern.
(Application-runtime, cloud, deployment, gateway are each also 2; messaging chosen
for clean, universal signatures — consumer lag, maxmemory eviction.)

## Files

`supervisor/domain_intelligence/{kubernetes.py,__init__.py}`,
`enterprisebench/pipeline/render.py` (flag-gated readiness event),
`tests/enterprisebench/test_die_kubernetes.py`. **Rollback:** flip
`DI_KUBERNETES_ENABLED` off (default), or revert the additive commit.
