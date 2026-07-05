# Cross-Incident Causal Graph

**Status:** Landed at branch `claude/code-review-analysis-MelXd`.
**Location:** `sentinel_core/causal_graph/`.
**External dependencies:** none — stdlib only.

Offline deterministic RCA intelligence layer that connects incidents,
services, symptoms, hypotheses, evidence, root causes, and remediation
outcomes **across investigations**. Zero LLM. Zero network. Zero
runtime coupling.

## Non-goals

- Not a production runtime.
- Not autonomous remediation.
- Not a replacement for the per-investigation `KnowledgeGraph`
  (`sentinel_core/models/knowledge_graph.py`).
- Not a replacement for the persisted `intelligence/causal_graph.py`
  store — a different subsystem in a different top-level package.

## Data model

| Kind | Types |
|------|-------|
| Node (`CausalNodeType`, 12) | `incident`, `service`, `symptom`, `signal`, `hypothesis`, `evidence`, `root_cause`, `remediation`, `deployment_change`, `dependency`, `failure_mode`, `incident_pattern` |
| Edge (`CausalEdgeType`, 12) | `observed_in`, `caused_by`, `supports`, `disproves`, `precedes`, `correlates_with`, `resolved_by`, `affects`, `depends_on`, `recurs_with`, `reduces_mtti`, `increases_confidence` |
| Container | `CausalGraph`, `CausalChain`, `CausalPath`, `CausalRecurrence`, `RCAPath`, `MTTIPath`, `CausalRecommendation` |

## Graph construction

`CausalGraphBuilder.build(records)`:

For each `MemoryRecord`:
- `SERVICE` node (label = service, properties = application).
- `INCIDENT` node (label = incident_id, properties = memory_id, incident_type).
- `INCIDENT --AFFECTS--> SERVICE` edge.
- `FAILURE_MODE` node (label = incident_type) + `INCIDENT --CORRELATES_WITH--> FAILURE_MODE`.
- `ROOT_CAUSE` node (label = truncated root cause) + `INCIDENT --CAUSED_BY--> ROOT_CAUSE`, weight = confidence/100.
- `REMEDIATION` node + `ROOT_CAUSE --RESOLVED_BY--> REMEDIATION` + `INCIDENT --RESOLVED_BY--> REMEDIATION`.
- For each evidence key: `EVIDENCE` node + `EVIDENCE --OBSERVED_IN--> INCIDENT` + `EVIDENCE --SUPPORTS--> ROOT_CAUSE`.
- For each false lead: `SIGNAL` node + `SIGNAL --DISPROVES--> ROOT_CAUSE`.
- For each deployment-related skill: `DEPLOYMENT_CHANGE` node + `DEPLOYMENT_CHANGE --PRECEDES--> INCIDENT` + `--CAUSED_BY--> ROOT_CAUSE`.
- For each topology dependency: `DEPENDENCY` node + `SERVICE --DEPENDS_ON--> DEPENDENCY` + `DEPENDENCY --CORRELATES_WITH--> INCIDENT`.
- For each hypothesis in decision_trace: `HYPOTHESIS` node + `HYPOTHESIS --OBSERVED_IN--> INCIDENT` + `HYPOTHESIS --SUPPORTS--> ROOT_CAUSE`.

Cross-incident `RECURS_WITH`: any two incidents that share the same `ROOT_CAUSE` node are connected.

## Causal chain detection

`ChainDetector.detect(records, min_count=2)` groups records by tuple `(service, incident_type, root_cause, remediation)`. Any group of size ≥ `min_count` becomes a `CausalChain` with:

- `chain_id` (deterministic sha256[:16] of node sequence)
- `count`, `memory_ids`
- `confidence` (mean of record confidences / 100)
- `average_mtti_ms`

## RCA path ranking

`RCAPathRanker.build(records)` produces one `RCAPath` per `(service, symptom, root_cause)` triplet, sorted by `(recurrence DESC, confidence DESC, path_id)`.

## MTTI path ranking

`MTTIPathRanker.build(records)` produces one `MTTIPath` per `(service, root_cause, evidence_ordering, remediation)` combination, sorted by `(best_mtti_ms ASC, average_mtti_ms ASC, path_id)`.

## Recommendation kinds

`CausalRecommendationKind`:
- `recurring_root_cause`
- `fastest_rca_path`
- `high_risk_service`
- `reliable_remediation`
- `evidence_to_prove`
- `false_lead_to_skip`

Every recommendation includes an `evidence` tuple explaining WHY.

## Reports (deterministic JSON)

| Renderer | Contents |
|----------|----------|
| `render_causal_graph` | Full nodes + edges, sorted by id |
| `render_causal_chains` | All chains found by `ChainDetector` |
| `render_rca_paths` | All ranked `RCAPath` entries |
| `render_mtti_paths` | All ranked `MTTIPath` entries |
| `render_recurrence_report` | Recurrences across 5 dimensions (root_cause / service / symptom / evidence_pattern / remediation) |
| `render_service_causal_profile` | Per-service summary: incident count, top root causes, avg MTTI, avg confidence |
| `render_causal_recommendations` | All recommendations |
| `render_master_report` | All seven bundled |

## Reuse (no duplicate storage)

- **Incident Intelligence Memory** — `MemoryRecord` is the sole input. No new store.
- **Hypothesis Intelligence** — hypotheses embedded in `MemoryRecord.decision_trace.hypotheses` (when present) are surfaced as `HYPOTHESIS` nodes.
- **Strategy Optimizer** — the causal graph's `MTTIPath` set is compatible with `MttiEstimator`; strategy optimizer can feed into causal recommendations by ingesting the same corpus.
- **SentinelReplay / SentinelBench** — scores land on `MemoryRecord.investigation_score` and `sentinelbench_score` which are already read by causal reports.

## Isolation guarantees (tested)

- No import of `requests`, `httpx`, `urllib3`, `boto3`, `openai`, `anthropic`, `kubernetes`, or `supervisor.agent`.
- Deterministic sort_keys serialisation; identical input → byte-identical output.
- Additive — no runtime module registered, no feature flag introduced, no planner integration.

## Sample

See `docs/architecture/cross_incident_causal_graph_sample.json` — 3-record corpus (2 checkout saturations + 1 payments DNS incident).

## Files delivered

| Path | Purpose |
|------|---------|
| `sentinel_core/causal_graph/__init__.py` | Public facade |
| `sentinel_core/causal_graph/schemas.py` | Container types + id helpers |
| `sentinel_core/causal_graph/causal_node.py` | `CausalNode` + `CausalNodeType` (12) |
| `sentinel_core/causal_graph/causal_edge.py` | `CausalEdge` + `CausalEdgeType` (12) |
| `sentinel_core/causal_graph/graph_builder.py` | `CausalGraphBuilder` |
| `sentinel_core/causal_graph/chain_detector.py` | `ChainDetector` |
| `sentinel_core/causal_graph/recurrence.py` | `RecurrenceDetector` (5 dimensions) |
| `sentinel_core/causal_graph/rca_paths.py` | `RCAPathRanker` |
| `sentinel_core/causal_graph/mtti_paths.py` | `MTTIPathRanker` |
| `sentinel_core/causal_graph/recommendation_engine.py` | `CausalRecommendationEngine` (6 kinds) |
| `sentinel_core/causal_graph/report.py` | 7 renderers + master + `to_json` |
| `tests/causal_graph/test_causal_all.py` | 83 tests across 10 test classes |
| `docs/architecture/cross_incident_causal_graph.md` | This file |
| `docs/architecture/cross_incident_causal_graph_sample.json` | Reproducible sample |
