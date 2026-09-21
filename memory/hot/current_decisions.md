# Current Session Decisions
<!-- Written by agent during session, harvested by SessionEnd hook -->
<!-- Cleared after SessionEnd harvests it into stop_log.md -->
<!-- Format: bullet list. Add [PROMOTE: target] to flag for promotion. -->

- [DECISION] [PROMOTE: operational_decision_ledger] Audit-only PR; no provider abstraction implementation — user scoped OBSERVE→LEARN stop after docs.
- [DECISION] [PROMOTE: operational_decision_ledger] Place the audit under `docs/architecture/` (existing architecture docs) rather than a new framework or root-level parallel spec. Companion coupling table is `docs/architecture/model_coupling_table.md`.
- [DECISION] [PROMOTE: operational_decision_ledger] `InferencePort` + `converse()` dict shape is the smallest model-agnostic boundary; do not introduce LiteLLM/LangChain. MCP tools stay MCP (planner JSON), not native LLM tool-calling, in slice 1–2.
- [DECISION] [PROMOTE: rca_patterns] Default CI/investigation path is LLM-optional (`LLM_ENABLED=false` in CI). Deterministic analyzers + playbooks are the SRE core; Bedrock `converse()` is an overlay. Do not make LLM mandatory.
- [DECISION] Document vs source: `GATEWAY_MODE` and `.env.example` LLM_PROVIDER are unused by Python. Live MCP switch is `AGENTCORE_GATEWAY_URL`; live LLM is Bedrock only (`supervisor/llm.py`).
- [DECISION] Dev-loop `anthropic.Anthropic().messages.create` in review_responder/ci_shepherd/dev_loop_agent is out of scope for SRE model-agnosticism.
- [PATTERN] [PROMOTE: rca_patterns] When adding LLM providers, keep `tests/test_determinism.py`, converse() dict-shape tests, INC12345 expected RCA, and MCP stub path unchanged.

## Format

```
- [DECISION] <what was decided> — <why>
- [DECISION] [PROMOTE: operational_decision_ledger] <important decision> — <reason>
- [BLOCKER] <what is blocking> — <next step>
- [WORKAROUND] [PROMOTE: known_workarounds] <workaround used> — <context>
- [PATTERN] [PROMOTE: rca_patterns] <pattern observed> — <evidence>
```
