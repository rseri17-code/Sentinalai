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

### Decision 5 — 2026-09-21: Slices 2–4 shipped; GATEWAY_MODE and Anthropic are runtime now
- **Task context**: Continue model-agnostic work after the audit + Slice 1 facade.
- **Decision**: Record that #78 Anthropic adapter, #79 GATEWAY_MODE + honest clone UX, and #80 MCP URL / YAML aliases are on `main`. Decision 3’s “unread env vars” description is historical.
- **Rejected alternative**: Rewriting the earlier ledger rows in place.
- **Why rejected**: Ledger is append-only session memory.
- **Reversible**: n/a (describes shipped work)
- **Session**: `main` after #80 (`46b9da8`)

### Decision 6 — 2026-09-21: OpenAI Chat Completions is the third InferencePort (Slice 5)
- **Task context**: Smallest third provider behind the existing `converse()` / `get_inference_port()` door. Do not change SRE investigation logic.
- **Decision**: Add `OpenAIInference` in `supervisor/llm.py` (same file as Bedrock/Anthropic). `LLM_PROVIDER=openai` selects it. `OPENAI_API_KEY` is read at call time and never logged. Map Chat Completions `prompt_tokens`/`completion_tokens` and `finish_reason` (`stop`→`end_turn`, `length`→`max_tokens`) onto the frozen converse dict. Map SDK errors to the Anthropic taxonomy (`rate_limited` / `timeout` / `unknown`), not `bedrock_error:*`. Factory warning lists `null, bedrock, anthropic, openai`. Default native id `gpt-4o` when `LLM_MODEL` is Bedrock/Claude-shaped. Keep `openai` optional in `requirements.txt` (import-guarded like boto3).
- **Rejected alternative**: New `supervisor/providers/` package; LiteLLM; native tool-use/streaming; uncommenting openai as a hard core dep; rewriting `investigate()`.
- **Why rejected**: Slice 2 already proved one adapter in `llm.py` is the smallest boundary. A new package and a required dep would expand clone install surface without helping tests (SDK is mocked / skip-if-missing).
- **Reversible**: yes (adapter is additive; default remains `LLM_ENABLED=false`)
- **Session**: `cursor/openai-inference-port-d2fb`
- **Evidence**: `supervisor/llm.py` `OpenAIInference`, `tests/test_llm.py` `TestOpenAIAdapter`, `tests/test_slice2_inference_providers.py` A–D

### Decision 7 — 2026-09-21: Owner chose Apache-2.0 for OSS plug-and-play
- **Task context**: Slice 0–5 left LICENSE to the owner. Owner approved adding Apache-2.0 on the Slice 5 branch.
- **Decision**: Root `LICENSE` is the standard Apache License Version 2.0 text. Copyright 2026 the SentinalAI authors (`pyproject.toml` has no authors field). `pyproject.toml` `license = {text = "Apache-2.0"}`. README and model-agnostic audit docs state Apache-2.0 instead of Proprietary / owner-must-choose.
- **Rejected alternative**: MIT, proprietary-only, or inventing a copyright holder beyond the fallback the owner specified.
- **Why rejected**: Owner named Apache-2.0. No existing copyright notice in-repo; fallback matches the instruction.
- **Reversible**: yes (license file + pyproject + docs)
- **Session**: `cursor/openai-inference-port-d2fb`
- **Evidence**: `LICENSE`, `pyproject.toml`, `README.md` License section

_Update this file during session. Promote significant entries to tasks/decisions.md._
