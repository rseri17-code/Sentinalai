"""Phase 13 — AnalyzePhase behavioral tests.

Verifies the fourth behavioral decomposition: the ANALYZE stage now lives
in supervisor.phases.analyze.AnalyzePhase. Tests stub the supervisor
surface so the phase is exercised in isolation; agent-wiring tests confirm
the integration with investigate() preserves byte-equivalence.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from sentinel_core.context import ContextBuilder
from supervisor.phases.analyze import AnalyzePhase, AnalyzeResult
from supervisor.phases.contracts import PhaseResult, PhaseStatus


# ---------------------------------------------------------------------------
# Stand-in objects
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


class _Circuits:
    pass


class _Gate:
    """Stand-in GateResult for either pre-collect or post-analyze gates."""
    def __init__(self, passed: bool = True, verdict_value: str = "pass",
                 blocking_reason: str | None = None):
        self.passed = passed
        self._verdict_value = verdict_value
        self._blocking_reason = blocking_reason
    @property
    def verdict(self):
        class _V:
            def __init__(self, v): self.value = v
        return _V(self._verdict_value)
    @property
    def blocking_gate(self):
        if self._blocking_reason is None:
            return None
        class _BG:
            def __init__(self, r): self.reason = r
        return _BG(self._blocking_reason)
    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "verdict": self._verdict_value,
            "blocking_reason": self._blocking_reason,
        }


@dataclass
class _FakeCollect:
    """Stand-in CollectResult — fields AnalyzePhase reads."""
    evidence: dict
    gate_post_collection: object = None
    early_return: object = None


@dataclass
class _FakeClassification:
    incident_type: str = "saturation"
    budget: object = None


class _FakeSupervisor:
    """Minimal SentinalAISupervisor stand-in for AnalyzePhase isolation tests."""

    def __init__(self,
                 analyze_result=None,
                 critique_mutates: bool = False,
                 deadline: float | None = None):
        self._tls = threading.local()
        if deadline is not None:
            self._tls.investigation_deadline = deadline
        self._analyze_evidence_calls: list[tuple] = []
        self._critique_calls: list[tuple] = []
        self._empty_result_calls: list[tuple] = []
        self._analyze_result = analyze_result or {
            "incident_id": "INC1",
            "root_cause": "test cause",
            "confidence": 70,
            "evidence_timeline": [{"phase": "x"}],
            "reasoning": "test reasoning",
            "_hypothesis_count": 3,
            "_winner_hypothesis": "test_winner",
            "_llm_metrics": {"input_tokens": 100, "output_tokens": 50},
        }
        self._critique_mutates = critique_mutates

    def _analyze_evidence(self, incident_id, incident, incident_type, evidence):
        self._analyze_evidence_calls.append((incident_id, incident_type, len(evidence)))
        return dict(self._analyze_result)

    def _apply_self_critique(self, result, evidence, incident_id, incident_type,
                              service, receipts, budget, circuits):
        self._critique_calls.append((incident_id, incident_type, service))
        if self._critique_mutates:
            result["_critique_applied"] = True
            evidence["_critique_gap_evidence"] = {"new": True}
        return result, evidence

    def _empty_result(self, incident_id, reason, **kw):
        self._empty_result_calls.append((incident_id, reason, kw))
        out = {
            "incident_id": incident_id,
            "root_cause": reason,
            "confidence": 10,
            "evidence_timeline": [],
            "reasoning": f"Investigation could not proceed: {reason}",
        }
        if kw.get("degraded"):
            out["confidence_degraded"] = True
            out["confidence_degraded_reason"] = kw.get("degraded_reason", reason)
        return out


def _fetch_out(*, service="checkout"):
    return {
        "incident":  {"summary": "x", "affected_service": service,
                       "created_at": "2026-06-29T10:00:00Z"},
        "summary":   "x",
        "service":   service,
        "receipts":  _Receipts(),
        "budget":    _Budget(),
        "circuits":  _Circuits(),
        "call_graph": None,
        "early_return": None,
    }


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

class TestResultShape:
    def test_returns_phase_result(self):
        sup = _FakeSupervisor()
        result = AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _FakeClassification(budget=_Budget()),
            _FakeCollect(evidence={"logs": []}, gate_post_collection=_Gate()),
        )
        assert isinstance(result, PhaseResult)
        assert result.phase == "analyze"
        assert result.status == PhaseStatus.COMPLETED

    def test_output_holds_analyze_result(self):
        sup = _FakeSupervisor()
        result = AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _FakeClassification(budget=_Budget()),
            _FakeCollect(evidence={"logs": []}, gate_post_collection=_Gate()),
        )
        assert isinstance(result.output.result["analyze"], AnalyzeResult)


# ---------------------------------------------------------------------------
# Hypothesis-result and confidence
# ---------------------------------------------------------------------------

class TestHypothesisAndConfidence:
    def test_result_dict_passes_through(self):
        sup = _FakeSupervisor()
        out = AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _FakeClassification(budget=_Budget()),
            _FakeCollect(evidence={"logs": []}, gate_post_collection=_Gate()),
        ).output.result["analyze"]
        assert out.result["root_cause"] == "test cause"
        # confidence is an int (calibrator may rescale; we assert presence + type)
        assert isinstance(out.confidence, int)

    def test_transient_fields_popped_to_locals(self):
        sup = _FakeSupervisor()
        out = AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _FakeClassification(budget=_Budget()),
            _FakeCollect(evidence={"logs": []}, gate_post_collection=_Gate()),
        ).output.result["analyze"]
        assert out.hypothesis_count == 3
        assert out.winner_hypothesis == "test_winner"
        assert out.llm_metrics == {"input_tokens": 100, "output_tokens": 50}
        # And they must be REMOVED from result dict
        assert "_hypothesis_count" not in out.result
        assert "_winner_hypothesis" not in out.result
        assert "_llm_metrics" not in out.result


# ---------------------------------------------------------------------------
# Timeline + evidence snapshot
# ---------------------------------------------------------------------------

class TestTimelineAndSnapshot:
    def test_evidence_timeline_preserved(self):
        sup = _FakeSupervisor()
        out = AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _FakeClassification(budget=_Budget()),
            _FakeCollect(evidence={"logs": []}, gate_post_collection=_Gate()),
        ).output.result["analyze"]
        assert "evidence_timeline" in out.result

    def test_evidence_snapshot_excludes_underscore_keys(self):
        sup = _FakeSupervisor()
        ev = {
            "logs": [1, 2],
            "metrics": {"v": 1},
            "_incident_type": "x",         # underscore — excluded
            "_suggested_root_causes": [],  # underscore — excluded
        }
        out = AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _FakeClassification(budget=_Budget()),
            _FakeCollect(evidence=ev, gate_post_collection=_Gate()),
        ).output.result["analyze"]
        snapshot = out.result["_evidence_snapshot"]
        assert "logs" in snapshot
        assert "metrics" in snapshot
        assert "_incident_type" not in snapshot
        assert "_suggested_root_causes" not in snapshot


# ---------------------------------------------------------------------------
# Gate G2+G3+G5
# ---------------------------------------------------------------------------

class TestGatePostAnalysis:
    def test_gate_attached_to_result(self):
        sup = _FakeSupervisor()
        out = AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _FakeClassification(budget=_Budget()),
            _FakeCollect(evidence={"logs": []}, gate_post_collection=_Gate()),
        ).output.result["analyze"]
        assert "_gate_post_analysis" in out.result

    def test_collection_gate_attached_when_present(self):
        sup = _FakeSupervisor()
        coll_gate = _Gate(passed=True, verdict_value="pass")
        out = AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _FakeClassification(budget=_Budget()),
            _FakeCollect(evidence={"logs": []}, gate_post_collection=coll_gate),
        ).output.result["analyze"]
        assert out.result.get("_gate_post_collection") is not None

    def test_collection_gate_none_does_not_error(self):
        sup = _FakeSupervisor()
        out = AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _FakeClassification(budget=_Budget()),
            _FakeCollect(evidence={"logs": []}, gate_post_collection=None),
        ).output.result["analyze"]
        # Just confirm it didn't crash; gate field may simply be absent
        assert isinstance(out.result, dict)


# ---------------------------------------------------------------------------
# Self-critique mutation
# ---------------------------------------------------------------------------

class TestSelfCritique:
    def test_critique_called_with_full_dependencies(self):
        sup = _FakeSupervisor()
        AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(service="payments"),
            _FakeClassification(incident_type="error_spike", budget=_Budget()),
            _FakeCollect(evidence={"logs": []}, gate_post_collection=_Gate()),
        )
        assert sup._critique_calls == [("INC1", "error_spike", "payments")]

    def test_critique_mutation_propagates(self):
        sup = _FakeSupervisor(critique_mutates=True)
        ev = {"logs": []}
        out = AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _FakeClassification(budget=_Budget()),
            _FakeCollect(evidence=ev, gate_post_collection=_Gate()),
        ).output.result["analyze"]
        assert out.result.get("_critique_applied") is True
        assert "_critique_gap_evidence" in out.evidence


# ---------------------------------------------------------------------------
# Git blame + causal change extraction
# ---------------------------------------------------------------------------

class TestExtraction:
    def test_git_blame_pinpoint_when_blame_present(self):
        sup = _FakeSupervisor()
        ev = {
            "git_blame": {
                "culprit_file":  "svc/api.py",
                "culprit_line":  42,
                "sha":           "abc123def4567890",
                "author":        "alice",
                "date":          "2026-06-29",
                "message":       "feat: add new endpoint" + "x" * 200,
                "repo":          "svc/app",
            },
        }
        out = AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _FakeClassification(budget=_Budget()),
            _FakeCollect(evidence=ev, gate_post_collection=_Gate()),
        ).output.result["analyze"]
        pinpoint = out.result.get("git_blame_pinpoint")
        assert pinpoint is not None
        assert pinpoint["file"] == "svc/api.py"
        assert pinpoint["line"] == 42
        assert pinpoint["sha"] == "abc123def456"  # truncated to 12
        assert pinpoint["author"] == "alice"
        assert len(pinpoint["commit_message"]) <= 120

    def test_git_blame_pinpoint_absent_when_no_blame(self):
        sup = _FakeSupervisor()
        out = AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _FakeClassification(budget=_Budget()),
            _FakeCollect(evidence={"logs": []}, gate_post_collection=_Gate()),
        ).output.result["analyze"]
        assert "git_blame_pinpoint" not in out.result

    def test_causal_change_extracted_above_threshold(self):
        sup = _FakeSupervisor()
        ev = {
            "_itsm_change_correlations": [{
                "id":          "CHG1",
                "title":       "config flip",
                "change_type": "config_change",
                "risk_level":  "medium",
                "minutes_before_incident": 5,
                "correlation_score":   0.85,
                "correlation_reason":  "matched window",
                "matched_commit":      {"sha": "abc"},
            }],
        }
        out = AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _FakeClassification(budget=_Budget()),
            _FakeCollect(evidence=ev, gate_post_collection=_Gate()),
        ).output.result["analyze"]
        cc = out.result.get("causal_change")
        assert cc is not None
        assert cc["id"] == "CHG1"
        assert cc["correlation_score"] == 0.85

    def test_causal_change_skipped_below_threshold(self):
        sup = _FakeSupervisor()
        ev = {
            "_itsm_change_correlations": [{
                "id": "CHG1",
                "correlation_score": 0.30,  # below 0.45 threshold
            }],
        }
        out = AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _FakeClassification(budget=_Budget()),
            _FakeCollect(evidence=ev, gate_post_collection=_Gate()),
        ).output.result["analyze"]
        assert "causal_change" not in out.result


# ---------------------------------------------------------------------------
# Deadline early-return
# ---------------------------------------------------------------------------

class TestDeadlineEarlyReturn:
    def test_returns_empty_result_when_deadline_passed(self):
        import time as _time
        # Set deadline to a time in the PAST so the guard triggers immediately
        past_deadline = _time.monotonic() - 1.0
        sup = _FakeSupervisor(deadline=past_deadline)
        out = AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _FakeClassification(budget=_Budget()),
            _FakeCollect(evidence={"logs": []}, gate_post_collection=_Gate()),
        ).output.result["analyze"]
        assert out.early_return is not None
        assert "deadline" in out.early_return["root_cause"].lower()
        # And _analyze_evidence was NOT called
        assert sup._analyze_evidence_calls == []


# ---------------------------------------------------------------------------
# Empty / partial evidence
# ---------------------------------------------------------------------------

class TestEmptyAndPartial:
    def test_empty_evidence_does_not_crash(self):
        sup = _FakeSupervisor()
        out = AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _FakeClassification(budget=_Budget()),
            _FakeCollect(evidence={}, gate_post_collection=_Gate()),
        ).output.result["analyze"]
        assert isinstance(out.result, dict)
        # _evidence_snapshot should be empty (no keys to include)
        assert out.result["_evidence_snapshot"] == {}

    def test_partial_evidence_only_underscore_keys(self):
        sup = _FakeSupervisor()
        out = AnalyzePhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _FakeClassification(budget=_Budget()),
            _FakeCollect(evidence={"_incident_type": "x"},
                          gate_post_collection=_Gate()),
        ).output.result["analyze"]
        assert out.result["_evidence_snapshot"] == {}


# ---------------------------------------------------------------------------
# agent.py wiring
# ---------------------------------------------------------------------------

class TestAgentWiring:
    def test_agent_imports_analyze_phase(self):
        import supervisor.agent as agent_mod
        src = open(agent_mod.__file__).read()
        assert "from supervisor.phases.analyze import AnalyzePhase" in src

    def test_old_inline_calibrator_call_gone(self):
        """The inline get_calibrator().calibrate(...) used to live in
        investigate(); it now lives in AnalyzePhase. Sentinel for the move."""
        import supervisor.agent as agent_mod
        src = open(agent_mod.__file__).read()
        # The exact statement form from the legacy code
        assert "confidence = get_calibrator().calibrate(confidence, evidence_context=evidence)" not in src
        # And the post-analysis gate check is also moved
        assert "check_post_analysis(result, evidence, budget.remaining())" not in src

    def test_investigate_signature_unchanged(self):
        from supervisor.agent import SentinalAISupervisor
        import inspect
        sig = inspect.signature(SentinalAISupervisor.investigate)
        params = list(sig.parameters.keys())
        assert params == ["self", "incident_id", "replay"]


# ---------------------------------------------------------------------------
# Import safety
# ---------------------------------------------------------------------------

class TestImportSafety:
    def test_analyze_module_does_not_import_agent_at_load(self):
        import supervisor.phases.analyze as ap
        src = open(ap.__file__).read()
        for line in src.splitlines():
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            if line == stripped and stripped.startswith(("from supervisor.agent", "import supervisor.agent")):
                pytest.fail(f"top-level import of supervisor.agent in analyze.py: {line!r}")
