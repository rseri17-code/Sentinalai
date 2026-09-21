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

### Decision 1 — 2026-09-21: converse() is an InferencePort facade
- **Task context**: Slice 1 of the model-agnostic audit — make converse() resolve a port from env without SRE-domain edits.
- **Decision**: Keep Bedrock Converse inside `BedrockInference` in `supervisor/llm.py`. `converse()` always calls `get_inference_port()`. `LLM_ENABLED` defaults false. `LLM_PROVIDER=null|none|disabled` → NullInference; `bedrock` (default when enabled) → Bedrock. Unknown providers (anthropic/openai) → NullInference + warning until Slice 2.
- **Rejected alternative**: New `supervisor/providers/` package, LiteLLM, or changing agent.py to take a port.
- **Why rejected**: Smallest boundary is the existing converse() dict; tests patch `_get_client` / `LLM_ENABLED` on llm.py. A new package can wait for Slice 2.
- **Reversible**: yes
- **Session**: `cursor/llm-inference-port-facade-7096`
