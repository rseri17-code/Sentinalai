"""EpisodicMemoryLookup activation tests."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from sentinel_core.runtime import (
    IntelligenceRuntime,
    IntelligenceStage,
    RuntimeContext,
)
from supervisor.intelligence_modules import install_default_modules
from supervisor.intelligence_modules.episodic_memory_lookup import (
    EPISODIC_MEMORY_LOOKUP_FEATURE_FLAG,
    EPISODIC_MEMORY_LOOKUP_SPEC,
    LOOKUP_VERSION,
    _MAX_MATCHES,
    episodic_memory_lookup_runner,
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
            "incident": {"incident_id": incident_id, "affected_service": service},
            "service":  service,
        },
        cres=_FakeCres(incident_type=incident_type),
    )


@pytest.fixture(autouse=True)
def _isolate_memory(tmp_path, monkeypatch):
    """Point EPISODIC_MEMORY_PATH at an empty file so no demo seeding fires."""
    path = tmp_path / "episodic_memory.jsonl"
    path.write_text("")   # touch — prevents seed_demo_episodes
    monkeypatch.setenv("EPISODIC_MEMORY_PATH", str(path))


def _seed_episode(
    *,
    episode_id: str,
    service: str,
    incident_type: str,
    root_cause: str = "DB pool exhausted",
    resolution_action: str = "restarted pool",
    outcome: str = "resolved",
    confidence: float = 0.8,
) -> None:
    row = {
        "episode_id":         episode_id,
        "incident_id":        "INC-" + episode_id,
        "service":            service,
        "incident_type":      incident_type,
        "failure_signature":  root_cause,
        "root_cause":         root_cause,
        "confidence":         confidence,
        "resolution_action":  resolution_action,
        "resolved_by":        "SRE-on-call",
        "time_to_resolve_ms": 90000,
        "evidence_keys":      ["logs"],
        "outcome":            outcome,
        "tags":               ["database"],
        "recorded_at":        datetime.now(timezone.utc).isoformat(),
    }
    with open(os.environ["EPISODIC_MEMORY_PATH"], "a") as f:
        f.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# ModuleSpec
# ---------------------------------------------------------------------------

class TestSpec:
    def test_name(self):
        assert EPISODIC_MEMORY_LOOKUP_SPEC.name == "episodic_memory_lookup"

    def test_stage(self):
        assert EPISODIC_MEMORY_LOOKUP_SPEC.stage == IntelligenceStage.POST_CLASSIFY

    def test_feature_flag(self):
        assert EPISODIC_MEMORY_LOOKUP_SPEC.feature_flag == EPISODIC_MEMORY_LOOKUP_FEATURE_FLAG
        assert EPISODIC_MEMORY_LOOKUP_FEATURE_FLAG == "ENABLE_EPISODIC_MEMORY_LOOKUP"

    def test_priority(self):
        assert EPISODIC_MEMORY_LOOKUP_SPEC.priority == 500


# ---------------------------------------------------------------------------
# Skip / Empty
# ---------------------------------------------------------------------------

class TestSkipAndEmpty:
    def test_no_signal_skips(self):
        ctx = RuntimeContext(
            investigation_id="inv-x",
            stage=IntelligenceStage.POST_CLASSIFY,
            fetch_out={"service": ""},
            cres=_FakeCres(incident_type=""),
        )
        out = episodic_memory_lookup_runner(ctx)
        assert out["status"] == "skipped"
        assert out["reason"] == "no_service_and_no_incident_type"

    def test_empty_store_returns_success_empty(self):
        out = episodic_memory_lookup_runner(_ctx())
        assert out["status"] == "success"
        assert out["episodes"] == []
        assert out["match_count"] == 0


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

class TestMatching:
    def test_matches_by_service_and_incident_type(self):
        _seed_episode(
            episode_id="e1",
            service="checkout",
            incident_type="saturation",
            root_cause="DB pool exhausted at checkout",
            resolution_action="scaled pool to 100 conns",
        )
        out = episodic_memory_lookup_runner(_ctx())
        assert out["match_count"] == 1
        ep = out["episodes"][0]
        assert ep["episode_id"] == "e1"
        assert "DB pool" in ep["root_cause_head"]
        assert "scaled" in ep["resolution_action_head"]
        assert ep["outcome"] == "resolved"

    def test_does_not_match_other_service(self):
        _seed_episode(episode_id="e-other", service="payments",
                       incident_type="saturation")
        out = episodic_memory_lookup_runner(_ctx(service="checkout"))
        assert out["episodes"] == []

    def test_bounded_by_max(self):
        for i in range(_MAX_MATCHES + 3):
            _seed_episode(episode_id=f"em-{i:02d}",
                           service="checkout",
                           incident_type="saturation",
                           root_cause=f"cause #{i}")
        out = episodic_memory_lookup_runner(_ctx())
        assert len(out["episodes"]) == _MAX_MATCHES
        assert out["match_count"] == _MAX_MATCHES

    def test_root_cause_and_action_heads_truncated(self):
        long_rc = "x" * 500
        long_ac = "y" * 500
        _seed_episode(
            episode_id="e-long",
            service="checkout",
            incident_type="saturation",
            root_cause=long_rc,
            resolution_action=long_ac,
        )
        out = episodic_memory_lookup_runner(_ctx())
        ep = out["episodes"][0]
        assert len(ep["root_cause_head"]) <= 160
        assert len(ep["resolution_action_head"]) <= 160


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

class TestFeatureFlags:
    def test_master_off(self, monkeypatch):
        from supervisor.intelligence_runtime import build_default_runtime
        monkeypatch.delenv("ENABLE_INTELLIGENCE_RUNTIME", raising=False)
        monkeypatch.setenv(EPISODIC_MEMORY_LOOKUP_FEATURE_FLAG, "true")
        rt = build_default_runtime()
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        assert results == []

    def test_module_flag_off_yields_skipped(self, monkeypatch):
        monkeypatch.setenv("ENABLE_INTELLIGENCE_RUNTIME", "true")
        monkeypatch.delenv(EPISODIC_MEMORY_LOOKUP_FEATURE_FLAG, raising=False)
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        entry = next(r for r in results if r.name == "episodic_memory_lookup")
        assert entry.status == "skipped"

    def test_module_flag_on(self, monkeypatch):
        monkeypatch.setenv(EPISODIC_MEMORY_LOOKUP_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        entry = next(r for r in results if r.name == "episodic_memory_lookup")
        assert entry.status == "success"
        assert entry.metadata["version"] == LOOKUP_VERSION


# ---------------------------------------------------------------------------
# Receipt lift + failure isolation + read-only
# ---------------------------------------------------------------------------

class TestReceiptLift:
    def test_metadata_flows(self, monkeypatch):
        monkeypatch.setenv(EPISODIC_MEMORY_LOOKUP_FEATURE_FLAG, "true")
        _seed_episode(episode_id="e-lift", service="checkout",
                       incident_type="saturation")
        from supervisor.phase_receipts import PhaseReceiptCollector
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        col = PhaseReceiptCollector()
        with col.record("classify") as _r:
            results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
            if results:
                _r.metadata["intelligence"] = [r.to_dict() for r in results]
        entry = next(e for e in col.to_list()[0]["metadata"]["intelligence"]
                       if e["name"] == "episodic_memory_lookup")
        assert entry["status"] == "success"
        assert entry["metadata"]["match_count"] == 1


class TestFailureIsolation:
    def test_search_failure_returns_empty(self, monkeypatch):
        monkeypatch.setenv(EPISODIC_MEMORY_LOOKUP_FEATURE_FLAG, "true")
        from intelligence import episodic_memory as _em
        with patch.object(_em.EpisodicMemory, "search",
                           side_effect=RuntimeError("boom")):
            out = episodic_memory_lookup_runner(_ctx())
        assert out["status"] == "success"
        assert out["episodes"] == []

    def test_runtime_catches_unexpected(self, monkeypatch):
        monkeypatch.setenv(EPISODIC_MEMORY_LOOKUP_FEATURE_FLAG, "true")
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        from supervisor.intelligence_modules import episodic_memory_lookup as _eml
        with patch.object(_eml, "_extract_service",
                           side_effect=ValueError("boom")):
            results = rt.run_stage(IntelligenceStage.POST_CLASSIFY, _ctx())
        entry = next(r for r in results if r.name == "episodic_memory_lookup")
        assert entry.status == "failed"


class TestReadOnly:
    def test_lookup_does_not_write_new_episodes(self):
        _seed_episode(episode_id="ro-1", service="checkout",
                       incident_type="saturation")
        before = open(os.environ["EPISODIC_MEMORY_PATH"]).read()
        episodic_memory_lookup_runner(_ctx())
        after = open(os.environ["EPISODIC_MEMORY_PATH"]).read()
        assert before == after


class TestAgentWiring:
    def test_registered(self):
        rt = IntelligenceRuntime(enabled=True)
        install_default_modules(rt)
        assert rt.has("episodic_memory_lookup")

    def test_agent_source_untouched(self):
        import supervisor.agent as m
        src = open(m.__file__).read()
        assert "install_default_modules(_intel)" in src
        assert "episodic_memory_lookup" not in src


class TestJsonSafety:
    def test_payload_roundtrip(self):
        _seed_episode(episode_id="js-1", service="checkout",
                       incident_type="saturation")
        out = episodic_memory_lookup_runner(_ctx())
        s = json.dumps(out)
        d = json.loads(s)
        assert d["status"] == "success"
        assert d["match_count"] == 1
