# EnterpriseBench — Architecture, Gap Analysis & Implementation Roadmap

**Design only.** Per the execution strategy, this document is the architectural
design + gap analysis + phased roadmap. **No code is implemented in this cycle;
implementation begins only after this architecture is reviewed.**

EnterpriseBench is SentinelAI's **Enterprise Digital Twin + Continuous
Evaluation Platform**: a deterministic, hermetic, additive subsystem that
simulates an enterprise across every supported MCP, generates cross-platform
telemetry with known ground truth, drives the **black-box** investigation
engine, and scores every commit for investigation quality — so no change reaches
production without an objective quality measurement.

> **Prime directive:** EnterpriseBench modifies nothing in the engine, replay,
> evidence, confidence, or planner. It treats SentinelAI as a black box, feeding
> it telemetry through the existing MCP boundary and reading its outputs.

---

## 0. Honest framing (what this can and cannot prove)

- EnterpriseBench proves the engine is **correct, deterministic, and replayable
  against known ground truth** — RCA accuracy, evidence completeness, confidence
  calibration, replay fidelity, cross-MCP reasoning. This is real, machine-
  measurable validation.
- It **does not** prove real-operator outcomes. Part 8 ("operator effectiveness")
  is a **deterministic operator *model***; its numbers are synthetic-operator
  metrics for regression, **not** real human MTTI/trust. Those remain
  `NOT_MEASURED` until a supervised pilot (`docs/pilot/`). EnterpriseBench and the
  pilot are complementary, not substitutes. This distinction is load-bearing and
  must appear in every EnterpriseBench report.

---

## 1. Gap analysis — reuse before build (the most important section)

A large fraction already exists. EnterpriseBench **composes** it; it does not
re-implement it.

| Requirement | Already exists (reuse) | Gap to build |
|---|---|---|
| **Evaluation scorer (Part 7)** | `sentinel_core/eic/benchmark.py` — `score_submission` computes 10 dims (rca_correctness, localization, false_lead_avoidance, decisive_evidence_latency, evidence_efficiency, distractor_avoidance, hypothesis_quality, confidence_calibration, explainability, replayability) + `leaderboard` with bootstrap CIs; `investigation_value/gold_standard.py` (IQS), `metrics.py`, `effectiveness.py`, `scientific_validation` (calibration, McNemar/bootstrap, per-class) | latency + runtime-cost + operator-readability dims; a corpus-wide **runner/aggregator** |
| **MCP simulators (Part 2)** | `workers/mcp_client.py::_STUB_DISPATCH` — 9 deterministic simulators (splunk, dynatrace, sysdig, servicenow, moogsoft, kubernetes, github, confluence, signalfx); `scripts/stub_mcp_server.py`; `agui/synthetic_generator.py` | **missing simulators:** ThousandEyes, AWS/CloudWatch, Autosys, Identity/LDAP, CMDB-as-source, Route53/DNS, Certificates; existing stubs return **empty** fixtures — need incident-correlated responses |
| **Enterprise topology / CMDB (Part 1)** | `intelligence/causal_graph.py::ServiceNode` (team, tier, dependencies, health) | full org model (business units, AWS accounts, regions/AZs, LBs, gateways, DBs, queues, caches, IdP, DNS, certs, batch, criticality) with **stable deterministic identities** |
| **Incident library + ground truth (Parts 4/5/10)** | `eval/enterprise/corpus.json` (8 incidents, 13 sources, ground truth + traps, content-addressed); `eval/eic/tasks` (6); `eval/gold_standard` (3); `eval/ground_truth.json` (3) | scale to **100 families × 20+ variants**; the **mutation engine** (Part 6); cross-MCP correlation generator |
| **Replay / determinism (Part 9)** | `supervisor/replay.py`, `supervisor/frozen_corpus.py`; 5,982-test regression harness | a bench **CI runner** that runs the corpus, scores, diffs vs a versioned baseline, flags statistically-significant regressions |
| **Operator metrics (Part 8)** | `agui/operator_telemetry.py` (operator MTTI, escapes, decision quality), `agui/mtti.py` (system MTTI), `agui/improvement_engine.py` (ROI backlog) — **all Part-8 metrics already computed** | a deterministic **operator model** that emits synthetic interaction events to feed those computations |
| **Reporting (Part 10)** | `investigation_value/nightly.py`, `metrics.py`, `effectiveness._trend` | EnterpriseBench-specific **coverage matrices** (incident/MCP/scenario/capability) + versioned report bundle |
| **Injection boundary** | `workers/mcp_client.py` gateway-vs-stub switch (`GATEWAY_MODE`) | a **bench MCP source** that serves EnterpriseBench telemetry through the existing stub-dispatch path (no engine change) |

**Net:** ~40% of EnterpriseBench is reuse (scorer, operator metrics, replay,
9 simulators, corpus seed); ~60% is new but thin (twin model, generation/mutation
engine, ~7 missing simulators, CI runner, coverage reporting).

---

## 2. Architecture overview

```
                    EnterpriseBench (additive, hermetic, deterministic)
   ┌──────────────────────────────────────────────────────────────────────┐
   │ Enterprise Twin ──► Incident Library ──► Mutation Engine               │
   │ (org/topology/CMDB) (100 families)     (20+ variants/family)          │
   │        │                    │                    │                      │
   │        └──────────► Telemetry Generators ◄───────┘                     │
   │            (cross-MCP correlated, content-addressed, ground-truth-tied) │
   │                              │                                          │
   │                              ▼                                          │
   │            Bench MCP Source (serves via existing stub dispatch)         │
   └──────────────────────────────┬───────────────────────────────────────┘
                                   │  (black box; no engine change)
                                   ▼
        SentinelAI investigate() ──► result + receipts + replay
                                   │
   ┌──────────────────────────────▼───────────────────────────────────────┐
   │ Evaluation Engine (REUSE eic.score_submission + IQS + operator model)  │
   │  RCA · evidence · calibration · recommendation · replay · latency · cost│
   │                              │                                          │
   │                              ▼                                          │
   │ Regression Runner (baseline diff, CI gate) ──► Reports + Coverage       │
   └────────────────────────────────────────────────────────────────────────┘
```

**Determinism contract:** every entity id, timestamp, telemetry payload, and
task hash is derived from a content-addressed seed (`sha256`) — no wall-clock, no
randomness (reusing the discipline in `sentinel_core/eic` and `eval/enterprise`).
A dataset version = the sha of its generator inputs; regeneration is byte-
identical.

---

## 3. Directory structure (specification, not scaffolding)

Independent top-level `enterprisebench/` subsystem; datasets under `eval/`.
Directories are **created as their phase is implemented**, not pre-stubbed.

```
enterprisebench/
  enterprise/        # deterministic org/topology/CMDB twin model
  mcp_simulators/    # bench simulators; reuse workers/_stub_* where present
  telemetry/         # cross-MCP correlated telemetry generators
  incidents/         # incident-family definitions
  mutations/         # deterministic variant generators
  ground_truth/      # ground-truth schema + builders (reuse eic.make_task)
  scenarios/         # composed scenario specs (family × mutation × twin slice)
  evaluation/        # runner that calls eic.score_submission + IQS + operator model
  scoring/           # thin: latency/cost/readability dims + aggregation
  regression/        # baseline store + CI diff + significance gate
  operator/          # deterministic operator model -> operator_telemetry events
  reporting/         # coverage matrices + versioned report bundle
  schemas/           # JSON schemas for every artifact
  docs/              # component docs
eval/enterprisebench/  # generated datasets + baselines (committed, versioned)
```

---

## 4. Component specifications + public interfaces (design)

Interfaces are pure, deterministic, JSON-safe; each composes existing code.

- **EnterpriseTwin** — `build_twin(seed) -> Twin`. Emits stable entities
  (business_unit, application, service, cluster, db, queue, cache, gateway, lb,
  idp, dns, cert, batch_job, aws_account/region/az) + `dependencies` + `owner` +
  `criticality`. Reuses `causal_graph.ServiceNode` shape.
- **IncidentLibrary** — `families() -> list[FamilySpec]`;
  `instantiate(family, twin) -> IncidentSpec` (signal + fault entity + ground
  truth + expected evidence/confidence-range/recommendation).
- **MutationEngine** — `mutate(family) -> list[VariantSpec]`; each variant
  preserves deterministic ground truth (Part 6).
- **TelemetryGenerators** — `generate(incident, twin) -> {mcp: payload}`;
  cross-MCP correlated, tied to ground truth (Part 5). One generator per MCP.
- **BenchMCPSource** — serves generated telemetry through the existing
  `workers/mcp_client` stub dispatch so the black-box engine reads it unchanged.
- **EvaluationEngine** — `evaluate(task, result) -> ScoreCard`. **Reuses**
  `eic.make_task`/`score_submission`/`leaderboard` + `investigation_value` IQS;
  adds latency/cost/readability; never re-implements the 10 EIC dims.
- **OperatorModel** — `simulate(result) -> list[operator_event]` (deterministic);
  fed to the existing `agui.operator_telemetry` + `mtti` computations. Output
  labelled **synthetic-operator** everywhere.
- **RegressionRunner** — `run(corpus, engine) -> RunReport`;
  `diff(baseline, report) -> {regressions, significance}`; CI gate.
- **Reporter** — `report(runs) -> {enterprise_health, quality, calibration,
  coverage:{incident,mcp,scenario,capability}, trends}`; deterministic, versioned.

---

## 5. Data schemas (design)

All under `enterprisebench/schemas/` (JSON Schema), all content-addressed:

- **Twin** `{schema_version, seed, entities[], dependencies[], hash}`
- **IncidentSpec** `{incident_id, family, variant, twin_ref, signal, ground_truth
  {root_cause, root_cause_service, necessary_evidence, decisive_evidence},
  expected {confidence_min/max, recommendation}, traps, hash}` — a superset of
  the existing `eval/enterprise` entry, so the Phase-1 corpus migrates cleanly.
- **TelemetryBundle** `{incident_ref, by_mcp:{...}, correlation_hash}`
- **ScoreCard** — the EIC score dict + `{latency_ms, runtime_cost, readability,
  operator:{synthetic:true, mtti_ms, acceptance_rate}}`
- **RunReport / Baseline** — `{version, corpus_hash, engine_commit, scores[],
  aggregates, coverage}`

---

## 6. Continuous regression + CI (Part 9)

- A `pytest`-invocable entrypoint runs a **bounded** bench corpus per commit
  (fast tier), plus a nightly full-corpus run.
- Scores diff against a committed `Baseline`; a **statistically significant**
  degradation (bootstrap CI on the EIC composite, reusing `eic.leaderboard`)
  fails the gate. Below sample-size threshold → `NOT_MEASURED`, never a spurious
  block.
- Every run is reproducible (corpus_hash + engine_commit recorded).

---

## 7. Quality gates (self-imposed on EnterpriseBench)

Deterministic · replayable · versioned · CI-friendly · offline · hermetic ·
regression-safe · evidence-backed · scalable · extensible. Every generated
dataset regenerates byte-identically (guarded by a committed-file test, as
`eval/enterprise` already does).

---

## 8. Phased implementation roadmap (ordered by dependency, risk, validation value)

Each phase is a separate, reviewed, regression-green increment. **Nothing below
is implemented yet.**

| Phase | Deliverable | Reuses | New | Validation value | Risk |
|---|---|---|---|---|---|
| **EB-0** | Migrate `eval/enterprise` into the EnterpriseBench schema; wire the **EvaluationEngine** over `eic.score_submission` + a corpus **runner** | EIC scorer, enterprise corpus | runner + ScoreCard | **High** (measures the engine today) | Low |
| **EB-1** | **EnterpriseTwin** model (deterministic org/topology/CMDB) | `causal_graph.ServiceNode` | twin builder + schema | High (grounds everything) | Low |
| **EB-2** | **BenchMCPSource** + fill incident-correlated telemetry for the 9 existing simulators | `_STUB_DISPATCH` | correlation generators | High (real black-box runs) | Med |
| **EB-3** | **Missing MCP simulators** (ThousandEyes, AWS/CloudWatch, Autosys, Identity, DNS/Route53, Certificates) | stub pattern | 6–7 simulators | Med | Med |
| **EB-4** | **IncidentLibrary + MutationEngine**: grow families toward the 100×20 target incrementally (start ~10 families × 5 variants; scale) | enterprise corpus | family/variant generators | High (coverage) | **High (scale)** |
| **EB-5** | **RegressionRunner + CI gate** (fast tier per commit; nightly full) | replay, `leaderboard` CIs, pytest | baseline diff + gate | High (protects every commit) | Med |
| **EB-6** | **OperatorModel** → synthetic operator metrics (clearly labelled) | `operator_telemetry`, `mtti` | deterministic operator model | Med (regression only; not real outcomes) | Med |
| **EB-7** | **Reporting + coverage matrices**, versioned bundle | `investigation_value` reports | coverage/report builder | Med | Low |

**Sequencing rationale:** EB-0/EB-1/EB-2 deliver real engine measurement fastest
by composing what exists; EB-4 (the 2000-incident generation) is the largest and
riskiest and is deliberately incremental; EB-6 is last and lowest-authority
(synthetic operators, explicitly not the pilot).

---

## 9. Deliverables status (this cycle)

Architecture ✅ · directory structure ✅ · component specs ✅ · public interfaces
✅ · data schemas ✅ · enterprise-sim / MCP-sim / telemetry / incident /
ground-truth / evaluation / regression / reporting frameworks **designed** ✅ ·
documentation (this) ✅ · phased implementation plan ✅. **Implementation: not
started** (awaiting review, per the execution strategy).

## 9a. EB-0 — Baseline Evaluation Runner (IMPLEMENTED)

The first executable slice. Package `enterprisebench/` — additive, offline,
deterministic; treats SentinelAI as the system under test (no engine/replay/
evidence/confidence/planner change).

**Pipeline:** Corpus Discovery → Schema Validation → Scenario Loading →
Investigation Execution (pluggable) → Ground-Truth Evaluation (REUSE
`eic.score_submission`) → scorer-determinism check → Scenario Report → Aggregate
(`eic.leaderboard`) → Baseline Comparison → Exit decision.

**Missing-contract note (documented, not faked):** the enterprise corpus
telemetry is **not** wired into the live investigation engine — nothing feeds it
into `workers/mcp_client`. That injection is **EB-2 (`BenchMCPSource`)**, not
built. Therefore EB-0's *investigation execution* is a pluggable
`SubmissionProvider`: the default (`no_engine_provider`) returns `None`, so each
scenario is honestly `NOT_MEASURED`; a `file_provider({task_id: submission})`
supplies neutral submissions (how a future EB-2/engine run — or a test — feeds
results). EB-0 **never fabricates an investigation**. Engine *replay* validation
is likewise deferred to EB-2 (`replay_status: NOT_MEASURED` with reason);
EB-0 validates *scorer* determinism (score twice → byte-identical).

**Outcomes:** `PASS | FAIL | SKIPPED | UNSUPPORTED | NOT_MEASURED | ERROR`
(never collapsed). **Exit codes:** `0` ok · `1` regression · `2` invalid
corpus/config · `3` execution error.

**Corpus contract:** hard-rejects (exit 2) duplicate ids, missing ground-truth
root cause, malformed `necessary_evidence`, invalid confidence bounds,
unsupported corpus schema; per-scenario task-schema mismatch → `UNSUPPORTED`.
Deterministic ordering (by `task_id`); order-independent `corpus_hash`. No
coercion, no fabricated fields.

**Evaluation contract:** composes the EIC scorer (10 dimensions, each raw + a
`measured`/`NOT_MEASURED` state) — no scoring formula re-implemented; operator-
facing checks reuse `eval.enterprise.validate.check_expected`. No placeholder
numerics. No claim of real operator trust / adoption / human-MTTI / production
effectiveness — those stay `NOT_MEASURED`.

**Reporting:** deterministic `enterprisebench_report.json`,
`scenario_results.jsonl`, `baseline_summary.json` (+ optional `.md`). A
`content_hash` covers all but the documented volatile fields
(`run_timestamp`, `runtime_ms`, `commit`); no machine-specific absolute paths.

**Baseline contract:** explicit deterministic thresholds only (default composite
drop > 0.02); detects pass→fail, score degradation, determinism regression,
replay-status change, newly-unsupported, missing scenarios, scorer/corpus change.
**No auto-update** — a baseline is only written by `baseline create`.

**CLI (stdlib argparse, no new dependency):**
```
python -m enterprisebench run [--scenario ID] [--subset a,b] [--submissions PATH]
    [--out DIR] [--markdown] [--baseline PATH] [--fail-on-regression]
python -m enterprisebench baseline create --output PATH [--submissions PATH]
```

**CI:** offline, pytest-invocable; `tests/enterprisebench/test_eb0.py` (21) runs
the fast path per commit. The full-corpus lane (once EB-2 supplies engine
submissions) is scheduled/explicit — EB-0 makes **no** per-commit full-corpus
coverage claim.

**Tests (21):** deterministic ordering, duplicate/malformed/unsupported
rejection, scorer composition, NOT_MEASURED preservation, deterministic report
serialization, repeated-run equivalence (content-hash equal), baseline
comparison + regression exit code, explicit baseline creation, **no automatic
baseline mutation**, scenario filtering, offline default-corpus run.

**Known limitations:** engine execution + engine replay against the corpus are
deferred to EB-2; without supplied submissions every scenario is `NOT_MEASURED`
(by design); statistical-significance gating is deferred (explicit thresholds
only).

## 9b. EB-2 — Investigation Evaluation Pipeline (IMPLEMENTED)

The missing contract EB-0 documented — corpus telemetry wired into the *live*
investigation engine — is now built. Package `enterprisebench/pipeline/`
(additive; no engine/planner/runtime/replay/scoring change). It drives the
**unmodified** `SentinalAISupervisor.investigate(incident_id)` end-to-end against
EFIC telemetry and evaluates the engine's investigation *process* against the
hidden Enterprise Investigation Specification.

**Pipeline:** EFIC task → **telemetry render** (`render.py`) → **BenchMCPSource**
(`bench_source.py`) → **unmodified `investigate()`** (isolated subprocess,
`execute.py` + `_isolated_worker.py`) → **trace capture** → **reasoning
evaluation** (`evaluate.py`, REUSES `eic.score_submission` + the
`sentinelai_submission` adapter) → per-scenario score → deterministic **report**
(`run.py`, `__main__.py`).

**BenchMCPSource** duck-types `workers.mcp_client.McpGateway` (`invoke` +
`discover_tools`) and is injected via the engine's own
`SentinalAISupervisor(gateway=...)` seam — the single boundary every worker
funnels through. It responds ONLY to queries the engine issues; unmatched queries
fall through to the real stub dispatch, so absent evidence returns the exact
production-shaped empty (a genuine "no data", not an error or a hint). It respects
the query's server + action and records the full MCP interaction stream.

**Telemetry rendering** maps EFIC's abstract per-source telemetry into the exact
response schemas the engine reads (splunk logs/changes, dynatrace golden signals,
sysdig metrics/events, servicenow change-records/CI, moogsoft incident). Sources
with no native engine channel (certificates, route53_dns, identity, aws_cloudwatch,
autosys, cmdb, …) are folded into Splunk log lines — how such failures actually
surface to a log-first investigation — carrying only the observable *symptom*,
never the hidden root-cause sentence.

**Isolation invariant (proven):** the source is built from a `RenderedScenario`
derived from the PUBLIC `task.incident` + `task.telemetry` only. `ground_truth`,
`traps`, the `efic` block, and `investigation_spec` NEVER reach the engine.
Tests: `render()` is byte-identical under a scrambled answer key; no rendered
channel contains the root-cause sentence; and no `supervisor/` / `workers/` /
`intelligence/` module references `investigation_spec`.

**Evaluated configuration (fixed for every scenario, recorded in the report):**
the deterministic reasoning core — LLM refinement OFF (non-deterministic, needs
network), cross-incident learning neutralized (empty learning state, per-incident
isolation), engine reasoning stack ON (hypothesis engine, causal localization,
decision intelligence). These are the engine's own flags; no code is changed.

**Determinism:** each scenario runs in a fresh isolated subprocess with empty
learning state (no in-memory singleton or background-write leakage); concurrency-
ordered trace fields (the engine dispatches collection workers on a thread pool)
are canonicalized; the report excludes volatile timing from its `content_hash`.
The engine's one repo-anchored side-effect (an episodic-memory append, never read
back) is sandboxed so a run leaves the working tree unchanged. The full 30-scenario
run reproduces byte-identically (`content_hash` stable across runs).

**Honest first measurement (30 EFIC scenarios, this engine, this config):** the
engine **localizes** the failing service well (localization 0.97, 29/30) and
**collects the right evidence across MCPs** (evidence-collection 0.98, cross-MCP
1.0, recommendation present 1.0), but does **not name the enterprise root cause**
(rca_correctness 0/30 by keyword grading — root causes are symptom-level), its
confidence is suppressed below the expected band by the anti-hallucination
citation gate (confidence-in-range 1/30), and decisive-evidence attribution /
multi-hypothesis elimination are not exposed in this configuration. 11/30
scenarios reference an MCP the engine cannot yet query (the EB-3 simulator gap);
only 1 has fully-unreachable decisive evidence. Process dimensions the engine
does not expose (blast radius, business context, confidence-evolution trajectory,
evidence-attribution detail, recovery validation) are reported `NOT_MEASURED`,
never faked. This is a real, reproducible measurement of the gap between the
engine's current capability and the EFIC enterprise-realism bar.

**CLI:** `python -m enterprisebench.pipeline run [--only IDs] [--out DIR]
[--markdown] [--threshold T]`. **Tests:** `tests/enterprisebench/test_eb2.py`
(11) — isolation proof, production-parity source behavior, evaluator reuse +
`NOT_MEASURED` honesty, end-to-end determinism, and no-repo-pollution.

**Known limitations:** EB-2 measures the deterministic core (LLM-refinement layer
excluded); it evaluates the current engine, whose default RC phrasing is
symptom-level; ~11/30 scenarios reference MCPs the engine cannot query (EB-3 §9c
found this is an engine-consumption gap, not a twin gap — simulators alone do not
close it);
golden-signal magnitudes are synthesized generically (specifics come from
log/event/change text); process depth is bounded by what the engine exposes.

## 9c. EB-3 — Enterprise Digital Twin: MCP simulators + reachability wall (IMPLEMENTED)

EB-3 set out to give every EFIC-required MCP a dedicated, production-shaped
simulator so investigations run on realistic native sources rather than folded
substitutes. A mandatory reachability analysis (cited below) established the
**load-bearing finding**: under the hard constraint *"do not modify the
investigation engine"*, almost none of these MCPs can be reached.

**Reachability (unmodified engine, env/config only — proven, with cites):**

| MCP | Engine-reachable? | Evidence |
|---|---|---|
| thousandeyes | **Yes, via `ENABLE_THOUSANDEYES_RCA=true`** | `network_worker` (`workers/network_worker.py:33`) + timeout/latency/network playbook steps (`supervisor/tool_selector.py:37,57,71`) |
| certificates, route53/DNS, identity/IAM, aws_cloudwatch, autosys | **No** | no worker, no playbook step, no gateway mapping, no flag — only appear as classification keywords/log hints. Querying them requires **adding a worker + playbook step** (an engine code change the mission forbids). |
| cmdb | **Not a distinct MCP** | served through `servicenow.get_ci_details` (already supported) |

**Delivered (additive, honest):**
- **ThousandEyes simulator** (`enterprisebench/pipeline/simulators.py`) — deterministic, production-shaped (`te_list_alerts`/`te_get_test_results`/`te_list_tests`); healthy probes yield NO alerts (genuine negative evidence), positive signals (packet loss / DNS failure / TLS error) raise active alerts. Wired via the engine's own `TE_USE_FIXTURES`/adapter transport seam (subprocess-local injection; no engine source change) and activated by the engine's `ENABLE_THOUSANDEYES_RCA` flag.
- **CMDB → ServiceNow CI**: EFIC `cmdb` evidence (config versions, registry) is surfaced in `servicenow.get_ci_details` — its production equivalent the engine actually queries — not folded.
- **Folding removed**: `render.py` no longer flattens non-native sources into Splunk log lines. Each source now maps to a native channel or is marked engine-unreachable in provenance. The observable symptom still reaches the engine via native EFIC-authored Splunk telemetry.

**The decisive, honest result:** activating ThousandEyes is a **measured no-op** — `TE-on == TE-off` (identical root cause + confidence on the scenario where TE is decisive, EFIC-NET-LOSS-001). The deterministic analyzer does not convert network evidence into a different diagnosis. Across the full corpus, removing folding + adding the reachable simulators moved mean EIC only 0.277 → 0.278. **Improving twin fidelity does not change what the black-box engine investigates, because the engine has no consumption path for these MCPs.** The gap is engine-side, not twin-side.

**Consequence for the mission:** the EB-3 objective ("simulate every MCP so every investigation uses realistic native sources") is **not achievable under "engine untouched"** — 5 MCPs have no query path and the one reachable MCP is a no-op. Building dedicated simulators for the 5 unreachable MCPs would be unconsumed theater and is deliberately *not* done. To make the twin consumable, the engine must gain workers + playbook steps for these MCPs — an architectural decision that requires lifting the no-engine-change rule. **Decision: EB-3 INCOMPLETE** (reachable fidelity delivered; the remaining frontier is blocked by the constraint and escalated for review).

**Tests:** `tests/enterprisebench/test_eb3.py` (9) — simulator determinism + shape, healthy=no-alerts / positive=alerts, folding removed, cmdb→ServiceNow-CI, engine-unreachable MCPs documented. EB-2 pipeline unchanged and still deterministic (`test_eb2.py` green).

## 10. Recommendation

Proceed to **EB-0** first (evaluation runner over the existing EIC scorer +
enterprise corpus) — it converts the platform's existing, unrun evaluation
assets into a per-commit investigation-quality measurement with the least new
code and the highest immediate value, and it validates the whole EnterpriseBench
contract on a small corpus before scaling generation (EB-4). Do not begin EB-0
until this architecture is reviewed.

*Update: EB-0, EB-2, and EB-3 are now implemented (§9a, §9b, §9c). EB-3
established that the Enterprise Digital Twin cannot be completed for EFIC's MCPs
without modifying the black-box engine (5 MCPs unreachable; the one reachable MCP
is a no-op in the deterministic analyzer). The highest-value next objective is
therefore an **architectural decision, not another simulator phase**: either
(a) sanction a bounded, reviewed extension of the engine's worker/playbook layer
so it can query certificates/DNS/identity/aws/autosys (lifting the no-engine-change
rule for a scoped increment), or (b) accept that these failure modes are
investigated only through Splunk/ServiceNow and re-scope EFIC accordingly. Both
are decisions for the review board; EB-3 does not force either.*
