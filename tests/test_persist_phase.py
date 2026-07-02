"""Phase 14 — PersistPhase behavioral tests.

Verifies the fifth (final) behavioral decomposition: the PERSIST stage now
lives in supervisor.phases.persist.PersistPhase. Tests stub the supervisor
surface so the phase is exercised in isolation; agent-wiring tests confirm
the integration with investigate() preserves byte-equivalence.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from sentinel_core.context import ContextBuilder
from supervisor.phases.contracts import PhaseResult, PhaseStatus
from supervisor.phases.persist import PersistPhase, PersistResult


# ---------------------------------------------------------------------------
# Stand-in objects
# ---------------------------------------------------------------------------

class _Budget:
    def __init__(self, calls_made: int = 5) -> None:
        self.case_id = "INC1"
        self.calls_made = calls_made


class _Receipts:
    def __init__(self) -> None:
        self.case_id = "INC1"


class _Severity:
    def __init__(self, level: int = 3, label: str = "medium",
                 source: str = "moogsoft", budget: int = 25) -> None:
        self.level = level
        self.label = label
        self.source = source
        self.budget = budget


class _Span:
    """Stand-in trace span — supports set_attribute and elapsed_ms."""
    def __init__(self, elapsed_ms: float = 123.45) -> None:
        self.elapsed_ms = elapsed_ms
        self.attributes: dict = {}
    def set_attribute(self, key: str, value) -> None:
        self.attributes[key] = value


@dataclass
class _FakeClassification:
    incident_type: str = "saturation"
    severity: object = None
    budget: object = None
    itsm_context: object = None


@dataclass
class _FakeAnalyze:
    result: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    confidence: int = 70
    hypothesis_count: int = 3
    winner_hypothesis: str = "test_winner"
    llm_metrics: dict = field(default_factory=dict)
    early_return: object = None


class _ProposedFix:
    """Stand-in ProposedFix with the attributes PersistPhase reads."""
    def __init__(self, fix_type: str = "code_patch", confidence: float = 85,
                 sha: str = "abc123def456", repo: str = "svc/app",
                 risk_level: str = "low") -> None:
        self.fix_type = fix_type
        self.confidence = confidence
        self.sha = sha
        self.repo = repo
        self.risk_level = risk_level
    def to_dict(self) -> dict:
        return {
            "fix_type": self.fix_type,
            "confidence": self.confidence,
            "sha": self.sha,
            "repo": self.repo,
            "risk_level": self.risk_level,
            "description": "test description",
        }


class _FakeSupervisor:
    """Minimal SentinalAISupervisor stand-in for PersistPhase isolation tests."""

    def __init__(self, deadline: float | None = None,
                 proposed_fix: object = None,
                 judge_scores: dict | None = None,
                 record_observability_side_effect=None):
        self._tls = threading.local()
        if deadline is not None:
            self._tls.investigation_deadline = deadline
        self.workers = {"metrics_worker": object(), "log_worker": object(),
                        "itsm_worker": object()}
        # Track calls to the delegated helpers
        self.observability_calls: list = []
        self.judge_calls: list = []
        self.proposed_fix_calls: list = []
        self.persist_results_calls: list = []
        # Track submissions to _parallel_executor (VerificationLoop dispatch)
        self.executor_submits: list = []
        # Canned returns
        self._proposed_fix = proposed_fix
        self._judge_scores = judge_scores or {}
        # Provide a real executor-like object with a submit method
        self._parallel_executor = _RecordingExecutor(self.executor_submits)
        # Optional side effect for observability (e.g. to mutate span)
        self._observability_side_effect = record_observability_side_effect

    def _record_observability(self, span, result, evidence, budget, receipts,
                               incident_id, incident_type, service, confidence,
                               hypothesis_count, winner_hypothesis, llm_metrics):
        self.observability_calls.append(
            (incident_id, incident_type, service, confidence,
             hypothesis_count, winner_hypothesis)
        )
        if self._observability_side_effect:
            self._observability_side_effect(span)

    def _run_judge_scoring(self, incident_id, incident_type, result):
        self.judge_calls.append((incident_id, incident_type))
        return self._judge_scores

    def _generate_proposed_fix(self, incident_id, investigation_id, service,
                                evidence, result):
        self.proposed_fix_calls.append((incident_id, investigation_id, service))
        return self._proposed_fix

    def _persist_results(self, result, incident_id, incident_type, service,
                          evidence, receipts, budget, confidence,
                          hypothesis_count, winner_hypothesis, severity,
                          summary, llm_metrics, judge_scores, elapsed,
                          *, incident=None):
        self.persist_results_calls.append({
            "incident_id":       incident_id,
            "incident_type":     incident_type,
            "service":           service,
            "confidence":        confidence,
            "hypothesis_count":  hypothesis_count,
            "winner_hypothesis": winner_hypothesis,
            "summary":           summary,
            "judge_scores":      judge_scores,
            "elapsed":           elapsed,
            "incident":          incident,
        })


class _RecordingExecutor:
    def __init__(self, log_list) -> None:
        self._log = log_list
    def submit(self, fn, *args, **kwargs):
        self._log.append((fn, args, kwargs))
        return None


def _fetch_out(*, service="checkout", summary="checkout 5xx"):
    return {
        "incident":  {"summary": summary, "affected_service": service,
                       "created_at": "2026-06-29T10:00:00Z"},
        "summary":   summary,
        "service":   service,
        "receipts":  _Receipts(),
        "budget":    _Budget(),
        "circuits":  object(),
        "call_graph": None,
        "early_return": None,
    }


def _make_classification(**kw):
    return _FakeClassification(
        incident_type=kw.get("incident_type", "saturation"),
        severity=kw.get("severity", _Severity()),
        budget=kw.get("budget", _Budget()),
        itsm_context=kw.get("itsm_context", None),
    )


def _make_analyze(**kw):
    return _FakeAnalyze(
        result=kw.get("result", {
            "incident_id": "INC1",
            "root_cause": "test cause",
            "confidence": 70,
            "evidence_timeline": [],
            "reasoning": "test reasoning",
        }),
        evidence=kw.get("evidence", {}),
        confidence=kw.get("confidence", 70),
        hypothesis_count=kw.get("hypothesis_count", 3),
        winner_hypothesis=kw.get("winner_hypothesis", "test_winner"),
        llm_metrics=kw.get("llm_metrics", {"input_tokens": 100, "output_tokens": 50}),
    )


# ---------------------------------------------------------------------------
# Common patch set: silence the module-level side effects that PersistPhase
# invokes (remediation, dashboard, git-linker) so tests focus on wiring.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _stub_side_effects():
    # PersistPhase lazy-imports these inside execute(), so patch the SOURCE
    # modules (patch-where-defined pattern).
    with patch("supervisor.remediation.generate_remediation", return_value={"actions": []}), \
         patch("supervisor.metrics_dashboard.record_investigation_outcome"), \
         patch("supervisor.incident_git_linker.link_incident_to_commit"), \
         patch("intelligence.background_runner.get_runner"):
        yield


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

class TestResultShape:
    def test_returns_phase_result(self):
        sup = _FakeSupervisor()
        result = PersistPhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(), _make_classification(), _make_analyze(),
            span=_Span(),
        )
        assert isinstance(result, PhaseResult)
        assert result.phase == "persist"
        assert result.status == PhaseStatus.COMPLETED

    def test_output_holds_persist_result(self):
        sup = _FakeSupervisor()
        result = PersistPhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(), _make_classification(), _make_analyze(),
            span=_Span(),
        )
        assert isinstance(result.output.result["persist"], PersistResult)


# ---------------------------------------------------------------------------
# Observability + judge + remediation
# ---------------------------------------------------------------------------

class TestObservabilityAndJudge:
    def test_observability_called_with_correct_args(self):
        sup = _FakeSupervisor()
        PersistPhase(sup).execute(
            ContextBuilder.for_incident("INC42"),
            _fetch_out(service="payments"),
            _make_classification(incident_type="oom"),
            _make_analyze(confidence=88, hypothesis_count=5,
                          winner_hypothesis="oom_analyzer"),
            span=_Span(),
        )
        assert sup.observability_calls == [
            ("INC42", "oom", "payments", 88, 5, "oom_analyzer"),
        ]

    def test_judge_called_after_observability(self):
        sup = _FakeSupervisor(judge_scores={"overall": 0.87})
        PersistPhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(), _make_classification(), _make_analyze(),
            span=_Span(),
        )
        assert sup.judge_calls == [("INC1", "saturation")]
        # And the judge scores flow into _persist_results
        assert sup.persist_results_calls[0]["judge_scores"] == {"overall": 0.87}

    def test_remediation_added_to_result(self):
        sup = _FakeSupervisor()
        with patch("supervisor.remediation.generate_remediation",
                   return_value={"actions": ["scale up"]}):
            out = PersistPhase(sup).execute(
                ContextBuilder.for_incident("INC1"),
                _fetch_out(), _make_classification(),
                _make_analyze(result={"root_cause": "x"}),
                span=_Span(),
            ).output.result["persist"].result
        assert out["remediation"] == {"actions": ["scale up"]}


# ---------------------------------------------------------------------------
# Proposed fix conditional path
# ---------------------------------------------------------------------------

class TestProposedFix:
    def test_no_diff_analysis_skips_proposed_fix(self):
        sup = _FakeSupervisor(proposed_fix=_ProposedFix())
        out = PersistPhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(), _make_classification(),
            _make_analyze(evidence={"logs": []}),  # no diff_analysis
            span=_Span(),
        ).output.result["persist"].result
        assert "proposed_fix" not in out
        # And _generate_proposed_fix was NOT called
        assert sup.proposed_fix_calls == []

    def test_diff_analysis_present_triggers_fix_gen(self):
        sup = _FakeSupervisor(proposed_fix=_ProposedFix())
        out = PersistPhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(), _make_classification(),
            _make_analyze(evidence={"diff_analysis": {"culprit_file": "x"}}),
            span=_Span(),
        ).output.result["persist"].result
        assert sup.proposed_fix_calls  # was called
        assert "proposed_fix" in out
        assert out["proposed_fix"]["fix_type"] == "code_patch"

    def test_fix_type_none_does_not_populate_result(self):
        sup = _FakeSupervisor(proposed_fix=_ProposedFix(fix_type="none"))
        out = PersistPhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(), _make_classification(),
            _make_analyze(evidence={"diff_analysis": {"culprit_file": "x"}}),
            span=_Span(),
        ).output.result["persist"].result
        assert "proposed_fix" not in out

    def test_verification_loop_dispatched_when_fix_valid(self):
        sup = _FakeSupervisor(proposed_fix=_ProposedFix())
        PersistPhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(), _make_classification(),
            _make_analyze(evidence={"diff_analysis": {"culprit_file": "x"}}),
            span=_Span(),
        )
        # Executor.submit was invoked exactly once with the verification runner
        assert len(sup.executor_submits) == 1


# ---------------------------------------------------------------------------
# Dashboard metrics
# ---------------------------------------------------------------------------

class TestDashboardMetrics:
    def test_record_investigation_outcome_called(self):
        sup = _FakeSupervisor()
        with patch("supervisor.metrics_dashboard.record_investigation_outcome") as _mk:
            PersistPhase(sup).execute(
                ContextBuilder.for_incident("INC77"),
                _fetch_out(service="cart"),
                _make_classification(incident_type="error_spike"),
                _make_analyze(confidence=65),
                span=_Span(elapsed_ms=987.6),
            )
            assert _mk.called
            kwargs = _mk.call_args.kwargs
            assert kwargs["incident_id"] == "INC77"
            assert kwargs["incident_type"] == "error_spike"
            assert kwargs["service"] == "cart"
            assert kwargs["confidence"] == 65
            assert kwargs["elapsed_ms"] == 987.6
            assert kwargs["fix_applied"] is False
            assert kwargs["fix_verified"] is False


# ---------------------------------------------------------------------------
# Persist deadline guard
# ---------------------------------------------------------------------------

class TestPersistDeadlineGuard:
    def test_persist_called_when_within_deadline(self):
        import time as _time
        # Deadline far in the future
        sup = _FakeSupervisor(deadline=_time.monotonic() + 60.0)
        PersistPhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(), _make_classification(), _make_analyze(),
            span=_Span(),
        )
        assert len(sup.persist_results_calls) == 1

    def test_persist_skipped_when_deadline_passed(self):
        import time as _time
        past = _time.monotonic() - 1.0
        sup = _FakeSupervisor(deadline=past)
        out = PersistPhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(), _make_classification(),
            _make_analyze(result={"root_cause": "x"}),
            span=_Span(),
        ).output.result["persist"].result
        # _persist_results was NOT invoked
        assert sup.persist_results_calls == []
        # And the result was degraded
        assert out["confidence_degraded"] is True
        assert "persist_skipped:deadline_exceeded" in out["confidence_degraded_reason"]

    def test_persist_skip_preserves_prior_degraded_reason(self):
        import time as _time
        past = _time.monotonic() - 1.0
        sup = _FakeSupervisor(deadline=past)
        prior = _make_analyze(result={
            "root_cause": "x",
            "confidence_degraded_reason": "prior_reason",
        })
        out = PersistPhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(), _make_classification(), prior,
            span=_Span(),
        ).output.result["persist"].result
        # Reasons concatenated
        assert "prior_reason" in out["confidence_degraded_reason"]
        assert "persist_skipped:deadline_exceeded" in out["confidence_degraded_reason"]
        assert "; " in out["confidence_degraded_reason"]

    def test_no_deadline_still_persists(self):
        sup = _FakeSupervisor(deadline=None)  # TLS attribute absent
        PersistPhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(), _make_classification(), _make_analyze(),
            span=_Span(),
        )
        assert len(sup.persist_results_calls) == 1


# ---------------------------------------------------------------------------
# _persist_results call parity
# ---------------------------------------------------------------------------

class TestPersistResultsCall:
    def test_persist_receives_all_expected_inputs(self):
        sup = _FakeSupervisor()
        PersistPhase(sup).execute(
            ContextBuilder.for_incident("INC55"),
            _fetch_out(service="notif", summary="notif p95 spike"),
            _make_classification(incident_type="latency",
                                  severity=_Severity(level=2, label="high"),
                                  budget=_Budget(calls_made=12)),
            _make_analyze(
                result={"root_cause": "downstream slow"},
                evidence={"logs": [1]},
                confidence=82,
                hypothesis_count=4,
                winner_hypothesis="cascade",
                llm_metrics={"input_tokens": 500},
            ),
            span=_Span(elapsed_ms=1500.0),
        )
        call = sup.persist_results_calls[0]
        assert call["incident_id"]       == "INC55"
        assert call["incident_type"]     == "latency"
        assert call["service"]           == "notif"
        assert call["confidence"]        == 82
        assert call["hypothesis_count"]  == 4
        assert call["winner_hypothesis"] == "cascade"
        assert call["summary"]           == "notif p95 spike"
        assert call["elapsed"]           == 1500.0
        assert call["incident"] is not None


# ---------------------------------------------------------------------------
# Complete emit + feedback loop
# ---------------------------------------------------------------------------

class TestCompleteAndFeedback:
    def test_emit_complete_uses_confidence_and_root_cause(self):
        sup = _FakeSupervisor()
        with patch("supervisor.progress_stream.get_stream") as _mk_stream:
            emit_complete = _mk_stream.return_value.emit_complete
            PersistPhase(sup).execute(
                ContextBuilder.for_incident("INC1"),
                _fetch_out(),
                _make_classification(),
                _make_analyze(
                    result={"root_cause": "final cause",
                             "citation_coverage": 0.83},
                    confidence=91,
                ),
                span=_Span(elapsed_ms=200.0),
            )
            assert emit_complete.called
            kwargs = emit_complete.call_args.kwargs
            assert kwargs["investigation_id"] == "INC1"
            assert kwargs["root_cause"] == "final cause"
            assert kwargs["confidence"] == 91
            assert kwargs["citation_coverage"] == 0.83
            assert kwargs["elapsed_ms"] == 200.0

    def test_feedback_loop_exception_is_swallowed(self):
        """The Pattern Intelligence hook is best-effort; any exception must
        be swallowed so investigate() completes normally."""
        sup = _FakeSupervisor()
        # Make get_runner raise; PersistPhase must NOT re-raise
        with patch("intelligence.background_runner.get_runner",
                   side_effect=RuntimeError("runner offline")):
            out = PersistPhase(sup).execute(
                ContextBuilder.for_incident("INC1"),
                _fetch_out(), _make_classification(), _make_analyze(),
                span=_Span(),
            ).output.result["persist"].result
        assert "root_cause" in out


# ---------------------------------------------------------------------------
# None / empty optional inputs
# ---------------------------------------------------------------------------

class TestNoneOptionalInputs:
    def test_empty_evidence(self):
        sup = _FakeSupervisor()
        out = PersistPhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _make_classification(),
            _make_analyze(evidence={}, result={"root_cause": "x"}),
            span=_Span(),
        ).output.result["persist"].result
        assert isinstance(out, dict)

    def test_none_itsm_context(self):
        sup = _FakeSupervisor()
        out = PersistPhase(sup).execute(
            ContextBuilder.for_incident("INC1"),
            _fetch_out(),
            _make_classification(itsm_context=None),
            _make_analyze(),
            span=_Span(),
        ).output.result["persist"].result
        assert "remediation" in out

    def test_empty_llm_metrics(self):
        sup = _FakeSupervisor()
        with patch("supervisor.metrics_dashboard.record_investigation_outcome") as _mk:
            PersistPhase(sup).execute(
                ContextBuilder.for_incident("INC1"),
                _fetch_out(),
                _make_classification(),
                _make_analyze(llm_metrics={}),
                span=_Span(),
            )
            # Missing input_tokens / output_tokens must not crash — dict.get defaults to 0
            assert _mk.call_args.kwargs["llm_input_tokens"] == 0
            assert _mk.call_args.kwargs["llm_output_tokens"] == 0


# ---------------------------------------------------------------------------
# agent.py wiring
# ---------------------------------------------------------------------------

class TestAgentWiring:
    def test_agent_imports_persist_phase(self):
        import supervisor.agent as agent_mod
        src = open(agent_mod.__file__).read()
        assert "from supervisor.phases.persist import PersistPhase" in src

    def test_old_inline_persist_calls_gone(self):
        """The inline generate_remediation / record_investigation_outcome /
        _persist_results calls used to live in investigate(); they now live
        in PersistPhase. Sentinel for the move."""
        import supervisor.agent as agent_mod
        src = open(agent_mod.__file__).read()
        # These exact statement forms used to appear inline in investigate()
        assert "record_investigation_outcome(\n" not in src
        assert "self._persist_results(\n" not in src

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
    def test_persist_module_does_not_import_agent_at_load(self):
        import supervisor.phases.persist as pp
        src = open(pp.__file__).read()
        for line in src.splitlines():
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            if line == stripped and stripped.startswith(("from supervisor.agent", "import supervisor.agent")):
                pytest.fail(f"top-level import of supervisor.agent in persist.py: {line!r}")
