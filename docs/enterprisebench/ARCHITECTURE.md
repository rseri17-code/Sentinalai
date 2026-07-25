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

## 10. Recommendation

Proceed to **EB-0** first (evaluation runner over the existing EIC scorer +
enterprise corpus) — it converts the platform's existing, unrun evaluation
assets into a per-commit investigation-quality measurement with the least new
code and the highest immediate value, and it validates the whole EnterpriseBench
contract on a small corpus before scaling generation (EB-4). Do not begin EB-0
until this architecture is reviewed.
