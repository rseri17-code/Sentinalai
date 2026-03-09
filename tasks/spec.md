# SPEC: Radon D/F Complexity Reduction

## 1. Objective
Reduce all D/F grade functions to C or better (CC <= 15) to pass the Radon exit gate.

## 2. Root Problem
Six functions exceed cyclomatic complexity threshold D (CC > 20):
- `_build_timeline` (E, CC=31) — agent.py
- `investigate` (D, CC=28) — agent.py
- `_stub_response` (D, CC=27) — mcp_client.py
- `_call_worker` (D, CC=25) — agent.py
- `get_investigation_workflow` (D, CC=22) — tool_selector.py
- `_fetch_itsm_context` (D, CC=21) — agent.py

## 3. Current Behavior
All functions work correctly. 1718 tests pass. No bugs. The issue is maintainability and readability, not correctness.

## 4. Proposed Solution
Extract sub-methods at natural boundaries. Three batches:

**Batch 1 (Cycle 5a):** Zero-risk extractions
- `_stub_response` → dispatch table lookup (mcp_client.py)
- `get_investigation_workflow` → extract phase-to-action mapping (tool_selector.py)

**Batch 2 (Cycle 5b):** Pure data transform
- `_build_timeline` → 5 source-specific extractors (agent.py)

**Batch 3 (Cycle 5c):** Core pipeline
- `_fetch_itsm_context` → `_budgeted_itsm_call` helper
- `_call_worker` → `_record_call_outcome` helper
- `investigate` → should drop below D after Batch 2-3 reduce its callees

## 5. Files Affected
- workers/mcp_client.py (Batch 1)
- supervisor/tool_selector.py (Batch 1)
- supervisor/agent.py (Batch 2, 3)

## 6. Risks
- **Determinism risk**: LOW for Batch 1-2 (no scoring path). MEDIUM for Batch 3 (_call_worker is in the hot path). Mitigation: run test_determinism.py before and after every change.
- **Regression risk**: All batches are pure extract-method refactors. No logic changes. Same inputs → same outputs.
- **Coverage risk**: Extracted methods inherit existing test coverage. No new untested paths created.

## 7. Acceptance Tests
Pre-refactor:
- All 1718 existing tests pass
- test_determinism.py all pass
- test_scoring_purity.py all pass

Post-refactor (each batch):
- All existing tests still pass (zero regressions)
- test_determinism.py all pass
- test_scoring_purity.py all pass
- `radon cc <file> -n D -s` shows zero D/F for the refactored functions
- Coverage >= 96.56% (no regression)

## 8. Verification Plan
V1: Run test_determinism.py + test_scoring_purity.py before and after
V2: Run module test suite
V3: Full suite with coverage
V4: Staff engineer gate
V5: `radon cc supervisor/ workers/ -n D -s` returns empty

## 9. Rollback Plan
Each batch is a single commit. `git revert <sha>` restores previous state. No schema changes, no dependency changes, no config changes.

## 10. Definition of Done
- `radon cc supervisor/ workers/ -n D -s` returns zero results
- All 1718+ tests pass
- Coverage >= 96.56%
- Mypy 0 errors
- Ruff 0 findings
- test_determinism.py + test_scoring_purity.py all pass
