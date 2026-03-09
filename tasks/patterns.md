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
