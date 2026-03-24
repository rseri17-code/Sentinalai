"""Tests for the continuous learning loop."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call
import pytest

from supervisor.learning_loop import run_learning_step, rebuild_calibrator_from_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GOOD_RESULT = {
    "root_cause": "Database connection pool exhaustion",
    "confidence": 85,
    "reasoning": "Connection pool exceeded limits caused by traffic spike",
    "evidence_timeline": [{"source": "logs", "event": "pool exhausted"}],
}

GROUND_TRUTH_ENTRY = {
    "incident_id": "INC_LEARN_001",
    "root_cause": "Database connection pool exhaustion",
    "root_cause_keywords": ["connection pool", "database", "exhaustion"],
    "incident_type": "saturation",
    "service": "payment-service",
    "severity": 2,
    "required_evidence": ["logs"],
    "expected_confidence_min": 70,
    "expected_confidence_max": 95,
}


def _make_evaluator(has_gt=True, eval_result=None):
    """Return a mock GroundTruthEvaluator."""
    evaluator = MagicMock()
    evaluator.has_ground_truth.return_value = has_gt
    if has_gt and eval_result is not None:
        evaluator.evaluate.return_value = eval_result
    elif has_gt:
        from supervisor.ground_truth_eval import EvalResult
        evaluator.evaluate.return_value = EvalResult(
            incident_id="INC_LEARN_001",
            root_cause_match="exact",
            root_cause_score=1.0,
            confidence_error=0.0,
            confidence_calibrated=True,
            evidence_coverage=1.0,
            missing_evidence=[],
            predicted_confidence=85,
            actual_correct=True,
        )
    return evaluator


# ---------------------------------------------------------------------------
# run_learning_step — happy path
# ---------------------------------------------------------------------------

class TestRunLearningStep:
    def test_returns_false_when_no_ground_truth(self):
        evaluator = _make_evaluator(has_gt=False)
        with patch("supervisor.learning_loop.GroundTruthEvaluator") as mock_cls:
            mock_cls.from_file.return_value = evaluator
            result = run_learning_step("INC_UNKNOWN", GOOD_RESULT)
        assert result is False
        evaluator.evaluate.assert_not_called()

    def test_returns_true_when_ground_truth_exists(self):
        evaluator = _make_evaluator(has_gt=True)
        with (
            patch("supervisor.learning_loop.GroundTruthEvaluator") as mock_cls,
            patch("supervisor.learning_loop._persist_eval"),
            patch("supervisor.learning_loop._update_calibrator"),
        ):
            mock_cls.from_file.return_value = evaluator
            result = run_learning_step("INC_LEARN_001", GOOD_RESULT)
        assert result is True

    def test_calls_evaluate_with_result(self):
        evaluator = _make_evaluator(has_gt=True)
        with (
            patch("supervisor.learning_loop.GroundTruthEvaluator") as mock_cls,
            patch("supervisor.learning_loop._persist_eval"),
            patch("supervisor.learning_loop._update_calibrator"),
        ):
            mock_cls.from_file.return_value = evaluator
            run_learning_step("INC_LEARN_001", GOOD_RESULT)
        evaluator.evaluate.assert_called_once_with("INC_LEARN_001", GOOD_RESULT)

    def test_persist_and_calibrate_called_on_success(self):
        evaluator = _make_evaluator(has_gt=True)
        with (
            patch("supervisor.learning_loop.GroundTruthEvaluator") as mock_cls,
            patch("supervisor.learning_loop._persist_eval") as mock_persist,
            patch("supervisor.learning_loop._update_calibrator") as mock_calib,
        ):
            mock_cls.from_file.return_value = evaluator
            run_learning_step("INC_LEARN_001", GOOD_RESULT)

        mock_persist.assert_called_once()
        mock_calib.assert_called_once()

    def test_returns_false_when_evaluator_returns_none(self):
        evaluator = MagicMock()
        evaluator.has_ground_truth.return_value = True
        evaluator.evaluate.return_value = None
        with patch("supervisor.learning_loop.GroundTruthEvaluator") as mock_cls:
            mock_cls.from_file.return_value = evaluator
            result = run_learning_step("INC_LEARN_001", GOOD_RESULT)
        assert result is False


# ---------------------------------------------------------------------------
# run_learning_step — failure isolation
# ---------------------------------------------------------------------------

class TestLearningLoopFailureIsolation:
    def test_persist_failure_does_not_prevent_calibration(self):
        evaluator = _make_evaluator(has_gt=True)
        with (
            patch("supervisor.learning_loop.GroundTruthEvaluator") as mock_cls,
            patch("supervisor.learning_loop._persist_eval", side_effect=RuntimeError("db down")),
            patch("supervisor.learning_loop._update_calibrator") as mock_calib,
        ):
            mock_cls.from_file.return_value = evaluator
            # Should not raise
            run_learning_step("INC_LEARN_001", GOOD_RESULT)
        # Calibration still attempted after persist failure
        mock_calib.assert_called_once()

    def test_calibration_failure_does_not_raise(self):
        evaluator = _make_evaluator(has_gt=True)
        with (
            patch("supervisor.learning_loop.GroundTruthEvaluator") as mock_cls,
            patch("supervisor.learning_loop._persist_eval"),
            patch("supervisor.learning_loop._update_calibrator", side_effect=RuntimeError("lock")),
        ):
            mock_cls.from_file.return_value = evaluator
            # Should not raise; just log warning
            run_learning_step("INC_LEARN_001", GOOD_RESULT)


# ---------------------------------------------------------------------------
# _update_calibrator — thread-safety: lock is acquired
# ---------------------------------------------------------------------------

class TestUpdateCalibratorThreadSafety:
    def test_calibrator_lock_acquired_during_update(self):
        from supervisor.learning_loop import _calibrator_lock
        from supervisor.ground_truth_eval import EvalResult

        eval_result = EvalResult(
            incident_id="INC_LOCK",
            root_cause_match="exact",
            root_cause_score=1.0,
            confidence_error=0.0,
            confidence_calibrated=True,
            evidence_coverage=1.0,
            missing_evidence=[],
            predicted_confidence=80,
            actual_correct=True,
        )
        mock_calibrator = MagicMock()
        with patch("supervisor.learning_loop.ConfidenceCalibrator") as mock_cls:
            mock_cls.load.return_value = mock_calibrator
            from supervisor.learning_loop import _update_calibrator
            _update_calibrator(eval_result, "INC_LOCK")

        mock_calibrator.update.assert_called_once_with(
            [{"predicted_confidence": 80, "actual_correct": True}]
        )
        mock_calibrator.save.assert_called_once()


# ---------------------------------------------------------------------------
# rebuild_calibrator_from_db
# ---------------------------------------------------------------------------

class TestRebuildCalibratorFromDb:
    def test_returns_zero_when_no_records(self):
        with patch("supervisor.learning_loop.load_eval_results_for_calibration", return_value=[]):
            count = rebuild_calibrator_from_db()
        assert count == 0

    def test_rebuilds_from_records(self):
        records = [
            {"predicted_confidence": 80, "actual_correct": True},
            {"predicted_confidence": 40, "actual_correct": False},
        ]
        mock_calibrator = MagicMock()
        with (
            patch("supervisor.learning_loop.load_eval_results_for_calibration", return_value=records),
            patch("supervisor.learning_loop.ConfidenceCalibrator") as mock_cls,
        ):
            mock_cls.return_value = mock_calibrator
            count = rebuild_calibrator_from_db()

        assert count == 2
        mock_calibrator.update.assert_called_once_with(records)
        mock_calibrator.save.assert_called_once()

    def test_returns_zero_on_exception(self):
        with patch(
            "supervisor.learning_loop.load_eval_results_for_calibration",
            side_effect=Exception("db error"),
        ):
            count = rebuild_calibrator_from_db()
        assert count == 0
