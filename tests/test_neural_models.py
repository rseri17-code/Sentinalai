"""Tests for NeuralQualityNet and NeuralConfidenceCalibrator.

Coverage:
  - Forward pass output is always in [0, 1]
  - train_one reduces loss over multiple steps on a trivial example
  - save() → load() round-trip restores identical weights
  - blend_alpha() ramp-up matches stated threshold (starts at 5, max at MIN_SAMPLES)
  - active / fully_blended flags are consistent with blend_alpha()
  - _CalMLP is gone — calibrator reuses _MLP from neural_quality_net
  - get_arch_stats() returns valid weight/bias norms
  - feature engineering produces correctly-shaped vectors
"""

import json
import math
import os
import tempfile

import pytest

from supervisor.neural_quality_net import (
    _MLP,
    NeuralQualityNet,
    build_features,
    build_features_from_result,
    MIN_SAMPLES_FOR_BLEND,
    MAX_BLEND_WEIGHT,
    _LAYER_SIZES,
)
from supervisor.neural_confidence_calibrator import (
    NeuralConfidenceCalibrator,
    MIN_SAMPLES_FULL,
    MAX_BLEND,
    _LAYER_SIZES as _CAL_LAYER_SIZES,
    _GRAD_CLIP as _CAL_GRAD_CLIP,
)


# ---------------------------------------------------------------------------
# _MLP — core forward/backward
# ---------------------------------------------------------------------------

class TestMLP:
    def _make(self, sizes=(3, 4, 1)):
        return _MLP(list(sizes), seed=42)

    def test_predict_in_unit_interval(self):
        mlp = self._make()
        for _ in range(20):
            import random
            rng = random.Random(7)
            x = [rng.uniform(-2, 2) for _ in range(3)]
            p = mlp.predict(x)
            assert 0.0 <= p <= 1.0, f"predict out of [0,1]: {p}"

    def test_train_one_returns_nonneg_loss(self):
        mlp = self._make()
        x = [0.5, 0.3, 0.8]
        loss = mlp.train_one(x, 1.0)
        assert loss >= 0.0

    def test_train_one_decreases_loss(self):
        """Loss should generally decrease over 100 steps on a trivial target."""
        mlp = _MLP([3, 8, 1], lr=0.05, seed=0)
        x = [1.0, 0.0, 1.0]
        target = 1.0
        first = mlp.train_one(x, target)
        for _ in range(99):
            mlp.train_one(x, target)
        last = mlp.predict(x)
        # After 100 steps, prediction should be significantly closer to 1.0
        assert last > 0.7, f"Expected prediction > 0.7 after training, got {last:.4f}"

    def test_total_samples_increments(self):
        mlp = self._make()
        assert mlp.total_samples == 0
        mlp.train_one([0.1, 0.2, 0.3], 0.5)
        assert mlp.total_samples == 1
        mlp.train_one([0.4, 0.5, 0.6], 0.5)
        assert mlp.total_samples == 2

    def test_train_batch_mean_loss(self):
        mlp = self._make()
        samples = [([0.1 * i, 0.2, 0.3], 1.0) for i in range(5)]
        loss = mlp.train_batch(samples)
        assert loss >= 0.0
        assert mlp.total_samples == 5

    def test_train_batch_empty(self):
        mlp = self._make()
        assert mlp.train_batch([]) == 0.0

    def test_grad_clip_stored(self):
        mlp = _MLP([2, 4, 1], grad_clip=2.5)
        assert mlp.grad_clip == 2.5

    def test_sigmoid_bounds(self):
        """Extreme pre-activations must not overflow."""
        mlp = self._make((2, 2, 1))
        # Manually set weights to produce extreme pre-activation
        mlp.W = [[[1e6] * 2, [1e6] * 2], [[1e6, 1e6]]]
        mlp.b = [[0.0, 0.0], [0.0]]
        p = mlp.predict([1.0, 1.0])
        assert 0.0 <= p <= 1.0


# ---------------------------------------------------------------------------
# _MLP serialization round-trip
# ---------------------------------------------------------------------------

class TestMLPSerialization:
    def _make_trained(self):
        mlp = _MLP([4, 8, 1], lr=0.01, seed=1)
        for i in range(10):
            mlp.train_one([i * 0.1, 0.5, 0.3, 0.2], 1.0)
        return mlp

    def test_roundtrip_weights(self):
        mlp = self._make_trained()
        d = mlp.to_dict()
        restored = _MLP.from_dict(d)
        x = [0.3, 0.5, 0.7, 0.9]
        assert abs(mlp.predict(x) - restored.predict(x)) < 1e-9

    def test_roundtrip_preserves_samples(self):
        mlp = self._make_trained()
        restored = _MLP.from_dict(mlp.to_dict())
        assert restored.total_samples == mlp.total_samples

    def test_roundtrip_preserves_grad_clip(self):
        mlp = _MLP([2, 4, 1], grad_clip=2.5)
        restored = _MLP.from_dict(mlp.to_dict())
        assert restored.grad_clip == 2.5

    def test_roundtrip_json_serializable(self):
        mlp = self._make_trained()
        json.dumps(mlp.to_dict())  # must not raise


# ---------------------------------------------------------------------------
# NeuralQualityNet
# ---------------------------------------------------------------------------

class TestNeuralQualityNet:
    def _fresh(self):
        return NeuralQualityNet()

    def test_predict_in_unit_interval(self):
        net = self._fresh()
        feats = [0.5, 0.6, 0.7, 0.8, 0.4, 0.65, 1.0, 0.0, 1.0]
        assert 0.0 <= net.predict(feats) <= 1.0

    def test_blend_alpha_zero_below_threshold(self):
        net = self._fresh()
        assert net.blend_alpha() == 0.0  # 0 samples

    def test_blend_alpha_starts_at_5_samples(self):
        net = self._fresh()
        feats = [0.5] * 9
        # Train 4 samples — still 0
        for _ in range(4):
            net.train_one(feats, 1.0)
        assert net.blend_alpha() == 0.0
        # 5th sample — now > 0
        net.train_one(feats, 1.0)
        assert net.blend_alpha() > 0.0

    def test_blend_alpha_reaches_max_at_min_samples(self):
        net = self._fresh()
        feats = [0.5] * 9
        for _ in range(MIN_SAMPLES_FOR_BLEND):
            net.train_one(feats, 1.0)
        assert abs(net.blend_alpha() - MAX_BLEND_WEIGHT) < 1e-9

    def test_blend_alpha_does_not_exceed_max(self):
        net = self._fresh()
        feats = [0.5] * 9
        for _ in range(MIN_SAMPLES_FOR_BLEND * 2):
            net.train_one(feats, 1.0)
        assert net.blend_alpha() <= MAX_BLEND_WEIGHT

    def test_active_flag_aligns_with_blend_alpha(self):
        """active must be True exactly when blend_alpha() > 0."""
        net = self._fresh()
        feats = [0.5] * 9
        for i in range(MIN_SAMPLES_FOR_BLEND):
            expected_active = i >= 5
            assert net.get_report()["active"] == expected_active, (
                f"active mismatch at sample {i}"
            )
            net.train_one(feats, 1.0)

    def test_fully_blended_flag(self):
        net = self._fresh()
        feats = [0.5] * 9
        assert not net.get_report()["fully_blended"]
        for _ in range(MIN_SAMPLES_FOR_BLEND):
            net.train_one(feats, 1.0)
        assert net.get_report()["fully_blended"]

    def test_save_load_roundtrip(self):
        net = NeuralQualityNet()
        feats = [0.5] * 9
        for _ in range(5):
            net.train_one(feats, 1.0)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            net.save(path)
            restored = NeuralQualityNet.load(path)
            assert abs(net.predict(feats) - restored.predict(feats)) < 1e-9
            assert restored.total_samples == net.total_samples
        finally:
            os.unlink(path)

    def test_load_missing_returns_fresh(self):
        net = NeuralQualityNet.load("/nonexistent/path/model.json")
        assert net.total_samples == 0

    def test_get_arch_stats_shape(self):
        net = self._fresh()
        stats = net.get_arch_stats()
        sizes = stats["layer_sizes"]
        assert len(stats["weight_norms"]) == len(sizes) - 1
        assert len(stats["bias_norms"]) == len(sizes) - 1
        for norm in stats["weight_norms"] + stats["bias_norms"]:
            assert norm >= 0.0

    def test_thread_safety(self):
        import threading
        net = self._fresh()
        feats = [0.5] * 9
        errors = []

        def worker():
            try:
                for _ in range(20):
                    net.predict(feats)
                    net.train_one(feats, 1.0)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"Thread safety errors: {errors}"


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

class TestFeatureEngineering:
    def test_build_features_length(self):
        feats = build_features(
            dims={"volume": 0.8, "coherence": 0.6, "calibration": 0.7,
                  "specificity": 0.5, "diversity": 0.4},
            raw_confidence=75,
            sources_found=["logs", "metrics"],
        )
        assert len(feats) == 9

    def test_build_features_all_in_unit_interval(self):
        feats = build_features(
            dims={"volume": 1.5, "coherence": -0.1},  # out-of-range inputs
            raw_confidence=110,
            sources_found=["logs"],
        )
        for f in feats:
            assert 0.0 <= f <= 1.0, f"Feature out of [0,1]: {f}"

    def test_build_features_source_flags(self):
        feats = build_features({}, 50, ["logs", "signals"])
        assert feats[6] == 1.0   # has_logs
        assert feats[7] == 0.0   # has_metrics
        assert feats[8] == 1.0   # has_signals

    def test_build_features_from_result_none_without_online_eval(self):
        result = {"confidence": 70, "root_cause": "DB timeout"}
        assert build_features_from_result(result) is None

    def test_build_features_from_result_with_online_eval(self):
        result = {
            "confidence": 80,
            "_online_eval": {
                "dimensions": {"volume": 0.9, "coherence": 0.7, "calibration": 0.6,
                               "specificity": 0.8, "diversity": 0.5},
                "sources_found": ["logs", "metrics"],
            },
        }
        feats = build_features_from_result(result)
        assert feats is not None
        assert len(feats) == 9


# ---------------------------------------------------------------------------
# NeuralConfidenceCalibrator
# ---------------------------------------------------------------------------

class TestNeuralConfidenceCalibrator:
    def _fresh(self):
        return NeuralConfidenceCalibrator()

    def test_no_duplication_uses_mlp(self):
        """_CalMLP must not exist — calibrator must use _MLP from neural_quality_net."""
        import supervisor.neural_confidence_calibrator as mod
        assert not hasattr(mod, "_CalMLP"), (
            "_CalMLP still present — code duplication not resolved"
        )
        ncal = self._fresh()
        assert isinstance(ncal._mlp, _MLP)

    def test_grad_clip_is_calibrator_specific(self):
        """Calibrator uses tighter grad_clip=3.0, not quality net's 5.0."""
        ncal = self._fresh()
        assert ncal._mlp.grad_clip == _CAL_GRAD_CLIP

    def test_layer_sizes(self):
        ncal = self._fresh()
        assert ncal._mlp.layer_sizes == _CAL_LAYER_SIZES  # [5, 16, 1]

    def test_calibrate_none_when_untrained(self):
        ncal = self._fresh()
        result = ncal.calibrate_with_context(70, {"some": "evidence"})
        assert result is None  # alpha==0 until 5 samples

    def test_calibrate_returns_value_after_training(self):
        ncal = self._fresh()
        for _ in range(5):
            ncal.train_one(70, 1.0)
        result = ncal.calibrate_with_context(70, {"_online_eval": {"source_count": 3}})
        assert result is not None
        assert 0.0 <= result <= 100.0

    def test_blend_alpha_starts_at_5_samples(self):
        ncal = self._fresh()
        for i in range(4):
            ncal.train_one(60, 1.0)
            assert ncal.blend_alpha() == 0.0
        ncal.train_one(60, 1.0)
        assert ncal.blend_alpha() > 0.0

    def test_blend_alpha_reaches_max(self):
        ncal = self._fresh()
        for _ in range(MIN_SAMPLES_FULL):
            ncal.train_one(60, 1.0)
        assert abs(ncal.blend_alpha() - MAX_BLEND) < 1e-9

    def test_active_flag_aligns_with_blend_alpha(self):
        ncal = self._fresh()
        for i in range(10):
            expected = i >= 5
            assert ncal.get_report()["active"] == expected, f"mismatch at sample {i}"
            ncal.train_one(70, 1.0)

    def test_save_load_roundtrip(self):
        ncal = NeuralConfidenceCalibrator()
        for _ in range(5):
            ncal.train_one(70, 1.0)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ncal.save(path)
            restored = NeuralConfidenceCalibrator.load(path)
            assert restored.total_samples == ncal.total_samples
            assert abs(
                ncal.calibrate_with_context(70, {"_online_eval": {"source_count": 2}})
                - restored.calibrate_with_context(70, {"_online_eval": {"source_count": 2}})
            ) < 1e-9
        finally:
            os.unlink(path)

    def test_load_restores_calibrator_grad_clip(self):
        """After save/load, grad_clip must be 3.0 (calibrator-specific), not 5.0."""
        ncal = NeuralConfidenceCalibrator()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ncal.save(path)
            restored = NeuralConfidenceCalibrator.load(path)
            assert restored._mlp.grad_clip == _CAL_GRAD_CLIP
        finally:
            os.unlink(path)

    def test_source_count_from_evidence(self):
        evidence = {
            "search_logs_result": ["log1"],
            "query_metrics_result": {"p95": 200},
            "_internal": "skip",
        }
        count = NeuralConfidenceCalibrator._source_count_from_evidence(evidence)
        assert count == 2  # logs + metrics

    def test_get_arch_stats_shape(self):
        ncal = self._fresh()
        stats = ncal.get_arch_stats()
        sizes = stats["layer_sizes"]
        assert len(stats["weight_norms"]) == len(sizes) - 1
        for norm in stats["weight_norms"] + stats["bias_norms"]:
            assert norm >= 0.0

    def test_train_from_result(self):
        ncal = self._fresh()
        result = {
            "confidence": 70,
            "_online_eval": {
                "source_count": 3,
                "dimensions": {"volume": 0.8, "coherence": 0.6, "specificity": 0.7},
            },
        }
        loss = ncal.train_from_result(result, 1.0)
        assert loss >= 0.0
        assert ncal.total_samples == 1
