# EXECUTION_TRACE.md
**SentinelAI — Production Readiness Certification · Phase 1**
One investigation, traced end-to-end with file:line evidence.

## Entry
`SentinalAISupervisor.investigate(incident_id, replay=False) -> dict` — `supervisor/agent.py:304`.
- Runs inside `with trace_span("investigate", ...)` (`agent.py:395`).
- **Replay short-circuit** (`agent.py:312-329`): if `replay=True` and a stored
  artifact exists, re-runs `_analyze_evidence` on stored evidence + `annotate_citations`
  and returns — the only path that bypasses the five phases (this is what makes
  an investigation replay-exact: pinned evidence → deterministic re-analysis).
- Phase modules lazily imported (`agent.py:335-346`); each phase wrapped in
  `with _phase_receipts.record(<name>)` (`agent.py:350`).

## Phase sequence
| # | Phase | Call site | Output |
|---|---|---|---|
| 1 | `FetchPhase` (`phases/fetch.py:41`) | `agent.py:407` | `fout` dict: incident, summary, service, receipts, budget, circuits, call_graph |
| 2 | `ClassificationPhase` (`phases/classify.py:93`) | `agent.py:416` | `ClassificationResult` (frozen) |
| 3 | `CollectPhase` (`phases/collect.py:78`) | `agent.py:424` | `CollectResult(evidence, early_return, gate)` |
| 4 | `AnalyzePhase` (`phases/analyze.py:97`) | `agent.py:435` | `AnalyzeResult` |
| 5 | `PersistPhase` (`phases/persist.py:82`) | `agent.py:452` | final dict (returned `agent.py:457`) |

**FETCH** creates the per-investigation handles (CallGraph, ReceiptCollector,
ExecutionBudget, CircuitBreakerRegistry — `fetch.py:131-137`), resets thread-local
state (`fetch.py:121-128`), fetches the incident, and early-returns for empty/meta
incidents. **CLASSIFY** derives `incident_type`, severity, and **replaces the budget**
with a severity-scaled one (`classify.py:206`), and submits 3 deferred futures
(experience, KG, historical). **COLLECT** is where the `evidence` dict is born and
mutated in place: planner-loop or playbook dispatch (`collect.py:163-170`), then
~10 enrichment merges (itsm, cmdb_blast_radius, diff_analysis, git_blame,
trace_correlation, visual_evidence), with an **Evidence Gate** that can BLOCK →
early return.

## Shadow hooks (AnalyzePhase)
Real RCA first: `_analyze_evidence` → calibrate confidence → grounding → self-critique
→ `annotate_citations` → **post-analysis gate** (can rewrite `root_cause="BLOCKED"`,
`confidence=0`) → `_evidence_snapshot`. Then five flag-gated, additive-only hooks in
fixed order (each writes only its `_*` key, never touches root_cause/confidence):

| Order | Call | `analyze.py` | Writes | Flag (default OFF) |
|---|---|---|---|---|
| 1 | `run_hypothesis_engine` | :309 | `_hypothesis_graph`, `_elimination_narrative`, `_counterfactual` | `HYPOTHESIS_ENGINE_ENABLED` |
| 2 | `run_adaptive_advisor` | :320 | `_adaptive_investigation` | `ADAPTIVE_INVESTIGATION_ENABLED` |
| 3 | `run_causal_investigation` | :330 | `_causal_investigation` | `CAUSAL_INVESTIGATION_ENABLED` |
| 4 | `run_validation_engine` | :342 | `_investigation_validation` | `VALIDATION_ENGINE_ENABLED` |
| 5 | `run_decision_intelligence` | :351 | `_decision_intelligence` | `DECISION_INTELLIGENCE_ENABLED` |

Order is deliberate: 4 composes 1-3; 5 composes 1-4.

## Persist
Observability → **LLM judge** (best-effort, structural-quality only, no ground truth,
try/except) → remediation → proposed-fix (only if `diff_analysis`) → dashboard metrics
→ **persist-deadline-gated** heavy writes (`persist.py:284-309`: if past deadline,
skip writes and set `confidence_degraded=True`) → `_persist_results` fan-out
(`agent.py:735-1187`: replay store, memory, KG, DB, RCA report + ~20 background
learning side-effects, each individually try/except + flag/availability guarded).

## State object (finding)
There is **no single threaded context**. The effective carriers are plain dicts:
`fout` (handles), `evidence` (source of truth, in-place mutated), `result` (RCA output),
plus `sup._tls` thread-local (deadline, current_incident). A frozen
`InvestigationContext` (`sentinel_core/context/investigation.py:96`) is constructed and
passed to every phase but is **effectively inert** in this path — only `incident_id`/
`investigation_id` are read; its typed handles stay `None`. **Architectural ambiguity
worth noting** (TECHNICAL_DEBT §coupling): the "shared state object" in the signatures
is not the real carrier.

## Failure paths
- **Phase boundary: FAIL-FAST.** No try/except around the five phase calls
  (`agent.py:406-457`); the receipt context manager records the error and **re-raises**
  (`phase_receipts.py:151-157`). An uncaught phase exception aborts the investigation —
  no partial result.
- **In-phase: GRACEFUL DEGRADATION.** Every sub-operation (future awaits, recurrence,
  grounding, self-critique, judge, persist side-effects) is individually try/except
  wrapped and degrades to `None`/`[]`/best-effort.
- **Degradation via early-return, not exception:** empty/meta incident, evidence-gate
  block, deadline-before-analysis, persist-deadline skip each return a degraded/empty
  result through `_finish()` (receipts + artifact still attached).

## Determinism-sensitive points (main path)
| Element | Location | Reproducibility |
|---|---|---|
| `time.monotonic()` deadlines, `span.elapsed_ms` → metrics/`time_to_resolve_ms` | fetch/analyze/persist | timing — inherently non-reproducible (side-channel, not root_cause) |
| `created_at = time.time()` on ctx | `context/investigation.py:135` | non-det, but `investigation_id` is deterministic (`inv-<id>`) |
| `uuid.uuid4()` for `Episode.episode_id` | `agent.py:1153` | side-effect record only, not in returned result |
| Timeout-gated future awaits (`timeout=3/5/10`) | `collect.py:219,263,371,391` | **RISK: an evidence key's presence can depend on scheduling under load** |
| ThreadPool merges | collect | content order-independent (keyed dict writes, `dict.fromkeys` dedup) — stable given identical worker results |

**Conclusion:** `root_cause`/`confidence`/reasoning are deterministic given deterministic
offline workers; timing, UUIDs, timestamps, and timeout-gated future availability are the
reproducibility-sensitive points, largely confined to observability/persistence
side-channels. The timeout-gated future availability is the one that can affect the
`evidence` set itself and is carried into FAILURE_MODE_ANALYSIS and the backlog.
