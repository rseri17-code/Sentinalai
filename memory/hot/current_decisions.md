# Current Session Decisions
<!-- Written by agent during session, harvested by SessionEnd hook -->
<!-- Cleared after SessionEnd harvests it into stop_log.md -->
<!-- Format: bullet list. Add [PROMOTE: target] to flag for promotion. -->

- [DECISION] [PROMOTE: operational_decision_ledger] Slice 1: converse() facade over InferencePort; Bedrock stays in llm.py; LLM_ENABLED default false; unknown providers NullInference until Slice 2.
- [DECISION] [PROMOTE: operational_decision_ledger] Slice 2: AnthropicInference behind converse()/get_inference_port(); LLM_PROVIDER=anthropic; credentials at call-time; set_inference_port for tests A–D. No SRE-domain edits.

## Format

```
- [DECISION] <what was decided> — <why>
- [DECISION] [PROMOTE: operational_decision_ledger] <important decision> — <reason>
- [BLOCKER] <what is blocking> — <next step>
- [WORKAROUND] [PROMOTE: known_workarounds] <workaround used> — <context>
- [PATTERN] [PROMOTE: rca_patterns] <pattern observed> — <evidence>
```
