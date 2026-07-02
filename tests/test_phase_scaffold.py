"""Phase 7 — phase scaffold, contracts, and pure-helper extraction tests.

Verifies that:
- supervisor.phases is importable and exposes the contracts
- PhaseInput / PhaseOutput / PhaseResult / PhaseStatus serialize cleanly
- compute_confidence extraction preserves the supervisor.agent public API
  (identity, behavior, output)
- The five phase modules import without side effects and remain scaffolds
- No circular import is introduced
"""
from __future__ import annotations

import dataclasses
import importlib
import json

import pytest


# ---------------------------------------------------------------------------
# Phase package imports cleanly
# ---------------------------------------------------------------------------

class TestPackageImports:
    def test_phases_package_importable(self):
        import supervisor.phases  # must not raise
        assert hasattr(supervisor.phases, "PhaseInput")
        assert hasattr(supervisor.phases, "PhaseOutput")
        assert hasattr(supervisor.phases, "PhaseResult")
        assert hasattr(supervisor.phases, "PhaseStatus")

    def test_helpers_package_importable(self):
        import supervisor.helpers  # must not raise
        from supervisor.helpers.confidence import compute_confidence  # noqa: F401

    # All five phase modules are now live — no scaffolds remain.
    # Phase 10: fetch, Phase 11: classify, Phase 12: collect,
    # Phase 13: analyze, Phase 14: persist.

    def test_fetch_module_now_live(self):
        from supervisor.phases.fetch import FetchPhase
        assert FetchPhase is not None

    def test_classify_module_now_live(self):
        from supervisor.phases.classify import ClassificationPhase, ClassificationResult
        assert ClassificationPhase is not None
        assert ClassificationResult is not None

    def test_collect_module_now_live(self):
        from supervisor.phases.collect import CollectPhase, CollectResult
        assert CollectPhase is not None
        assert CollectResult is not None

    def test_analyze_module_now_live(self):
        from supervisor.phases.analyze import AnalyzePhase, AnalyzeResult
        assert AnalyzePhase is not None
        assert AnalyzeResult is not None

    def test_persist_module_now_live(self):
        from supervisor.phases.persist import PersistPhase, PersistResult
        assert PersistPhase is not None
        assert PersistResult is not None


# ---------------------------------------------------------------------------
# Phase contracts
# ---------------------------------------------------------------------------

class TestPhaseInput:
    def test_construction_with_context(self):
        from supervisor.phases import PhaseInput
        from sentinel_core.context import ContextBuilder

        ctx = ContextBuilder.for_incident("INC-A")
        inp = PhaseInput(ctx=ctx)
        assert inp.ctx is ctx
        assert inp.evidence == {}
        assert inp.extras == {}

    def test_input_is_frozen(self):
        from supervisor.phases import PhaseInput
        from sentinel_core.context import ContextBuilder

        inp = PhaseInput(ctx=ContextBuilder.for_incident("INC"))
        with pytest.raises(dataclasses.FrozenInstanceError):
            inp.evidence = {"x": 1}  # type: ignore[misc]

    def test_evidence_dict_is_independent_per_instance(self):
        from supervisor.phases import PhaseInput
        from sentinel_core.context import ContextBuilder

        a = PhaseInput(ctx=ContextBuilder.for_incident("A"))
        b = PhaseInput(ctx=ContextBuilder.for_incident("B"))
        a.evidence["x"] = 1
        assert "x" not in b.evidence


class TestPhaseOutput:
    def test_default_construction(self):
        from supervisor.phases import PhaseOutput

        out = PhaseOutput()
        assert out.evidence == {}
        assert out.result == {}

    def test_output_is_frozen(self):
        from supervisor.phases import PhaseOutput

        out = PhaseOutput()
        with pytest.raises(dataclasses.FrozenInstanceError):
            out.result = {"x": 1}  # type: ignore[misc]


class TestPhaseResult:
    def test_ok_when_completed(self):
        from supervisor.phases import PhaseOutput, PhaseResult, PhaseStatus

        r = PhaseResult(phase="fetch", status=PhaseStatus.COMPLETED, output=PhaseOutput())
        assert r.ok is True

    def test_not_ok_when_failed(self):
        from supervisor.phases import PhaseResult, PhaseStatus

        r = PhaseResult(phase="fetch", status=PhaseStatus.FAILED, error="boom")
        assert r.ok is False
        assert r.error == "boom"

    def test_phase_status_values_match_workflow_enum(self):
        from supervisor.phases import PhaseStatus
        from sentinel_core.models.workflow import PhaseStatus as WfStatus
        assert PhaseStatus is WfStatus


class TestContractsSerializeCleanly:
    def test_phase_output_dict_is_json_safe(self):
        from supervisor.phases import PhaseOutput
        out = PhaseOutput(
            evidence={"logs": [{"line": "abc"}]},
            result={"root_cause": "x", "confidence": 90},
        )
        # round-trip the two dict fields through JSON
        roundtripped_evidence = json.loads(json.dumps(out.evidence))
        roundtripped_result = json.loads(json.dumps(out.result))
        assert roundtripped_evidence == out.evidence
        assert roundtripped_result == out.result


# ---------------------------------------------------------------------------
# compute_confidence extraction — public API preserved
# ---------------------------------------------------------------------------

class TestComputeConfidenceExtraction:
    def test_supervisor_agent_still_exports_compute_confidence(self):
        from supervisor.agent import compute_confidence as agent_cc
        assert callable(agent_cc)

    def test_helpers_compute_confidence_same_object(self):
        from supervisor.agent import compute_confidence as agent_cc
        from supervisor.helpers.confidence import compute_confidence as helpers_cc
        assert agent_cc is helpers_cc

    def test_supervisor_agent_still_exports_hypothesis(self):
        from supervisor.agent import Hypothesis
        h = Hypothesis(
            name="x", root_cause="y", base_score=80.0,
            evidence_refs=[], reasoning="z",
        )
        assert h.name == "x"

    @pytest.mark.parametrize(
        "args, expected_floor, expected_ceil",
        [
            # base alone — no evidence → heavy penalty → close to 0 for non-symptom types
            (dict(base=80, logs=[], signals={}, metrics={}, events=[], changes=[]), 60, 80),
            # rich evidence → high
            (dict(
                base=80,
                logs=[{"line": "x"}] * 5,
                signals={"golden_signals": True, "anomaly_detected": True},
                metrics={"metrics": {"cpu": 99}, "pattern": "spike"},
                events=[{"e": 1}],
                changes=[{"c": 1}],
            ), 95, 100),
        ],
    )
    def test_compute_confidence_behavior_bounds(self, args, expected_floor, expected_ceil):
        from supervisor.agent import compute_confidence
        score = compute_confidence(**args)
        assert expected_floor <= score <= expected_ceil

    def test_absence_is_symptom_exempts_missing_penalties(self):
        """For silent_failure / missing_data, absent signals must NOT be penalized."""
        from supervisor.agent import compute_confidence
        no_signals = dict(
            base=80, logs=[], signals={}, metrics={},
            events=[], changes=[],
        )
        baseline = compute_confidence(**no_signals)
        silent = compute_confidence(**no_signals, incident_type="silent_failure")
        missing = compute_confidence(**no_signals, incident_type="missing_data")
        # silent_failure and missing_data skip the -5 / -3 penalties
        assert silent > baseline
        assert missing > baseline
        # silent and missing get the same treatment
        assert silent == missing

    def test_clamped_to_0_100(self):
        from supervisor.agent import compute_confidence
        very_low = compute_confidence(
            base=-1000, logs=[], signals={}, metrics={}, events=[], changes=[],
        )
        very_high = compute_confidence(
            base=10_000,
            logs=[{"x": 1}] * 50,
            signals={"golden_signals": True, "anomaly_detected": True},
            metrics={"metrics": {}, "pattern": "p"},
            events=[1], changes=[1],
            corroborating_sources=100,
        )
        assert very_low == 0
        assert very_high == 100


# ---------------------------------------------------------------------------
# Behavior parity — extracted code is byte-equivalent to the old impl
# ---------------------------------------------------------------------------

class TestBehaviorParity:
    """Reference implementation here mirrors what the function did BEFORE
    extraction. If the extracted helper diverges, this test fails."""

    @staticmethod
    def _reference(base, logs, signals, metrics, events, changes,
                   corroborating_sources=0, incident_type=""):
        score = base
        _ABSENCE = frozenset({"silent_failure", "missing_data"})
        sc = 0
        if logs:
            sc += 1
            score += min(len(logs), 5)
        if signals and signals.get("golden_signals"):
            sc += 1
            if signals.get("anomaly_detected"):
                score += 2
        if metrics and metrics.get("metrics"):
            sc += 1
            if metrics.get("pattern"):
                score += 1
        if events:
            sc += 1
        if changes:
            sc += 1
        score += sc * 2
        if incident_type not in _ABSENCE:
            if not signals or not signals.get("golden_signals"):
                score -= 5
            if not metrics or not metrics.get("metrics"):
                score -= 3
        score += corroborating_sources * 2
        return max(0, min(100, int(round(score))))

    @pytest.mark.parametrize("scenario", [
        # base, logs, signals, metrics, events, changes, corrob, incident_type
        (80, [], {}, {}, [], [], 0, ""),
        (80, [{"x": 1}] * 3, {}, {}, [], [], 0, ""),
        (80, [], {"golden_signals": True, "anomaly_detected": True}, {}, [], [], 0, ""),
        (80, [], {}, {"metrics": {"cpu": 1}, "pattern": "spike"}, [], [], 0, ""),
        (80, [], {}, {}, [], [], 0, "silent_failure"),
        (80, [], {}, {}, [], [], 0, "missing_data"),
        (60, [{"x": 1}], {"golden_signals": True}, {"metrics": {}}, [1], [1], 3, "error_spike"),
        (90, [{"a": 1}] * 10, {"golden_signals": True, "anomaly_detected": True},
            {"metrics": {"k": 1}, "pattern": "p"}, [1, 2], [1], 5, "oom"),
    ])
    def test_extracted_matches_reference(self, scenario):
        from supervisor.agent import compute_confidence
        base, logs, signals, metrics, events, changes, corrob, itype = scenario
        kwargs = dict(
            base=base, logs=logs, signals=signals, metrics=metrics,
            events=events, changes=changes,
            corroborating_sources=corrob, incident_type=itype,
        )
        assert compute_confidence(**kwargs) == self._reference(**kwargs)


# ---------------------------------------------------------------------------
# Import-cycle safety
# ---------------------------------------------------------------------------

class TestNoCircularImports:
    def test_helpers_confidence_has_no_supervisor_deps(self):
        import supervisor.helpers.confidence as mod
        src = open(mod.__file__).read()
        for forbidden in ("from supervisor.agent", "import supervisor.agent",
                          "from intelligence", "from workers"):
            assert forbidden not in src, (
                f"helpers.confidence must not depend on {forbidden!r}"
            )

    def test_phase_contracts_only_use_sentinel_core(self):
        import supervisor.phases.contracts as mod
        src = open(mod.__file__).read()
        for forbidden in (
            "from supervisor.agent", "import supervisor.agent",
            "from supervisor.workflow", "import supervisor.workflow",
            "from intelligence", "from workers", "from agui",
        ):
            assert forbidden not in src, (
                f"phases.contracts must not depend on {forbidden!r}"
            )

    def test_phase_scaffolds_have_no_imports_from_supervisor(self):
        """The scaffold modules must remain truly empty of supervisor coupling."""
        import os
        base = os.path.dirname(
            importlib.import_module("supervisor.phases").__file__
        )
        for name in ("fetch.py", "classify.py", "collect.py", "analyze.py", "persist.py"):
            with open(os.path.join(base, name)) as fh:
                src = fh.read()
            for forbidden in ("from supervisor.agent", "import supervisor.agent"):
                assert forbidden not in src, (
                    f"{name} must not import from supervisor.agent yet"
                )
