"""Phase 18 — DecisionTrace activation tests.

Verifies that the ENABLE_DECISION_TRACE feature-flag wires the dormant
intelligence.decision_trace module into AnalyzePhase without changing any
public behavior when disabled, and produces the expected artifacts when
enabled.

Explicit coverage per the mission spec:
- Feature disabled (byte-identical behavior)
- Feature enabled (NDJSON write + receipt metadata attach)
- Single hypothesis
- Multiple hypotheses
- Rejected candidates (only winner is recorded, count matches)
- Receipt generation carries decision_trace summary
- Replay compatibility (short-circuit bypasses AnalyzePhase entirely)
- Failure handling (DecisionTraceLog.append error is swallowed)
- Regression protection (AnalyzeResult schema addition is backward-safe)
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from sentinel_core.context import ContextBuilder
from supervisor.phases.analyze import AnalyzePhase, AnalyzeResult
from supervisor.phases.contracts import PhaseStatus


# ---------------------------------------------------------------------------
# Fake supervisor surface (mirrors tests/test_analyze_phase.py)
# ---------------------------------------------------------------------------

class _Budget:
    def __init__(self, calls_made: int = 5, remaining: int = 20) -> None:
        self.case_id = "INC1"
        self.calls_made = calls_made
        self._remaining = remaining
    def remaining(self) -> int:
        return self._remaining


class _Receipts:
    def __init__(self) -> None:
        self.case_id = "INC1"


class _Gate:
    def __init__(self, passed: bool = True, verdict_value: str = "pass",
                 blocking_reason: str | None = None):
        self.passed = passed
        self._v = verdict_value
        self._r = blocking_reason
    @property
    def verdict(self):
        class _V:
            def __init__(self, v): self.value = v
        return _V(self._v)
    @property
    def blocking_gate(self):
        if self._r is None:
            return None
        class _BG:
            def __init__(self, r): self.reason = r
        return _BG(self._r)
    def to_dict(self):
        return {"passed": self.passed, "verdict": self._v,
                "blocking_reason": self._r}


@dataclass
class _FakeCollect:
    evidence: dict
    gate_post_collection: object = None
    early_return: object = None


@dataclass
class _FakeClassification:
    incident_type: str = "saturation"
    budget: object = None


class _FakeSup:
    """Minimal SentinalAISupervisor stand-in — same shape as
    tests/test_analyze_phase.py's helper."""

    def __init__(self, analyze_result=None, winner="winner_hypothesis_1",
                 hypothesis_count=3):
        self._tls = threading.local()
        self._analyze_result = analyze_result or {
            "incident_id": "INC-DT",
            "root_cause": "checkout DB pool exhaustion",
            "confidence": 74,
            "evidence_timeline": [{"phase": "collect"}],
            "reasoning": "reasoning",
            "_hypothesis_count": hypothesis_count,
            "_winner_hypothesis": winner,
            "_llm_metrics": {"input_tokens": 100},
        }

    def _analyze_evidence(self, incident_id, incident, incident_type, evidence):
        return dict(self._analyze_result)

    def _apply_self_critique(self, result, evidence, *a, **kw):
        return result, evidence

    def _empty_result(self, incident_id, reason, **kw):
        return {"incident_id": incident_id, "root_cause": reason,
                "confidence": 10, "evidence_timeline": [], "reasoning": reason}


def _fetch_out(service="checkout"):
    return {
        "incident":  {"summary": "x", "affected_service": service,
                       "created_at": "2026-07-04T10:00:00Z"},
        "summary":   "x",
        "service":   service,
        "receipts":  _Receipts(),
        "budget":    _Budget(),
        "circuits":  object(),
        "call_graph": None,
        "early_return": None,
    }


def _run_analyze(sup, evidence=None):
    return AnalyzePhase(sup).execute(
        ContextBuilder.for_incident("INC-DT"),
        _fetch_out(),
        _FakeClassification(budget=_Budget()),
        _FakeCollect(evidence=evidence or {"logs": [], "metrics": {}},
                     gate_post_collection=_Gate()),
    ).output.result["analyze"]


# ---------------------------------------------------------------------------
# Feature flag OFF — byte-identical behavior
# ---------------------------------------------------------------------------

class TestFlagDisabled:
    def test_default_env_is_off(self, monkeypatch):
        monkeypatch.delenv("ENABLE_DECISION_TRACE", raising=False)
        assert AnalyzePhase._decision_trace_enabled() is False

    @pytest.mark.parametrize("val", ["", "false", "0", "no", "off", "False"])
    def test_falsy_values_stay_off(self, monkeypatch, val):
        monkeypatch.setenv("ENABLE_DECISION_TRACE", val)
        assert AnalyzePhase._decision_trace_enabled() is False

    def test_no_metadata_returned_when_off(self, monkeypatch):
        monkeypatch.delenv("ENABLE_DECISION_TRACE", raising=False)
        sup = _FakeSup()
        aout = _run_analyze(sup)
        assert aout.decision_trace_meta == {}

    def test_no_ndjson_file_written_when_off(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ENABLE_DECISION_TRACE", raising=False)
        monkeypatch.setenv("INVESTIGATIONS_DIR", str(tmp_path))
        sup = _FakeSup()
        _run_analyze(sup)
        # No investigation NDJSON files should have appeared
        assert list(tmp_path.glob("*_decisions.jsonl")) == []

    def test_result_dict_is_identical_when_off(self, monkeypatch):
        """The `result` dict AnalyzePhase returns must contain the same keys
        with and without the feature flag; only decision_trace_meta on the
        AnalyzeResult wrapper differs."""
        monkeypatch.delenv("ENABLE_DECISION_TRACE", raising=False)
        sup = _FakeSup()
        aout_off = _run_analyze(sup)
        off_keys = set(aout_off.result.keys())

        monkeypatch.setenv("ENABLE_DECISION_TRACE", "true")
        aout_on = _run_analyze(_FakeSup())
        on_keys = set(aout_on.result.keys())
        assert off_keys == on_keys


# ---------------------------------------------------------------------------
# Feature flag ON — activation
# ---------------------------------------------------------------------------

class TestFlagEnabled:
    @pytest.mark.parametrize("val", ["true", "TRUE", "1", "yes", "on"])
    def test_truthy_values_enable(self, monkeypatch, val):
        monkeypatch.setenv("ENABLE_DECISION_TRACE", val)
        assert AnalyzePhase._decision_trace_enabled() is True

    def test_metadata_populated_when_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENABLE_DECISION_TRACE", "true")
        monkeypatch.setenv("INVESTIGATIONS_DIR", str(tmp_path))
        sup = _FakeSup(winner="oom_after_config_change", hypothesis_count=3)
        aout = _run_analyze(sup)
        meta = aout.decision_trace_meta
        assert meta["decision_type"] == "hypothesis_selection"
        assert meta["decision"] == "oom_after_config_change"
        assert meta["candidates_evaluated"] == 3
        assert meta["trace_id"]
        assert 0.0 <= meta["confidence"] <= 1.0

    def test_ndjson_file_written_when_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENABLE_DECISION_TRACE", "true")
        monkeypatch.setenv("INVESTIGATIONS_DIR", str(tmp_path))
        sup = _FakeSup()
        _run_analyze(sup)
        # The file is named "{investigation_id}_decisions.jsonl"
        matches = list(tmp_path.glob("*_decisions.jsonl"))
        assert len(matches) == 1
        # And contains one line of JSON
        lines = [ln.strip() for ln in matches[0].read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["decision_type"] == "hypothesis_selection"

    def test_supporting_evidence_derived_from_evidence_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENABLE_DECISION_TRACE", "true")
        monkeypatch.setenv("INVESTIGATIONS_DIR", str(tmp_path))
        sup = _FakeSup()
        # Underscore-prefixed keys should be filtered out
        aout = _run_analyze(sup, evidence={
            "logs": [],
            "metrics": {},
            "_hypothesis_count": 3,       # should be filtered
            "_suggested_causes": [],      # should be filtered
        })
        meta = aout.decision_trace_meta
        assert meta["supporting_evidence_count"] == 2

    def test_reasoning_path_has_ordered_steps(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENABLE_DECISION_TRACE", "true")
        monkeypatch.setenv("INVESTIGATIONS_DIR", str(tmp_path))
        sup = _FakeSup()
        _run_analyze(sup)
        payload = json.loads(next(tmp_path.glob("*_decisions.jsonl")).read_text().splitlines()[0])
        rp = payload["reasoning_path"]
        assert isinstance(rp, list) and len(rp) >= 2
        assert "produced" in rp[0]
        assert rp[1].startswith("winner:")


# ---------------------------------------------------------------------------
# Winner-selection variants
# ---------------------------------------------------------------------------

class TestWinnerVariants:
    def test_single_hypothesis(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENABLE_DECISION_TRACE", "true")
        monkeypatch.setenv("INVESTIGATIONS_DIR", str(tmp_path))
        sup = _FakeSup(winner="only_candidate", hypothesis_count=1)
        aout = _run_analyze(sup)
        assert aout.decision_trace_meta["candidates_evaluated"] == 1
        assert aout.decision_trace_meta["decision"] == "only_candidate"
        # And reasoning_path pluralization is grammatical
        payload = json.loads(next(tmp_path.glob("*_decisions.jsonl")).read_text().splitlines()[0])
        assert "1 hypothesis" in payload["reasoning_path"][0]

    def test_no_winner(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENABLE_DECISION_TRACE", "true")
        monkeypatch.setenv("INVESTIGATIONS_DIR", str(tmp_path))
        sup = _FakeSup(winner="none", hypothesis_count=0)
        aout = _run_analyze(sup)
        # The trace still records; downstream can distinguish via decision="none"
        assert aout.decision_trace_meta["decision"] == "none"
        assert aout.decision_trace_meta["candidates_evaluated"] == 0

    def test_many_hypotheses(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENABLE_DECISION_TRACE", "true")
        monkeypatch.setenv("INVESTIGATIONS_DIR", str(tmp_path))
        sup = _FakeSup(winner="winner", hypothesis_count=10)
        aout = _run_analyze(sup)
        assert aout.decision_trace_meta["candidates_evaluated"] == 10


# ---------------------------------------------------------------------------
# Receipt integration via investigate()
# ---------------------------------------------------------------------------

class TestReceiptIntegration:
    """Verify the receipt-metadata lift path — investigate() reads
    ``aout.decision_trace_meta`` and mixes it into the analyze receipt's
    ``metadata`` bag.  We drive the same code path via PhaseReceiptCollector
    directly (identical to the block at agent.py:365-373) so this test does
    not depend on the full pipeline surviving the collect-gate on empty
    mock evidence."""

    def _lift_meta_into_receipt(self, aout):
        """Mirror agent.py's ``if _aout_tmp.decision_trace_meta: _r.metadata
        [\"decision_trace\"] = ...`` wiring using a real PhaseReceiptCollector."""
        from supervisor.phase_receipts import PhaseReceiptCollector
        c = PhaseReceiptCollector()
        with c.record("analyze") as r:
            if aout.decision_trace_meta:
                r.metadata["decision_trace"] = aout.decision_trace_meta
        return c.to_list()[0]

    def test_analyze_receipt_carries_decision_trace_metadata(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ENABLE_DECISION_TRACE", "true")
        monkeypatch.setenv("INVESTIGATIONS_DIR", str(tmp_path))
        sup = _FakeSup(winner="checkout_pool_exhaustion", hypothesis_count=4)
        aout = _run_analyze(sup)
        receipt = self._lift_meta_into_receipt(aout)
        # metadata dict on the receipt must carry the decision_trace summary
        assert "decision_trace" in receipt["metadata"]
        dt = receipt["metadata"]["decision_trace"]
        assert dt["decision_type"] == "hypothesis_selection"
        assert dt["decision"] == "checkout_pool_exhaustion"
        assert dt["candidates_evaluated"] == 4
        assert "trace_id" in dt

    def test_analyze_receipt_omits_decision_trace_when_off(self, monkeypatch):
        monkeypatch.delenv("ENABLE_DECISION_TRACE", raising=False)
        sup = _FakeSup()
        aout = _run_analyze(sup)
        receipt = self._lift_meta_into_receipt(aout)
        # No decision_trace key on the receipt when the flag is off
        assert "decision_trace" not in receipt["metadata"]

    def test_agent_wiring_present(self):
        """Sentinel check: the receipt-lift line that installs the metadata
        must exist in agent.py."""
        import supervisor.agent as agent_mod
        src = open(agent_mod.__file__).read()
        assert "_r.metadata[\"decision_trace\"] = _aout_tmp.decision_trace_meta" in src


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

class TestFailureHandling:
    def test_append_failure_is_swallowed(self, monkeypatch):
        monkeypatch.setenv("ENABLE_DECISION_TRACE", "true")
        # Force DecisionTraceLog.append to raise via a patched import
        from unittest.mock import patch as _p
        with _p("intelligence.decision_trace.DecisionTraceLog") as mk:
            instance = mk.return_value
            instance.append.side_effect = RuntimeError("disk offline")
            sup = _FakeSup()
            aout = _run_analyze(sup)
        # No exception propagated; metadata falls back to empty
        assert aout.decision_trace_meta == {}

    def test_import_failure_falls_back_silently(self, monkeypatch):
        monkeypatch.setenv("ENABLE_DECISION_TRACE", "true")
        from unittest.mock import patch as _p
        # Patch DecisionTrace.make on the source module to raise
        with _p("intelligence.decision_trace.DecisionTrace.make",
                side_effect=RuntimeError("boom")):
            sup = _FakeSup()
            aout = _run_analyze(sup)
        assert aout.decision_trace_meta == {}


# ---------------------------------------------------------------------------
# Replay compatibility (replay short-circuit runs BEFORE AnalyzePhase)
# ---------------------------------------------------------------------------

class TestReplayCompatibility:
    def test_replay_result_has_no_decision_trace(self, tmp_path, monkeypatch):
        """Replay short-circuits inside investigate() at agent.py:312 and
        never reaches AnalyzePhase. Confirm that path produces no trace file
        and no metadata."""
        monkeypatch.setenv("ENABLE_DECISION_TRACE", "true")
        monkeypatch.setenv("INVESTIGATIONS_DIR", str(tmp_path / "traces"))
        (tmp_path / "traces").mkdir()

        from unittest.mock import MagicMock, Mock
        from supervisor.agent import SentinalAISupervisor
        from supervisor.replay import ReplayStore

        replay_dir = tmp_path / "replay"
        replay_dir.mkdir()
        store = ReplayStore(replay_dir=str(replay_dir))
        (replay_dir / "INC_REP_20260703T100000Z.json").write_text(json.dumps({
            "case_id": "INC_REP",
            "result": {"root_cause": "cached", "confidence": 88,
                        "evidence_timeline": [], "reasoning": "cached"},
            "evidence": {},
        }))

        supervisor = SentinalAISupervisor()
        supervisor._replay_store = store
        for name in supervisor.workers:
            supervisor.workers[name] = MagicMock()
            supervisor.workers[name].execute = Mock(side_effect=lambda a, p: {})

        result = supervisor.investigate("INC_REP", replay=True)
        # No trace files were created
        assert list((tmp_path / "traces").glob("*_decisions.jsonl")) == []
        # And no receipts key (replay short-circuit runs pre-collector too)
        assert "_phase_receipts" not in result


# ---------------------------------------------------------------------------
# Regression protection — AnalyzeResult schema is backward-compatible
# ---------------------------------------------------------------------------

class TestSchemaBackCompat:
    def test_analyzeresult_new_field_defaults_to_empty(self):
        # A default-constructed AnalyzeResult must still work — the new
        # decision_trace_meta field has a factory default of {}.
        r = AnalyzeResult(result={}, evidence={})
        assert r.decision_trace_meta == {}

    def test_existing_analyzeresult_fields_untouched(self):
        r = AnalyzeResult(
            result={"root_cause": "x"}, evidence={"logs": []},
            confidence=70, hypothesis_count=2, winner_hypothesis="w",
            llm_metrics={"input_tokens": 10},
        )
        assert r.confidence == 70
        assert r.hypothesis_count == 2
        assert r.winner_hypothesis == "w"
        assert r.llm_metrics == {"input_tokens": 10}
        assert r.decision_trace_meta == {}
