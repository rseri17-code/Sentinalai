# RCA Code Patterns
<!-- Coding-assistant memory — NOT runtime production incident memory -->
<!-- Load when: implementing new hypothesis types, changing scoring logic, adding playbooks -->
<!-- Update when: a correct implementation pattern is confirmed by passing tests -->

## Purpose
Records confirmed code patterns for correctly implementing RCA features in this repo.
Specifically: how hypothesis scoring, playbook dispatch, and confidence calibration
are wired together, and what files must change together.

## Key Wiring (always keep current)

When adding a new **hypothesis type**:
- `supervisor/agent.py` — add to hypothesis list + scoring logic
- `supervisor/tool_selector.py` — may need classifier keyword update
- `tests/test_scoring_purity.py` — add scoring test
- `tests/test_determinism.py` — verify same input → same output

When adding a new **playbook**:
- `supervisor/tool_selector.py` — `_playbook_<type>()` method + classifier map
- `skills/<type>-investigation.md` — skill doc
- Verify `tests/test_determinism.py` still passes (tiebreak logic sensitive)

When changing **confidence scoring** (`compute_confidence`):
- `supervisor/agent.py` — the formula
- `tests/test_scoring_purity.py` — ALL purity tests must still pass
- `tests/test_determinism.py` — ALL determinism tests must still pass
- **Do not change** without human approval (it is a contract, not a heuristic)

## Schema for new entries

```
### Pattern N — [date]: [brief name]
- **Context**: what coding task triggered this
- **Pattern**: what the correct implementation looks like
- **What breaks if missed**: which test fails, how it manifests
- **Confirmed by**: test name that validates it
```

## Confirmed Patterns

### Pattern 1 — 2026-09-21: LLM overlay vs deterministic RCA core
- **Context**: Model-agnostic architecture audit. Needed a confirmed split so provider work does not retouch scoring/playbooks.
- **Pattern**: With `LLM_ENABLED=false` (CI, `tests/test_determinism.py` autouse), RCA is keyword classify → playbook MCP calls → `_analyze_*` + `compute_confidence` → gates. `supervisor/llm.converse()` is used only for refine/reasoning/classify-fallback/planner/judge/code-worker when enabled, and fail-open keeps pre-LLM hypotheses. Provider adapters must sit behind `InferencePort` / `converse()` without editing `supervisor/tool_selector.py` playbooks or `supervisor/helpers/confidence.py`.
- **What breaks if missed**: `tests/test_determinism.py`, `tests/test_scoring_purity.py`, INC12345 expected RCA (`tests/fixtures/expected_rca_outputs.py`), converse dict-shape tests (`tests/test_inference_contracts.py`).
- **Confirmed by**: `tests/test_determinism.py`, `tests/test_inference_contracts.py`, `supervisor/agent.py` `_analyze_evidence` LLM block, `.github/workflows/ci.yml` `LLM_ENABLED=false`
