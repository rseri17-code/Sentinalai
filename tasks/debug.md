# SentinalAI — Active Debug Notes

## ISSUE: Deterministic path coverage < 100%
SKILL: coverage_expansion
OBSERVED: Exit gate requires 100% line coverage on deterministic path files.
Current gaps:
  - tool_selector.py: 96% (lines 392-394: PyYAML ImportError; 451-452: phase filter with results; 486: phase_config branch)
  - guardrails.py: 96% (line 98: circuit breaker half_open_to_closed; lines 120-121: registry reset)
  - agent.py compute_confidence(): 100% ✓ (fully covered)
  - agent.py tiebreak (line 1048): covered ✓
  - agent.py lines 2111, 2115: _find_backend_event empty return, _is_gradual_increase short list
LOCATION: supervisor/tool_selector.py, supervisor/guardrails.py, supervisor/agent.py
ORIGIN: Tests don't exercise edge cases in these files

## Hypotheses
A: Missing tests for edge paths in tool_selector.py (PyYAML not installed, phase filtering with matches, phase budget from config) — HIGH confidence
B: Missing tests for circuit breaker recovery path and registry reset in guardrails.py — HIGH confidence
C: Missing tests for agent.py helper methods (_find_backend_event no match, _is_gradual_increase too few points) — HIGH confidence

All three are straightforward coverage gaps — no complex root cause. Confidence HIGH across all.
