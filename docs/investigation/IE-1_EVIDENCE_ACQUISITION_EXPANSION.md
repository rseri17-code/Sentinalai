# IE-1 — Investigation Capability Expansion (Evidence Acquisition)

**Architecture only. No production code in this cycle.** This document analyzes
the investigation engine's evidence-acquisition path, proves exactly why
certificates / DNS / identity / AWS / Autosys / CMDB / ThousandEyes are
unreachable, and designs an additive expansion that lets the engine acquire *and
reason over* that evidence without degrading existing behavior. Implementation
begins only after review.

> **Context.** EB-2 built a deterministic pipeline that runs the real engine on
> EFIC scenarios. EB-3 proved the bottleneck moved into the engine: improving
> twin fidelity no longer improves investigation quality, because the engine has
> no path to consume the new evidence (activating ThousandEyes was a *measured
> no-op*). IE-1 designs the engine-side fix.

---

## 1. Current Investigation Architecture

`SentinalAISupervisor.investigate(incident_id)` (`supervisor/agent.py:323`) runs a
fixed 5-phase pipeline (`agent.py:445-507`); the context carries only the id:

| Phase | Module | Does |
|---|---|---|
| FETCH | `supervisor/phases/fetch.py:79` | `moogsoft.get_incident_by_id` → `Incident` (service, severity, summary, timestamps) |
| CLASSIFY | `supervisor/phases/classify.py:177` → `tool_selector.classify_incident` (`tool_selector.py:223`) | summary → one of 9 incident types (keyword match; default `error_spike`) |
| COLLECT | `supervisor/phases/collect.py:171` → `agent._execute_playbook` (`agent.py:1915`) | run the incident-type playbook; workers dispatched concurrently; `evidence[label]=response` |
| ANALYZE | `agent._analyze_evidence` (`agent.py:2214`) | extract evidence → generate hypotheses → weight confidence → winner |
| PERSIST | `supervisor/phases/persist.py:106` | receipts, replay, result |

The engine is **deterministic offline** (LLM refinement optional, gated by
`LLM_ENABLED`; `supervisor/llm.py:138`). Confidence is capped by an
anti-hallucination citation gate unless evidence is cited.

---

## 2. Evidence Acquisition Pipeline

The path a signal must travel to affect a diagnosis has **eight sequential
links**. This is the central finding: acquisition alone (links 1-6) is
insufficient — evidence that is fetched but not *extracted and reasoned over*
(links 7-8) is a no-op, exactly as EB-3 measured for ThousandEyes.

```
1 classify_incident(summary)            tool_selector.py:223  → incident_type
2 INCIDENT_PLAYBOOKS[incident_type]     tool_selector.py:29   → [ {worker,action,query_hint,label} ]
3 _build_params(step, id, service)      agent.py:2161         → params (query/window/scope)
4 worker.execute(action, params)        workers/*.py          → gateway.invoke(tool, action, params)
5 McpGateway.invoke → server dispatch   mcp_client.py:639     → MCP response (or stub empty)
6 evidence[label] = response            agent.py:2040         → raw evidence dict
7 _extract_*(evidence)                  agent.py:3279-3454    → SIX fixed buckets
8 _analyze_<type>(…6 buckets…)          agent.py:2706+        → Hypothesis(root_cause, score, refs)
```

**The fixed evidence contract (link 7→8).** Every analyzer has the signature
`_analyze_X(self, service, summary, logs, signals, metrics, events, changes,
timeline)` (`agent.py:3030` etc.). Analysis can see **only these six buckets**.
Evidence from a new MCP that is not mapped into one of these buckets — or read by
an analyzer via a predicate like `_has_dns_issues(logs)` / `_find_deployment(
changes)` — cannot influence the outcome regardless of how faithfully it was
fetched.

---

## 3. Worker Selection Logic

Workers are built once per investigation from a static factory
(`agent.py:270-286`), each gated by the tools `discover_tools()` advertises
(`agent.py:288-297`) against `_WORKER_SERVERS` (`agent.py:238-254`):

```python
required = self._WORKER_SERVERS.get(name, frozenset())
if not required or required & available_servers:
    self.workers[name] = factory()          # else: skipped, logged
```

Registered workers → servers: `ops→moogsoft, log→splunk, metrics→sysdig,
apm→dynatrace/signalfx, itsm→servicenow, devops/code/git→github,
confluence→confluence, network→(none; ENABLE_THOUSANDEYES_RCA gates internally)`.
**Worker selection does not choose evidence** — the *playbook* does. Advertising a
new server does nothing unless a worker exists AND a playbook step invokes it.

---

## 4. MCP Reachability Graph

Per target MCP, the first broken link (∅ = does not exist):

| MCP | 1 classify | 2 playbook step | 3 worker | 7 extractor | 8 analyzer use | First break |
|---|---|---|---|---|---|---|
| **ThousandEyes** | via `network`/`timeout`/`latency` | ✔ `network_worker` steps | ✔ (flag-gated) | ✔ `_extract_network_evidence` (agent.py:3417) | **∅ no analyzer consumes it** | **link 8** |
| **certificates** | keyword→`network` (tool_selector.py:136) | ∅ | ∅ | ∅ | ∅ | **link 2** |
| **route53/DNS** | keyword→`network` | ∅ (dns is a Splunk hint only) | ∅ | ∅ | partial (`_has_dns_issues(logs)` reads Splunk, not DNS MCP) | **link 2** |
| **identity/IAM** | ∅ (no keywords) → `error_spike` | ∅ | ∅ | ∅ | ∅ | **link 1** |
| **aws_cloudwatch** | ∅ → `error_spike` | ∅ | ∅ | ∅ | ∅ | **link 1** |
| **Autosys** | ∅ → `error_spike` | ∅ | ∅ | ∅ | ∅ | **link 1** |
| **CMDB** | n/a | served via `servicenow.get_ci_details` | ✔ itsm_worker | partial (`_extract_itsm_context`) | **∅ no analyzer uses CI drift** | **link 8** |

**Two distinct failure classes:**
- **Acquisition-blocked** (certs, DNS, identity, AWS, Autosys) — break at links 1-2:
  the engine never even *asks*.
- **Reasoning-blocked** (ThousandEyes, CMDB) — break at link 8: the engine *asks
  and receives*, but no analyzer converts the answer into a hypothesis. This is
  the EB-3 no-op, now precisely located.

---

## 5. Missing Investigation Paths

To make each domain diagnostic, every broken link must be repaired end-to-end. By
EFIC **decisive-evidence** value (not scenario count — ThousandEyes is high-count
but its evidence is mostly *negative*, "rule out network", so low diagnostic
upside):

| Domain | EFIC scenarios (decisive) | Missing links | Target hypothesis to emit |
|---|---|---|---|
| AWS (CloudWatch) | 5 (S3 throttling, AZ impairment decisive) | 1,2,3,7,8 | `s3_throttling`, `az_impairment`, `sg_block` |
| DNS (Route53) | 2 (both decisive) | 2,3,7,8 (classify already→network) | `stale_dns_record`, `resolver_unhealthy` |
| Identity/IAM | 2 (both decisive) | 1,2,3,7,8 | `signing_key_expiry`, `iam_permission_revoked` |
| Certificates | 1 (decisive) | 2,3,7,8 (classify→network) | `certificate_expiry` |
| Autosys | 1 (decisive) | 1,2,3,7,8 | `batch_dependency_failure` |
| ThousandEyes | 17 (1 decisive) | **8 only** | strengthen/weaken existing net hypotheses |
| CMDB | 2 (decisive) | **8 only** (data already fetched) | `config_drift` |

---

## 6. Required Worker Changes

Two viable shapes; the recommendation is a **phased hybrid**.

**Option A — bespoke workers (mirror existing pattern).** For each acquisition-
blocked domain add: a worker class (`workers/<domain>_worker.py`) calling
`gateway.invoke("<server>.<tool>", action, params)`; a `_worker_factory` entry
(`agent.py:270`); a `_WORKER_SERVERS` entry (`agent.py:238`); and a
`_TOOL_TO_SERVER`/stub entry (`mcp_client.py`). *Pros:* identical to proven
workers, low architectural risk, each gated by `discover_tools()`. *Cons:* ~5 new
workers; per-domain boilerplate.

**Option B — one declarative `DomainProbeWorker`.** A single generic worker driven
by a `DomainProbe` registry entry `{server, tool, action, param_spec,
response_schema}`; new domains are data, not code. *Pros:* one worker, marginal
cost per domain ≈ a registry row + an extractor + a hypothesis rule. *Cons:* a new
abstraction; must not weaken the typed worker contract.

**Recommendation:** Option A for the **first** domain (prove the full 8-link chain
end-to-end against EB-2), then extract Option B's registry once 2-3 domains share
the shape. Do not build the abstraction before the pattern is validated.

---

## 7. Required Playbook Changes

- **New/extended incident types** (`INCIDENT_PLAYBOOKS`, `tool_selector.py:29`):
  add `identity`, `cloud`, `batch` playbooks; extend `network` with cert/DNS probe
  steps. Each new step: `{worker, action, query_hint, label}` + a matching
  `_build_params` branch (`agent.py:2161`, new `elif step["action"]==…`).
- **Classification** (`CLASSIFICATION_KEYWORDS`, `tool_selector.py:104`): add
  keyword sets for the acquisition-blocked domains (e.g. `iam/permission/denied/
  access denied`, `s3/cloudwatch/throttl/availability zone`, `autosys/batch/job
  terminated`). Certificates/DNS already route to `network`.
- **Safety:** new keywords must be *specific* (avoid stealing existing incidents);
  new steps are additive (existing playbooks unchanged); every new step's server is
  gated by `discover_tools()`, so absent tools degrade to today's behavior.

**Critical (links 7-8): extend the reasoning layer, or repeat the no-op.** Add a
`_extract_<domain>(evidence)` reader (`agent.py:3279+` pattern) and a
`_analyze_<type>` analyzer (or extend `network`/`generic`) that emits the target
hypothesis with correct `evidence_refs` (so the citation gate lifts confidence).
The six-bucket analyzer signature is extended with **one optional `domain: dict`
parameter defaulting to `{}`** — additive and backward-compatible; existing
analyzers ignore it.

---

## 8. Risk Analysis

| Risk | Severity | Mitigation |
|---|---|---|
| Classification drift — new keywords misroute existing incidents | **High** | specific keywords; keyword-precedence tests; EB-2 regression on all 30 EFIC + existing fixtures must not regress |
| Analyzer regression — editing `_analyze_evidence`/analyzers breaks the crown-jewel deterministic RCA | **High** | additive optional `domain` param; no edits to existing analyzer bodies; per-analyzer golden tests |
| Confidence miscalibration — new hypotheses over/under-score | Med | conservative `base_score`; require `evidence_refs`; calibration check via EB-2 confidence-in-range |
| Determinism loss — new workers add clock/order nondeterminism | Med | incident-anchored time only; concurrency-order canonicalized (as EB-2 does) |
| Tool-absence in prod — new servers not connected | Low | `_WORKER_SERVERS` gating → worker skipped, playbook step no-ops, today's behavior preserved |
| Scope creep in the gateway/planner | Med | changes confined to worker/playbook/extractor/analyzer; gateway transport untouched |

---

## 9. Backward Compatibility Strategy

- **Additive-only:** new workers, new playbook *types*, new keywords, new
  extractors, new analyzers. No existing worker, playbook step, analyzer body, or
  the gateway transport is modified.
- **Optional evidence param:** `domain: dict = {}` — old analyzers/callers unaffected.
- **Feature-flag every domain** (`IE_<DOMAIN>_ENABLED`, default **false**):
  flags off ⇒ byte-identical behavior to today. This preserves the ~6045-test
  regression and lets EB-2 measure each domain in isolation.
- **Tool-gated:** even with a flag on, an absent MCP degrades to current behavior.

---

## 10. Incremental Migration Plan

Each step is independently reviewable, flag-gated, and validated by EB-2 before
the next.

1. **IE-2 (pilot, bespoke):** highest decisive-value domain — **DNS** or **AWS-S3**
   (classify already near for DNS). One worker + playbook step + `_build_params`
   branch + extractor + analyzer, behind `IE_DNS_ENABLED`. Success = the domain's
   EFIC scenarios move FAIL→PASS in EB-2 with **zero regression elsewhere**.
2. **IE-3:** repeat for Identity, AWS, Autosys, Certificates (bespoke), each flagged
   and EB-2-validated.
3. **IE-4 (reasoning-blocked):** ThousandEyes + CMDB — link-8 only: add analyzers
   that consume the already-fetched evidence (network negative-evidence weighting;
   CI config-drift hypothesis). Flip `ENABLE_THOUSANDEYES_RCA` default only after
   EB-2 shows it is no longer a no-op.
4. **IE-5 (refactor):** extract the `DomainProbe` registry (Option B) from the 2-3
   proven bespoke domains; migrate them; no behavior change (guarded by
   golden-output equality).

---

## 11. Validation Strategy

- **EB-2 is the oracle.** Per domain, the corresponding EFIC scenarios must move
  FAIL→PASS (rca_correctness 0→1, confidence into the expected range) with the flag
  on, and remain byte-identical with the flag off. EB-2 is deterministic, so this
  is a hard, reproducible gate.
- **No-regression gate:** all non-target EFIC scenarios keep their EB-2 outcome +
  `content_hash` stability (flag off); full pytest regression green.
- **Per-analyzer golden tests:** fixed evidence → fixed hypothesis set.
- **Classification tests:** new keywords never re-route existing fixtures.
- **Determinism test:** flag-on runs reproduce byte-identically (EB-2 harness).

---

## 12. Rollback Strategy

- **Per-domain flags default false** → instant rollback by flipping one env var;
  no redeploy of logic.
- **Independent domains** — a faulty domain is disabled without touching others.
- **Git-revertable** — each domain is a self-contained additive commit (worker +
  playbook + extractor + analyzer + tests); revert restores prior state exactly.
- **Blast radius bounded** — because existing analyzer bodies are never edited, a
  rollback cannot corrupt the current deterministic RCA path.

---

## 13. Honest Assessment

- **Acquisition ≠ reasoning.** The load-bearing lesson from EB-3: fetching evidence
  changes nothing unless an analyzer consumes it. Any plan that adds workers +
  playbooks without extending links 7-8 will reproduce the ThousandEyes no-op.
- **ThousandEyes is high-count, low-value.** 17 EFIC scenarios, but its evidence is
  overwhelmingly *negative* ("network healthy → rule out network"); decisive in 1.
  Prioritizing by scenario count would mis-invest. Prioritize by decisive-evidence
  contribution (AWS, DNS, identity, certs, Autosys).
- **This requires lifting the "do not modify the engine" rule.** IE-1 through IE-5
  edit worker/playbook/analyzer code. That is the exact constraint EB-3 escalated;
  IE-1 is the design that a decision to lift it would authorize. Nothing here
  should be implemented until that decision is explicit.
- **The real risk is to the crown jewel.** The deterministic RCA analyzers are the
  most valuable, most-tested asset. The mitigation posture (additive-only, optional
  param, per-domain flags, EB-2 + golden gates) is designed so the *downside is
  bounded to "new domain does nothing"*, never "existing diagnosis breaks."
- **Unknown:** whether the deterministic analyzers can name enterprise root causes
  with the phrasing EFIC keyword-grades (EB-2 showed today's RCA is symptom-level).
  New analyzers must emit root-cause text aligned to the failure mode, not just the
  symptom — a reasoning-quality task beyond mere acquisition.

---

## 14. Final Recommendation

**Proceed to a single, scoped, reviewed pilot (IE-2), not a broad rollout.** Pick
the highest decisive-value, lowest-classification-risk domain — **DNS/Route53**
(already classifies to `network`; 2 decisive EFIC scenarios) — and implement the
**full 8-link chain** for it alone, behind `IE_DNS_ENABLED` (default false),
bespoke (Option A). Gate acceptance on EB-2: the DNS scenarios move FAIL→PASS and
**every other EFIC scenario + the full regression is unchanged with the flag off
and non-regressed with it on**. Only after that end-to-end proof do IE-3/IE-4
extend to the remaining domains and IE-5 extract the declarative registry.

Do **not** implement until the decision to lift the no-engine-change constraint is
explicit, and do **not** add acquisition without the matching reasoning (links
7-8) — that is the difference between capability and theater.

**Deliverable status:** architecture only. No engine, worker, playbook, analyzer,
EnterpriseBench, or EFIC code changed in this cycle. Awaiting review.
