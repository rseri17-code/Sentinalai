# SYSTEM_REALITY.md
**SentinelAI — Production Readiness Certification · Phase 0**
Whole-repository subsystem inventory. 577 Python modules, 56 `*_ENABLED` flags,
`supervisor/` 41K LOC. Classifications from import-graph + flag-polarity + call-site evidence.

## Global controls
- **Live path:** `supervisor/agent.py::investigate()` → `phases/{fetch,classify,collect,analyze,persist}.py`.
- **`EVIDENCE_GATES_ENABLED`** (default **ON**) — the **only fail-closed** output control.
- **`ENABLE_INTELLIGENCE_RUNTIME`** (default **OFF**) — gates the entire
  `intelligence_modules/*` cluster (including the dormant `deterministic_planner`).
- **Determinism scope:** engine + replay deterministic; three default-ON learning stores
  (`experience_store`, `strategy_evolver`, `knowledge_graph`) write wall-clock timestamps
  into persistent JSON that primes future runs → pipeline determinism is conditional on
  frozen stores (see DETERMINISM_REPORT caveat).

## A — Five phases (all RUNTIME / PRESENT / not removable)
| Phase | Flag | Determinism | Failure mode |
|---|---|---|---|
| FetchPhase `phases/fetch.py` | none | deterministic (`time.monotonic` deadline only) | fail-open; empty/meta → graceful `_empty_result` |
| ClassificationPhase `phases/classify.py` | none | deterministic; submits enrichment futures | fail-open |
| CollectPhase `phases/collect.py` | `AGENTIC_PLANNER`=false | deterministic dispatch; worker I/O external | fail-open per await; **fail-closed** G1/G4 gate |
| AnalyzePhase `phases/analyze.py` | hosts 5 shadow flags (OFF) | deterministic core | fail-open enrich; **fail-closed** G2/G3/G5 (can rewrite root_cause→BLOCK) |
| PersistPhase `phases/persist.py` | none | deterministic writes; heavy writes best-effort | fail-open; deadline guard skips writes |
| Core RCA `agent.py::_analyze_evidence` | `LLM_ENABLED` (refine) | deterministic rule engine | fail-open |

## B — Live planner + workers (RUNTIME / not removable)
- **Live planner** = `tool_selector.get_evolved_playbook` (deterministic stable-sort) +
  `strategy_evolver.get_weights` (advisory reorder). **NOT** `deterministic_planner/`.
- **Workers** `workers/*` via `_call_worker` — sole evidence source; fail-open
  (`{"error":…}` + 1 retry); output is external I/O.
- **strategy_evolver** — advisory reordering, default ON; writes `datetime.now`.

## C — Shadow investigation engines T1–T5 (SHADOW / PRESENT / default OFF)
Imported **unconditionally** in `analyze.py:308-351` (so a bare file delete would
`ImportError` the live path) but each self-guards on its OFF flag and no-ops. All
deterministic (no clock/rng/uuid), all fail-open ("never raises").
| Engine | Flag (OFF) | Tests |
|---|---|---|
| `hypothesis_engine.py` | `HYPOTHESIS_ENGINE_ENABLED` | tranche1 |
| `adaptive_investigation.py` | `ADAPTIVE_INVESTIGATION_ENABLED` | tranche2 |
| `causal_investigation.py` | `CAUSAL_INVESTIGATION_ENABLED` | tranche3 |
| `validation_engine.py` | `VALIDATION_ENGINE_ENABLED` | tranche4 |
| `decision_intelligence.py` | `DECISION_INTELLIGENCE_ENABLED` | tranche5 |
> NOTE: `supervisor/intelligence_modules/decision_intelligence.py` is a **different**
> module (`ENABLE_DECISION_INTELLIGENCE`) — do not confuse with the T5 engine.

## D — Live intelligence subsystems (RUNTIME / advisory / not removable)
`experience_store` (ON), `confidence_calibrator` (OFF→passthrough), `knowledge_graph`
(ON), `evidence_citation.annotate_citations` (ON, floor 0.70), `evidence_gates` G1–G5
(ON, **fail-closed**), `grounding_confidence` (best-effort), `replay.ReplayStore` (active
only when replay dir set). All advisory except evidence_gates (active output control).

## E — sentinel_core clusters (SHADOW / reachable-but-OFF / removable at default)
`intel_memory` (produce-only, nothing reads back), `investigation_artifact`
(`INVESTIGATION_ARTIFACT_ENABLED` OFF), `investigation_value` (readiness/nightly, offline),
`strategy_optimizer` / `hypotheses` / `causal_graph` (reached only via the OFF tranche
engines), `continuous_learning` (**zero non-test importers**), `deterministic_planner/`
(double-gated OFF, dormant). All removable with flags at default without changing live
output — but several are the substrate for future Wave 3, so "removable" ≠ "should remove."

## F — Offline / benchmarking (SHADOW / UNWIRED)
- **`sentinelbench/`** — CLI eval harness. **FINDING:** `runner.py:25 _build_fixture_result`
  scores **synthetic** fixture results; it does **not** call `investigate()`. If treated
  as end-to-end validation of the live pipeline, that is a certification gap (leaky/proxy
  metric) — see SCIENTIFIC_VALIDATION_AUDIT and backlog V-4.
- **`tests/replay/`** — offline harness, own store.

## Not runtime debt
`agents/`, `skills/`, `tasks/` (`.md` specs + memory, 0 Python); `ui/` (separate frontend).

## Classification summary
| Class | Count (subsystems audited) | Examples |
|---|---|---|
| RUNTIME / PRESENT | ~14 | 5 phases, core RCA, workers, planner, gates, citation, calibrator, KG, experience store |
| SHADOW / PRESENT (wired, OFF) | 6 | T1–T5 engines + evidence_ledger shadow |
| SHADOW / UNWIRED (removable) | ~9 | intel_memory, artifacts, readiness, strategy_optimizer, causal_graph, planner, continuous_learning |
| DEAD / test-only | 1 | `intelligence/confidence_calibrator.py` (see TECHNICAL_DEBT) |
| NOT runtime (docs/frontend) | 4 dirs | agents/, skills/, tasks/, ui/ |

## Certification-relevant findings surfaced in Phase 0
1. **Determinism is engine-scoped, not pipeline-scoped** (3 stateful learning stores). 
2. **SentinelBench validates synthetic fixtures, not live output.** 
3. **Fail-closed surface is thin** — only `evidence_gates`; every learning/enrichment
   write is fail-open, so a store failure degrades silently rather than surfacing. 
4. **The 5 tranche engines are wired-but-OFF** — RUNTIME wiring, SHADOW behavior.
