# SentinalAI — Architectural Decisions

## Decision 1: Radon D/E complexity — informational only
Date: 2026-03-08
Context: 5-6 functions in agent.py and mcp_client.py have D/E complexity grades.
Decision: These are informational findings, not blocking. They require
architectural review before refactoring due to the size and criticality of
these files (1955+ and 1005+ lines respectively).
Tradeoff: Accepting higher complexity in exchange for not introducing
regressions in critical pipeline code.

## Decision 2: OTEL init code coverage exclusion
Date: 2026-03-08
Context: Lines 34-71 in observability.py are module-level init code that
runs at import time, making them difficult to test in isolation.
Decision: Accept current 81% coverage for observability.py. Module-level
init is covered by integration-level usage.
