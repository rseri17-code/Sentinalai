"""PatternRecognition activation tests.

Verifies the second READ consumer of the persisted intelligence corpus:
``pattern_recognition`` runs at POST_CLASSIFY and consults the
operational_patterns table for recurring shapes. Read-only.

Coverage:
- ModuleSpec metadata (stage, feature flag, priority)
- Skip when neither service nor incident_type is available
- Success with empty matches when store is empty
- Success with matches when the store has recurring patterns
- Only patterns with occurrence_count >= _MIN_OCCURRENCES are surfaced
- Matches bounded to _MAX_MATCHES
- Feature flag on/off + master flag semantics
- Receipt metadata lift through the runtime
- Failure isolation (query raises → empty list, no crash)
- Read-only guarantee (no rows added)
- Ordering (runs after historical_lookup at same stage)
- JSON safety
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from sentinel_core.runtime import (
    IntelligenceRuntime,
    IntelligenceStage,
    RuntimeContext,
)
from supervisor.intelligence_modules import install_default_modules
from supervisor.intelligence_modules.pattern_recognition import (
    PATTERN_RECOGNITION_FEATURE_FLAG,
    PATTERN_RECOGNITION_SPEC,
    RECOGNITION_VERSION,
    _MAX_MATCHES,
    _MIN_OCCURRENCES,
    pattern_recognition_runner,
)


# ---------------------------------------------------------------------------
# Fakes + fixtures
# ---------------------------------------------------------------------------

@dataclass
class _FakeCres:
    incident_type: str = "saturation"


def _ctx(*, service="checkout", incident_type="saturation",
         incident_id="INC1", investigation_id="inv-INC1"):
    return RuntimeContext(
        investigation_id=investigation_id,
        stage=IntelligenceStage.POST_CLASSIFY,
        fetch_out={
            "incident": {"incident_id": incident_id, "summary": "x",
                          "affected_service": service},
            "service":  service,
        },
        cres=_FakeCres(incident_type=incident_type),
    )


# Minimal DDL — mirrors database/ops_persistence.py schema for
# operational_patterns. We create it directly so tests never touch the
# real ops DB and so we control which rows exist.
_DDL = """
CREATE TABLE IF NOT EXISTS operational_patterns (
    pattern_id          TEXT PRIMARY KEY,
    symptom_signature   TEXT NOT NULL DEFAULT '',
    incident_type       TEXT NOT NULL DEFAULT '',
    services            TEXT NOT NULL DEFAULT '[]',
    canonical_symptoms  TEXT NOT NULL DEFAULT '[]',
    occurrence_count    INTEGER NOT NULL DEFAULT 1,
    success_count       INTEGER NOT NULL DEFAULT 0,
    first_seen          TEXT NOT NULL DEFAULT '',
    last_seen           TEXT NOT NULL DEFAULT ''
)
"""


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    db_path = tmp_path / "ops_intelligence.db"
    monkeypatch.setenv("OPS_DB_PATH", str(db_path))
    monkeypatch.setenv("OPS_DB_ENABLED", "true")
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_DDL)
    conn.commit()
    conn.close()
    # Reset ops_persistence singleton
    import database.ops_persistence as _ops
    monkeypatch.setattr(_ops, "_instance", None, raising=False)


def _seed_pattern(
    *,
    pattern_id: str,
    incident_type: str,
    service: str,
    occurrence_count: int,
    success_count: int = 0,
    canonical_symptoms: list[str] | None = None,
    last_seen: str = "2026-07-01T00:00:00Z",
) -> None:
    conn = sqlite3.connect(os.environ["OPS_DB_PATH"])
    conn.execute(
        "INSERT INTO operational_patterns "
        "(pattern_id, symptom_signature, incident_type, services, "
        " canonical_symptoms, occurrence_count, success_count, "
        " first_seen, last_seen) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            pattern_id,
            "sig:" + pattern_id,
            incident_type,
            json.dumps([service] if service else []),
            json.dumps(canonical_symptoms or ["database", "pool", "exhausted"]),
            occurrence_count,
            success_count,
            "2026-06-01T00:00:00Z",
            last_seen,
        ),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# ModuleSpec metadata
# ---------------------------------------------------------------------------

class TestSpec:
    def test_spec_name(self):
        assert PATTERN_RECOGNITION_SPEC.name == "pattern_recognition"

    def test_spec_stage_is_post_classify(self):
        assert PATTERN_RECOGNITION_SPEC.stage == IntelligenceStage.POST_CLASSIFY

    def test_spec_feature_flag(self):
        assert PATTERN_RECOGNITION_SPEC.feature_flag == PATTERN_RECOGNITION_FEATURE_FLAG
        assert PATTERN_RECOGNITION_FEATURE_FLAG == "ENABLE_PATTERN_RECOGNITION"

    def test_spec_priority_is_after_historical_lookup(self):
        assert PATTERN_RECOGNITION_SPEC.priority == 200

    def test_spec_has_no_dependencies(self):
        assert PATTERN_RECOGNITION_SPEC.dependencies == ()


# ---------------------------------------------------------------------------
# Skip semantics
# ---------------------------------------------------------------------------

class TestSkipSemantics:
    def test_no_service_no_incident_type_skips(self):
        ctx = RuntimeContext(
            investigation_id="inv-empty",
            stage=IntelligenceStage.POST_CLASSIFY,
            fetch_out={"incident": {"incident_id": "INC1"}, "service": ""},
            cres=_FakeCres(incident_type=""),
        )
        out = pattern_recognition_runner(ctx)
        assert out["status"] == "skipped"
        assert out["reason"] == "no_service_and_no_incident_type"
        assert out["version"] == RECOGNITION_VERSION


# ---------------------------------------------------------------------------
# Empty store yields empty match list (but success)
# ---------------------------------------------------------------------------

class TestEmptyStore:
    def test_empty_store_returns_success_zero_matches(self):
        out = pattern_recognition_runner(_ctx())
        assert out["status"] == "success"
        assert out["pattern_matches"] == []
        assert out["match_count"] == 0


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------

class TestPatternMatching:
    def test_matches_by_service_and_incident_type(self):
        _seed_pattern(
            pattern_id="abc123",
            incident_type="saturation",
            service="checkout",
            occurrence_count=5,
            success_count=4,
        )
        out = pattern_recognition_runner(_ctx())
        assert out["status"] == "success"
        matches = out["pattern_matches"]
        assert len(matches) == 1
        m = matches[0]
        assert m["pattern_id"] == "abc123"
        assert m["occurrence_count"] == 5
        assert m["success_count"] == 4
        assert m["success_rate"] == 0.8
        assert "checkout" in m["services"]

    def test_single_occurrence_filtered_out(self):
        """Patterns with < _MIN_OCCURRENCES should not surface as recurring."""
        assert _MIN_OCCURRENCES >= 2
        _seed_pattern(
            pattern_id="rare",
            incident_type="saturation",
            service="checkout",
            occurrence_count=1,
        )
        out = pattern_recognition_runner(_ctx())
        assert out["pattern_matches"] == []
        assert out["match_count"] == 0

    def test_only_matches_correct_incident_type(self):
        _seed_pattern(
            pattern_id="not-mine",
            incident_type="other-type",
            service="checkout",
            occurrence_count=5,
        )
        out = pattern_recognition_runner(_ctx(incident_type="saturation"))
        assert out["pattern_matches"] == []

    def test_matches_bounded_by_max(self):
        # Seed more than the cap
        for i in range(_MAX_MATCHES + 3):
            _seed_pattern(
                pattern_id=f"p-{i:02d}",
                incident_type="saturation",
                service="checkout",
                occurrence_count=10 - i,  # different counts so ORDER BY DESC is stable
            )
        out = pattern_recognition_runner(_ctx())
        assert len(out["pattern_matches"]) == _MAX_MATCHES
        assert out["match_count"] == _MAX_MATCHES

    def test_falls_back_to_incident_type_when_service_missing(self):
        _seed_pattern(
            pattern_id="type-only",
            incident_type="saturation",
            service="checkout",
            occurrence_count=3,
        )
        # Different service — should still find it because SQL filter is
        # applied per-column and service filter is optional.
        ctx = RuntimeContext(
            investigation_id="inv-type",
            stage=IntelligenceStage.POST_CLASSIFY,
            fetch_out={"service": ""},
            cres=_FakeCres(incident_type="saturation"),
        )
        out = pattern_recognition_runner(ctx)
        assert len(out["pattern_matches"]) == 1


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

class TestFeatureFlags:
    def test_master_off_does_not_execute(self, monkeypatch):
        from supervisor.intelligence_runtime import build_default_runtime
        monkeypatch.delenv("ENABLE_INTELLIGENCE_RUNTIME", raising=False)
        monkeypatch.setenv(PATTERN_RECOGNITION_FEATURE_FLAG, "true")
        rt = build_default_runtime()
        assert rt.is_enabled() is False
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        assert results == []

    def test_module_flag_off_yields_skipped(self, monkeypatch):
        monkeypatch.setenv("ENABLE_INTELLIGENCE_RUNTIME", "true")
        monkeypatch.delenv(PATTERN_RECOGNITION_FEATURE_FLAG, raising=False)
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        pr = next(r for r in results if r.name == "pattern_recognition")
        assert pr.status == "skipped"

    def test_module_flag_on_runs(self, monkeypatch):
        monkeypatch.setenv(PATTERN_RECOGNITION_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        pr = next(r for r in results if r.name == "pattern_recognition")
        assert pr.status == "success"
        assert pr.metadata["version"] == RECOGNITION_VERSION


# ---------------------------------------------------------------------------
# Receipt metadata lift
# ---------------------------------------------------------------------------

class TestReceiptLift:
    def test_receipt_carries_pattern_matches(self, monkeypatch):
        monkeypatch.setenv(PATTERN_RECOGNITION_FEATURE_FLAG, "true")
        _seed_pattern(
            pattern_id="lift-1",
            incident_type="saturation",
            service="checkout",
            occurrence_count=3,
            success_count=2,
        )
        from supervisor.phase_receipts import PhaseReceiptCollector
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        col = PhaseReceiptCollector()
        with col.record("classify") as _r:
            results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
            if results:
                _r.metadata["intelligence"] = [r.to_dict() for r in results]
        receipt = col.to_list()[0]
        arr = receipt["metadata"]["intelligence"]
        entry = next(e for e in arr if e["name"] == "pattern_recognition")
        assert entry["status"] == "success"
        assert entry["metadata"]["match_count"] == 1


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    def test_store_query_failure_returns_empty_matches(self, monkeypatch):
        monkeypatch.setenv(PATTERN_RECOGNITION_FEATURE_FLAG, "true")
        from intelligence import pattern_intelligence as _pi
        with patch.object(_pi.PatternIntelligenceStore, "query",
                           side_effect=RuntimeError("db offline")):
            out = pattern_recognition_runner(_ctx())
        # Runner still returns success — one source, but doesn't propagate
        assert out["status"] == "success"
        assert out["pattern_matches"] == []

    def test_runtime_catches_unexpected_runner_exception(self, monkeypatch):
        monkeypatch.setenv(PATTERN_RECOGNITION_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        from supervisor.intelligence_modules import pattern_recognition as _pr
        with patch.object(_pr, "_extract_service",
                           side_effect=ValueError("boom")):
            results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        pr = next(r for r in results if r.name == "pattern_recognition")
        assert pr.status == "failed"
        assert pr.error_type == "ValueError"


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------

class TestReadOnly:
    def test_lookup_does_not_write_to_operational_patterns(self, monkeypatch):
        monkeypatch.setenv(PATTERN_RECOGNITION_FEATURE_FLAG, "true")
        _seed_pattern(
            pattern_id="ro-1",
            incident_type="saturation",
            service="checkout",
            occurrence_count=3,
        )
        conn = sqlite3.connect(os.environ["OPS_DB_PATH"])
        before = conn.execute("SELECT COUNT(*) FROM operational_patterns").fetchone()[0]
        conn.close()
        pattern_recognition_runner(_ctx())
        conn = sqlite3.connect(os.environ["OPS_DB_PATH"])
        after = conn.execute("SELECT COUNT(*) FROM operational_patterns").fetchone()[0]
        conn.close()
        assert before == after


# ---------------------------------------------------------------------------
# Stage isolation
# ---------------------------------------------------------------------------

class TestStageIsolation:
    def test_post_classify_stage_does_not_run_persist_modules(self, monkeypatch):
        monkeypatch.setenv(PATTERN_RECOGNITION_FEATURE_FLAG, "true")
        monkeypatch.setenv("ENABLE_HISTORICAL_LOOKUP", "true")
        monkeypatch.setenv("ENABLE_RESOLUTION_MEMORY_WRITE", "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        names = {r.name for r in results}
        assert "pattern_recognition" in names
        assert "historical_lookup" in names
        assert "resolution_memory" not in names
        assert "investigation_store" not in names


# ---------------------------------------------------------------------------
# Agent wiring
# ---------------------------------------------------------------------------

class TestAgentWiring:
    def test_registered_via_install_default_modules(self):
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        assert rt.has("pattern_recognition")

    def test_module_appears_in_post_classify_plan(self):
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        specs = rt.modules_for(IntelligenceStage.POST_CLASSIFY)
        assert any(s.name == "pattern_recognition" for s in specs)

    def test_agent_source_unchanged_by_this_activation(self):
        """This activation must not require any change to agent.py."""
        import supervisor.agent as m
        src = open(m.__file__).read()
        assert "from supervisor.intelligence_modules import install_default_modules" in src
        assert "install_default_modules(_intel)" in src
        assert "pattern_recognition" not in src


# ---------------------------------------------------------------------------
# JSON-safety — receipt payload must serialize cleanly
# ---------------------------------------------------------------------------

class TestJsonSafety:
    def test_payload_json_roundtrip(self):
        _seed_pattern(
            pattern_id="js-1",
            incident_type="saturation",
            service="checkout",
            occurrence_count=4,
            success_count=3,
            canonical_symptoms=["db", "pool", "exhausted"],
        )
        out = pattern_recognition_runner(_ctx())
        s = json.dumps(out)
        d = json.loads(s)
        assert d["status"] == "success"
        assert d["match_count"] == 1
        assert d["pattern_matches"][0]["success_rate"] == 0.75
