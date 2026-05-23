# Codebase Topology
<!-- Coding-assistant memory — NOT runtime service topology -->
<!-- Load when: changing interfaces between modules, adding new workers, refactoring pipeline stages -->
<!-- Update when: a module dependency is confirmed by tracing an import or test failure -->

## Purpose
Records confirmed module-level dependency relationships in this codebase.
Used so the coding assistant knows what else must change when modifying a file.
This is import/call topology, not infrastructure topology.

## Critical Dependencies (always current)

```
supervisor/agent.py
  └── imports all workers (ops, log, metrics, apm, knowledge, itsm, devops)
  └── imports supervisor/tool_selector.py  (classifier + playbooks)
  └── imports supervisor/guardrails.py     (budget + circuit breaker)
  └── imports supervisor/llm.py            (non-blocking refinement)
  └── imports supervisor/memory.py         (AgentCore STM/LTM)
  └── RULE: changes here require SPEC MODE (>1000 lines)

supervisor/tool_selector.py
  └── imports workers (for type hints only — no runtime dispatch)
  └── RULE: changes require test_determinism.py before AND after

workers/mcp_client.py
  └── central AgentCore gateway — all workers route through it
  └── RULE: changes require SPEC MODE (>1000 lines)
  └── RULE: boto3.Session() must be per-investigation, never cached

knowledge/ ← standalone
  └── retrieval_engine, graph_store, graph_backend_json, metadata_filter
  └── imported by knowledge_worker.py only
```

## Schema for new entries

```
### Dependency N — [date]: [module A] → [module B]
- **Nature**: import / runtime call / test dependency
- **Why it matters**: what breaks if this dependency is ignored
- **Confirmed by**: how this was discovered (test failure, grep, etc.)
```

## Confirmed Dependencies

_None added yet beyond the critical deps above._
_Promote from stop_log.md using [PROMOTE: topology_substrate]._
