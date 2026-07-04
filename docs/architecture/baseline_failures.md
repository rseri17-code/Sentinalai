# Baseline Test-Suite Failures

**Status:** Authoritative baseline as of Phase 23.
**Head commit at capture:** `f853e90`
**Suite:** `pytest` (v9.0.2), 4547 collected, 4518 passed, 27 failed, 2 skipped.
**Runtime:** ~7m 34s.

This document is the single source of truth for what is *pre-existing* vs
*regressed* in the SentinelAI test suite. Every future phase compares its
"after" numbers to the counts here. A run that reports "27 failed" with
the tests listed below is byte-identical to this baseline.

---

## Root Cause (single)

All 27 failures share **one** root cause: the pytest tool environment at
`/root/.local/share/uv/tools/pytest/lib/python3.11/site-packages/` does
not have `pytest-asyncio` installed. Every failing test is an `async def`
decorated with `@pytest.mark.asyncio`, and pytest emits:

```
async def functions are not natively supported.
You need to install a suitable plugin for your async framework, for example:
  - anyio
  - pytest-asyncio
  - pytest-tornasync
  - pytest-trio
  - pytest-twisted
```

Corroborating evidence:

- `pyproject.toml` line 37: `"pytest-asyncio>=0.23.0"` declared under
  `[project.optional-dependencies].dev`.
- `pyproject.toml` line 51: `asyncio_mode = "auto"` declared under
  `[tool.pytest.ini_options]` — this option is *unknown to base pytest*
  and the suite warns `PytestConfigWarning: Unknown config option: asyncio_mode`,
  which only appears when the plugin is absent.
- `grep -c "pytest.mark.asyncio" tests/**/*.py` returns exactly 27
  occurrences across exactly the 4 failing files (see below).
- Installing `pytest-asyncio` into the tool env resolves all 27 with no
  source-code change.

There is **no product bug** and **no code-quality issue** in any of the
27 tests or the code they exercise. The failures are an artefact of the
tool-env provisioning; the tests are correct.

---

## Failure Enumeration

### `tests/agui/test_event_bus.py::TestInProcessEventBus` (6)

| # | Test |
|---|------|
| 1 | `test_publish_and_subscribe` |
| 2 | `test_deduplication` |
| 3 | `test_multiple_investigations_isolated` |
| 4 | `test_history_available_for_late_subscribers` |
| 5 | `test_unsubscribe_stops_delivery` |
| 6 | `test_wildcard_subscriber` |

Subject under test: in-process AG-UI event bus. Every test is async.

### `tests/agui/test_replay_engine.py::TestReplayExecution` (4)

| # | Test |
|---|------|
| 1 | `test_replay_delivers_all_events` |
| 2 | `test_replay_validates_before_starting` |
| 3 | `test_pause_resume` |
| 4 | `test_abort_stops_replay` |

Subject under test: AG-UI replay engine execution loop. Every test is async.

### `tests/agui/test_synthetic_generator.py::TestSyntheticGenerator` (12)

| #  | Test |
|----|------|
| 1  | `test_generate_error_spike` |
| 2  | `test_all_events_are_valid_schema` |
| 3  | `test_sequences_are_ordered` |
| 4  | `test_required_event_types_present` |
| 5  | `test_includes_llm_events` |
| 6  | `test_includes_memory_events` |
| 7  | `test_circuit_breaker_variant` |
| 8  | `test_control_gate_variant` |
| 9  | `test_budget_warning_variant` |
| 10 | `test_all_scenarios_generate_valid_events` |
| 11 | `test_generated_events_pass_replay_validation` |
| 12 | `test_deterministic_with_seed` |

Subject under test: synthetic AG-UI event generator. Every test is async.

### `tests/test_webhook_auth.py::TestCheckSig` (5)

| # | Test |
|---|------|
| 1 | `test_require_auth_no_secret_raises_401` |
| 2 | `test_require_auth_valid_signature_passes` |
| 3 | `test_require_auth_bad_signature_raises_401` |
| 4 | `test_require_auth_missing_header_raises_401` |
| 5 | `test_no_require_auth_no_secret_skips` |

Subject under test: FastAPI webhook signature-check dependency. Every
test is async (uses `TestClient` + `await` for FastAPI's async handlers).

**Total: 27.**

---

## Classification Summary

| Cluster | Count | Root cause | Code defect? | Blocking? |
|---------|-------|------------|--------------|-----------|
| `test_event_bus.py` | 6 | Missing pytest-asyncio | No | No |
| `test_replay_engine.py` | 4 | Missing pytest-asyncio | No | No |
| `test_synthetic_generator.py` | 12 | Missing pytest-asyncio | No | No |
| `test_webhook_auth.py` | 5 | Missing pytest-asyncio | No | No |

All 27 → single environmental cause → **not blocking**. Future phases
may treat these as "known baseline" without re-triaging.

---

## Fix Path (Deferred — Not Phase 23)

The fix is an environment change, not a source change:

```bash
uv tool install --force pytest --with pytest-asyncio --with pytest-timeout
# or, if a project venv exists:
pip install -e '.[dev]'
```

Phase 23 does **not** apply this fix because:

1. The pytest tool env at `/root/.local/share/uv/tools/pytest/` is
   outside the repo; changes there are not reproducible from source and
   do not persist across container recreation.
2. The proper fix is either (a) provisioning tooling for the ephemeral
   remote-execution container, or (b) landing a project venv setup step
   — both are out of scope for a single bounded phase and touch shared
   infrastructure.

Phase 23 delivers only the classification. A follow-up (Phase 24 or a
dedicated env-fix phase) may action the fix path once the delivery
mechanism is agreed.

---

## Regression Comparison Protocol

For every subsequent phase, the regression check is:

```
Full-suite result must be: 4518 passed, 27 failed, 2 skipped
Failing tests must be identical to the enumeration above.
```

Any deviation — a new failure not on this list, or one of these 27
starting to pass without an explicit env-fix — must be investigated
before the phase ships.
