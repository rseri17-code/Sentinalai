"""Shared pytest configuration and fixtures for SentinalAI tests.

Purpose
-------
Tests must be hermetic: they must not read from or write to the shipped
``eval/`` learning-state files (experience_store, knowledge_graph, calibration
map, adaptive thresholds, evolved strategy weights, gap-pattern aggregator,
incident-to-commit index).

If tests used the shipped state, priming from past investigations would leak
into unit tests and cause non-deterministic failures (e.g. hypothesis priming
from ``experience_store.json`` injecting historical root-cause strings that
don't belong to the test's mock evidence).

Tests *also* must not leak state to each other — one test's "investigation"
writes to the experience store via a background executor, and subsequent
tests would see the record and prime new hypotheses from it.

What this does
--------------
1. Before any test module is imported, set every persistence-path
   environment variable to a fresh per-session temp directory.  The modules
   read these at import time, so this has to run as early as possible —
   hence the top-level code rather than a fixture.
2. Around every test, delete any state file that may have been created by a
   previous test in the same session.  This keeps tests independent of
   ordering without having to mock every persistence boundary.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types

import pytest


# ---------------------------------------------------------------------------
# Optional-dependency stubs for a minimal test environment.
#
# The CI image has `sqlalchemy` and `requests` installed via
# ``pip install -e ".[dev]"``.  When running locally without dev extras
# these modules are missing.  The unit tests in test_persistence_coverage.py
# patch `database.persistence.get_engine` to return a mock, but the
# persistence module still does ``from sqlalchemy import text`` at call
# time, which raises ModuleNotFoundError and defeats the mock.
#
# Install a tiny stub exposing only the ``text`` helper if real
# SQLAlchemy is absent — real CI still gets the real library.
# ---------------------------------------------------------------------------

if "sqlalchemy" not in sys.modules:
    try:
        import sqlalchemy  # noqa: F401
    except Exception:  # pragma: no cover — exercised only when dep missing
        _sa_stub = types.ModuleType("sqlalchemy")

        def _text(sql: str):  # pragma: no cover — trivial identity
            return sql

        _sa_stub.text = _text
        sys.modules["sqlalchemy"] = _sa_stub

# Create a per-session temp dir for all SentinalAI learning-state files.
_TMP_STATE_DIR = tempfile.mkdtemp(prefix="sentinalai-test-state-")

# Every env var that a supervisor/knowledge module reads at import time.
# Keep in sync with module-level `os.environ.get(...)` calls.
_STATE_PATHS = {
    "EXPERIENCE_STORE_PATH":    os.path.join(_TMP_STATE_DIR, "experience_store.json"),
    "KNOWLEDGE_GRAPH_PATH":     os.path.join(_TMP_STATE_DIR, "knowledge_graph.json"),
    "CALIBRATION_MAP_PATH":     os.path.join(_TMP_STATE_DIR, "calibration_map.json"),
    "ADAPTIVE_THRESHOLDS_PATH": os.path.join(_TMP_STATE_DIR, "adaptive_thresholds.json"),
    "EVOLVED_STRATEGY_PATH":    os.path.join(_TMP_STATE_DIR, "evolved_strategy.json"),
    "GAP_AGGREGATOR_PATH":      os.path.join(_TMP_STATE_DIR, "gap_patterns.json"),
    "INCIDENT_GIT_INDEX_PATH":  os.path.join(_TMP_STATE_DIR, "incident_git_index.json"),
}

for _k, _v in _STATE_PATHS.items():
    # Only set if the test caller has not already overridden — respects
    # individual tests that want to point at a specific fixture file.
    os.environ.setdefault(_k, _v)


@pytest.fixture(autouse=True)
def _reset_learning_state(request):
    """Remove any persisted learning-state file created by a prior test.

    Running before every test, this guarantees that background-executor
    writes from test N are not visible to test N+1.  Tests that explicitly
    seed state still work because they write after this fixture runs.
    """
    # Delete state files from prior tests
    for path in _STATE_PATHS.values():
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            # Don't fail the test if the file happens to be a directory etc.
            pass
    yield
