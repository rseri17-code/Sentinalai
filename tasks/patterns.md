# SentinalAI — Known Failure Patterns

## Pattern 1: Ruff noqa without error code
Signature: `ruff check` reports invalid `# noqa` comment without specific code
Root cause: Bare `# noqa` used instead of `# noqa: <CODE>`
Fix: Replace with `# noqa: <SPECIFIC_CODE>` for each suppression
Test: `ruff check .` returns 0 findings
Seen: 2026-03-08 Cycle 1

## Pattern 2: Missing return type annotation
Signature: `mypy` reports missing return type on function
Root cause: Function missing `-> <type>` annotation
Fix: Add explicit return type annotation
Test: `mypy supervisor workers knowledge --ignore-missing-imports` returns 0 errors
Seen: 2026-03-08 Cycle 2

## Pattern 3: Mypy not installed in CI
Signature: `mypy: command not found` in CI logs
Root cause: mypy not in dev dependencies
Fix: Add mypy to `[project.optional-dependencies] dev` in pyproject.toml
Test: CI typecheck job passes
Seen: 2026-03-08 Cycle 2

## Pattern 4: Deterministic path coverage gap
Signature: Coverage report shows < 100% on tool_selector.py, guardrails.py, or scoring functions
Root cause: Edge paths (ImportError fallbacks, phase filtering branches, circuit breaker recovery callbacks, helper method short-circuits) not exercised by tests
Fix: Write targeted tests for each uncovered line — mock imports for ImportError paths, create YAML catalogs with phase config for filtering paths, call record_success with worker_name after circuit open for metric callbacks
Test: `pytest --cov=supervisor/tool_selector --cov=supervisor/guardrails --cov-report=term-missing` shows 100%
Seen: 2026-03-09

## Pattern 5: Test failures after installing boto3
Signature: Tests in test_analyzer_branches.py fail with `TypeError: Object of type MagicMock is not JSON serializable` after installing boto3
Root cause: Test fixture `_make_supervisor_with_data` replaces all workers with MagicMock but only sets `.execute` on 5 of 9 workers. With boto3 installed, LLM/ITSM/DevOps code paths execute further, and unmocked workers return MagicMock values that fail JSON serialization in span_end logging.
Fix: Set `.execute = Mock(side_effect=mock_noop)` on ALL workers before overriding specific ones.
Test: `pytest tests/test_analyzer_branches.py` — 25 passed
Seen: 2026-03-09
