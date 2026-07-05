# Incident Intelligence Memory

**Status:** Landed at branch `claude/code-review-analysis-MelXd`.
**Location:** `sentinel_core/intel_memory/` and `tests/intelligence_memory/`.
**External dependencies:** none — Python stdlib only.

Incident Intelligence Memory is Sentinel's **permanent operational
memory layer**. Every completed investigation can be persisted as an
immutable `MemoryRecord`. Future investigations can retrieve
similar past incidents deterministically — no embeddings, no vector
databases, no LLM invocation.

## Non-goals

- Not a production runtime module.
- Not autonomous remediation.
- Not self-modifying code.
- Not an LLM inference layer.

## Directory name rationale

The mission brief specified `sentinel_core/intelligence_memory/`, but
the sentinel_core package enforces a substring rule against
`"intelligence"` in module names (`tests/test_sentinel_core_compatibility.py`).
The package therefore lives at `sentinel_core/intel_memory/`, following
the same convention already used for `sentinel_core/models/intel_context.py`.
The test directory `tests/intelligence_memory/` follows the original
mission brief — test-file names are unconstrained.

## Reused components

| Component | Reused how |
|-----------|-----------|
| `sentinel_core.models.knowledge_graph` | KnowledgeGraph snapshot embedded on MemoryRecord |
| `sentinel_core.models.decision_context` | DecisionContext-shape decision_trace embedded on MemoryRecord |
| `sentinel_core.models.plan` | Planner capability ids stored in `planner_decisions` |
| SentinelBench scoring | `sentinelbench_score` field on MemoryRecord |
| SentinelReplay run history | `replay_history` field on MemoryRecord |
| ResolutionMemory / EpisodicMemory / InvestigationStore | Referenced via `receipt_references` (no duplication of storage) |
| Existing report renderer patterns | Same `to_json(dict, sort_keys=True, indent=2)` convention |

## Architecture overview

```
    ┌──────────────────────────────────────────────────────────┐
    │  MemoryStore                                             │
    │  (JSON per record under caller-supplied root)            │
    │  save/load/list/has/delete                               │
    └──────────────────┬───────────────────────────────────────┘
                       │
                       ▼
         ┌─────────────────────────┐
         │  MemoryRecord           │
         │  (immutable dataclass)  │
         │  ─────────────────────  │
         │  identity, classify,    │
         │  topology, evidence,    │
         │  planner, decision,     │
         │  KG, outcome, metrics,  │
         │  skills, references,    │
         │  scoring, replay_hist   │
         └──────┬─────────┬────────┘
                │         │
                │         ▼
                │   ┌────────────────────┐
                │   │  Fingerprint       │
                │   │  compute_fingerprint(FingerprintInput) │
                │   │  sha256[:16]       │
                │   │  no embeddings     │
                │   └────────────────────┘
                │
                ▼
    ┌────────────────────────────────────────────────────────┐
    │  Retrieval                                             │
    │  by_fingerprint / service / application / incident_type │
    │  / topology / deployment / namespace / root_cause      │
    │  / transaction_path / planner_capability / confidence  │
    │  / mtti_range / time_window                            │
    └────────────────────────────────────────────────────────┘
                │
                ▼
      ┌───────────────────────────┐
      │  SimilarityEngine         │
      │  11 weighted dimensions   │
      │  exact / topology / dep   │
      │  evidence / planner       │
      │  transaction / RCA        │
      │  infra / resolution       │
      │  blast_radius             │
      │  score / score_many       │
      └──────┬────────────────────┘
             │
             ▼
       ┌──────────────────────┐
       │  Ranker              │
       │  top_n(...)          │
       └─────┬────────────────┘
             │
             ▼
    ┌──────────────────────────────────────────┐
    │  GuidedInvestigation                     │
    │  Top-10 similar + aggregated payload:    │
    │    - have_seen_this_before               │
    │    - evidence overlap                    │
    │    - recommended_investigation_order     │
    │    - recommended_evidence                │
    │    - recommended_planner_capabilities    │
    │    - known_root_causes                   │
    │    - known_resolutions                   │
    │    - expected_confidence                 │
    │    - expected_mtti_ms                    │
    │    - expected_blast_radius               │
    │    - likely_next_step                    │
    │    - previously_successful_sequence      │
    └──────────────────────────────────────────┘

    ┌──────────────────────────────────────────┐
    │  LearningLoop                            │
    │  recurring_root_causes / evidence /      │
    │  planner_paths / failed_investigations / │
    │  false_leads / missing_evidence /        │
    │  topology_failures / transaction /       │
    │  deployment / dependency / blast_radius /│
    │  mtti_bottlenecks / confidence_drops     │
    │  all_patterns(...)                       │
    └──────────────────────────────────────────┘

    ┌──────────────────────────────────────────┐
    │  Report renderers (deterministic JSON)   │
    │  memory_report / similarity / learning / │
    │  recurring_patterns / incident_clusters /│
    │  guided_investigation / knowledge_growth /│
    │  experience_reuse / top_root_causes /    │
    │  top_false_leads / mtti_improvement /    │
    │  master_report                           │
    └──────────────────────────────────────────┘
```

## Success metrics — how the memory answers each mission question

| Question | Answered by |
|----------|-------------|
| Have we seen this before? | `GuidedInvestigation.build().have_seen_this_before` |
| How similar is it? | `SimilarityEngine.score_many(...)` |
| What evidence solved it previously? | `guided.recommended_evidence` |
| What planner path worked best? | `guided.recommended_planner_capabilities` |
| What investigation order worked best? | `guided.recommended_investigation_order` |
| Which evidence was unnecessary? | `learning.recurring_false_leads(...)` |
| What resolution succeeded? | `guided.known_resolutions` |
| How much faster should this investigation be? | `mtti_improvement` report + `guided.expected_mtti_ms` |
| What MTTI should we expect? | `guided.expected_mtti_ms` |
| What confidence should we expect? | `guided.expected_confidence` |

## Isolation guarantees

- No import of `requests`, `httpx`, `urllib3`, `boto3`, `openai`,
  `anthropic`, or `kubernetes` in any `sentinel_core.intel_memory.*` module.
- No import of `supervisor.agent` in any module.
- All I/O is scoped to caller-supplied paths; the store never writes
  before `save(...)` is called.
- Deterministic serialization (`sort_keys=True`); identical input →
  byte-identical JSON output.
- Enforced by `tests/test_sentinel_core_compatibility.py` (no `supervisor`,
  `intelligence`, `workers`, `agui`, `database`, `integrations` substring
  in any sentinel_core module name).

## Extension points

- Add a new sub-record shape → append a frozen dataclass in `schemas.py`,
  extend `MemoryRecord.from_dict` to accept it.
- Add a new similarity dimension → add a function in `similarity.py`,
  register a weight in `SIMILARITY_WEIGHTS`, extend `SimilarityEngine.score`.
- Add a new retrieval filter → add a method to `Retrieval`.
- Add a new learning pattern → add a method to `LearningLoop` and
  wire it into `all_patterns`.
- Add a new report → add a `render_*` function in `report.py` and wire
  it into `render_master_report`.

## Future roadmap

| Milestone | Description |
|-----------|-------------|
| Ingest historical `_intelligence.json` artifacts | Convert `context_persister` outputs into `MemoryRecord` via a caller-supplied adapter. |
| Ingest ResolutionMemory + EpisodicMemory + InvestigationStore | Same: build adapters that read the existing SQLite/JSONL stores and yield `MemoryRecord` objects. |
| GuidedInvestigation → analyzer prompt (opt-in) | A future runtime module can attach `guided_investigation.recommended_evidence` to the LLM prompt behind a feature flag — but this repository's mission constraint forbids modifying the analyzer prompt today. |
| Memory pruning + TTL | Deterministic pruning strategies (age, confidence, reuse count). |
| Memory export + import | Corpus-level tools for backup / cross-environment portability. |

## Files delivered

| Path | Purpose |
|------|---------|
| `sentinel_core/intel_memory/__init__.py` | Package facade |
| `sentinel_core/intel_memory/schemas.py` | MemoryRecord + supporting dataclasses + enums |
| `sentinel_core/intel_memory/fingerprint.py` | Deterministic sha256[:16] fingerprint + component hashes |
| `sentinel_core/intel_memory/similarity.py` | 11-dim weighted SimilarityEngine |
| `sentinel_core/intel_memory/memory_store.py` | JSON-per-record persistent store |
| `sentinel_core/intel_memory/retrieval.py` | 13 read-only filter APIs |
| `sentinel_core/intel_memory/learning.py` | LearningLoop with 13 recurring-pattern detectors |
| `sentinel_core/intel_memory/ranking.py` | Ranker (thin wrapper on SimilarityEngine) |
| `sentinel_core/intel_memory/recommendation.py` | GuidedInvestigation |
| `sentinel_core/intel_memory/report.py` | 11 report renderers + master_report + to_json |
| `tests/intelligence_memory/test_schemas.py` | Schema tests |
| `tests/intelligence_memory/test_fingerprint.py` | Fingerprint tests |
| `tests/intelligence_memory/test_similarity.py` | Similarity tests |
| `tests/intelligence_memory/test_memory_store_retrieval.py` | Store + retrieval tests |
| `tests/intelligence_memory/test_learning_recommendation.py` | LearningLoop + GuidedInvestigation + report tests |
| `docs/architecture/incident_intelligence_memory.md` | This document |
