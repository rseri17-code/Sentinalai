# Implementation Decision Ledger
<!-- Coding-assistant memory — decisions made during coding sessions, not runtime operational decisions -->
<!-- Load when: about to make a significant implementation choice, or repeating a past task -->
<!-- Update when: a non-obvious implementation decision is made and the reasoning should be preserved -->

## Purpose
Records why implementation choices were made during coding sessions on this repo.
Distinct from tasks/decisions.md (architectural/tooling) — this captures
session-level reasoning: why this approach over that one, what was rejected.

## Schema

```
### Decision N — [date]: [brief name]
- **Task context**: what were we implementing
- **Decision**: what was chosen
- **Rejected alternative**: what else was considered
- **Why rejected**: concrete reason (test failure, complexity, design conflict)
- **Reversible**: yes/no
- **Session**: branch or commit where this was decided
```

## Decisions

### Decision 1 — 2026-09-21: Model-agnostic boundary is InferencePort, not a new framework
- **Task context**: Audit how to make Sentinal a production-ready, open-source, model-agnostic SRE investigation agent without implementing the abstraction yet.
- **Decision**: Keep `sentinel_core.models.inference.InferencePort` / `supervisor.llm.converse()` dict shape as the only SRE-facing LLM API. Future providers (Bedrock already, then Anthropic or OpenAI) are adapters behind that port. Do not add LiteLLM, LangChain, or a parallel client.
- **Rejected alternative**: Rewriting `investigate()` or prompts per provider; native LLM tool-calling for playbook steps.
- **Why rejected**: Playbooks already call MCP workers; planner already asks for JSON `{worker, action, params}`. SRE analyzers are deterministic and CI-proven with `LLM_ENABLED=false`. A new framework would touch SRE-domain code and break `tests/test_inference_contracts.py` / `tests/test_determinism.py`.
- **Reversible**: yes (adapters are additive)
- **Session**: `cursor/model-agnostic-sre-audit-7096`
- **Evidence**: `supervisor/llm.py`, `sentinel_core/models.inference.py`, `supervisor/planner.py`, `.github/workflows/ci.yml`

### Decision 2 — 2026-09-21: LLM is an overlay; clone-and-run path is stubs + LLM off
- **Task context**: Distinguish true architectural dependencies from config.
- **Decision**: Treat keyword classification, `INCIDENT_PLAYBOOKS`, `_analyze_*`, `compute_confidence`, evidence gates, and `McpGateway` stubs as the portable SRE engine. Bedrock Converse, AgentCore Memory, and AgentCore gateway naming are optional overlays / AWS fabric.
- **Rejected alternative**: Making a cloud LLM mandatory for RCA.
- **Why rejected**: CI already sets `LLM_ENABLED=false`; `is_enabled()` fail-open keeps pre-LLM hypotheses. Clone-and-run works today without AWS if stubs are used.
- **Reversible**: n/a (describes current architecture)
- **Session**: `cursor/model-agnostic-sre-audit-7096`
- **Evidence**: `tests/test_determinism.py`, `workers/mcp_client.py:713-715`, `supervisor/agent.py:2347-2371`

### Decision 3 — 2026-09-21: GATEWAY_MODE and LLM_PROVIDER docs are drift, not runtime
- **Task context**: Map clone/configure/connect friction.
- **Decision**: Document that `GATEWAY_MODE` is unread in Python (live MCP = `AGENTCORE_GATEWAY_URL` / ARNs) and `LLM_PROVIDER` is unread by `supervisor.llm` (health check only). Fixing that is slice 0–3 (honesty + config), not SRE logic.
- **Rejected alternative**: Treating README/certification `GATEWAY_MODE=stub` as the implemented switch.
- **Why rejected**: `rg GATEWAY_MODE --glob '*.py'` is empty.
- **Reversible**: yes (implement the env var later or delete it from docs)
- **Session**: `cursor/model-agnostic-sre-audit-7096`

### Decision 4 — 2026-09-21: converse() is an InferencePort facade
- **Task context**: Slice 1 of the model-agnostic audit — make converse() resolve a port from env without SRE-domain edits.
- **Decision**: Keep Bedrock Converse inside `BedrockInference` in `supervisor/llm.py`. `converse()` always calls `get_inference_port()`. `LLM_ENABLED` defaults false. `LLM_PROVIDER=null|none|disabled` → NullInference; `bedrock` (default when enabled) → Bedrock. Unknown providers (anthropic/openai) → NullInference + warning until Slice 2.
- **Rejected alternative**: New `supervisor/providers/` package, LiteLLM, or changing agent.py to take a port.
- **Why rejected**: Smallest boundary is the existing converse() dict; tests patch `_get_client` / `LLM_ENABLED` on llm.py. A new package can wait for Slice 2.
- **Reversible**: yes
- **Session**: `cursor/llm-inference-port-facade-7096`

_Update this file during session. Promote significant entries to tasks/decisions.md._
