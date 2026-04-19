"""Tests for new functions in supervisor.strategy_evolver:
   should_skip_step(), record_gap_pattern(), get_service_weight(), per-service weights.
"""
from __future__ import annotations

import os
import json
import pytest

os.environ.setdefault("STRATEGY_EVOLVER_ENABLED", "true")


def _make_path(tmp_path):
    return str(tmp_path / "evolved_strategy.json")


# ---------------------------------------------------------------------------
# should_skip_step()
# ---------------------------------------------------------------------------

class TestShouldSkipStep:

    def test_returns_false_when_disabled(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        monkeypatch.setattr(mod, "STRATEGY_EVOLVER_ENABLED", False)
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", _make_path(tmp_path))
        assert mod.should_skip_step("timeout", "get_traces") is False

    def test_returns_false_when_no_data(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", _make_path(tmp_path))
        assert mod.should_skip_step("timeout", "get_traces") is False

    def test_returns_false_when_insufficient_calls(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", p)
        # Write an entry with calls < MIN_CALLS_TO_EVOLVE (5)
        data = {"timeout": {"get_traces": {"weight": 0.1, "calls": 2, "ema_signal": 0.1}}}
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(data, f)
        assert mod.should_skip_step("timeout", "get_traces") is False

    def test_returns_true_when_weight_below_threshold(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", p)
        # Weight 0.20 is below default threshold 0.35
        data = {"timeout": {"get_traces": {"weight": 0.20, "calls": 10, "ema_signal": 0.20}}}
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(data, f)

        # Mock adaptive_thresholds to return 0.35
        import supervisor.adaptive_thresholds as at_mod
        monkeypatch.setattr(at_mod, "ADAPTIVE_THRESHOLDS_ENABLED", False)

        assert mod.should_skip_step("timeout", "get_traces") is True

    def test_returns_false_when_weight_above_threshold(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", p)
        data = {"timeout": {"get_traces": {"weight": 0.80, "calls": 10, "ema_signal": 0.80}}}
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(data, f)

        import supervisor.adaptive_thresholds as at_mod
        monkeypatch.setattr(at_mod, "ADAPTIVE_THRESHOLDS_ENABLED", False)

        assert mod.should_skip_step("timeout", "get_traces") is False

    def test_service_specific_weight_takes_precedence(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", p)

        import supervisor.adaptive_thresholds as at_mod
        monkeypatch.setattr(at_mod, "ADAPTIVE_THRESHOLDS_ENABLED", False)

        # Type-level weight is fine, but service-level is very low
        data = {
            "timeout": {"get_traces": {"weight": 0.8, "calls": 10, "ema_signal": 0.8}},
            "_service_weights": {
                "timeout.get_traces.payment-svc": {
                    "weight": 0.15, "calls": 10, "ema_signal": 0.15,
                }
            },
        }
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(data, f)

        assert mod.should_skip_step("timeout", "get_traces", service="payment-svc") is True
        # Different service should not be skipped
        assert mod.should_skip_step("timeout", "get_traces", service="auth-svc") is False


# ---------------------------------------------------------------------------
# record_gap_pattern()
# ---------------------------------------------------------------------------

class TestRecordGapPattern:

    def test_noop_when_disabled(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "STRATEGY_EVOLVER_ENABLED", False)
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", p)
        mod.record_gap_pattern("timeout", "svc", ["golden_signals"])
        assert not os.path.exists(p)

    def test_noop_for_empty_gap_list(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", p)
        mod.record_gap_pattern("timeout", "svc", [])
        assert not os.path.exists(p)

    def test_applies_negative_signal_to_relevant_steps(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", _make_path(tmp_path))
        # Apply gap pattern 20 times — should eventually lower the weight
        for _ in range(20):
            mod.record_gap_pattern("timeout", "svc", ["golden_signals"])
        weights = mod.get_weights("timeout")
        # get_metric_chart or get_golden_signals mapped from "golden_signals"
        matching = [v for k, v in weights.items()
                    if k in ("get_metric_chart", "get_golden_signals")]
        if matching:
            # Weights should have moved toward low end
            assert any(w < 1.0 for w in matching)

    def test_creates_service_weight_entries(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", p)
        mod.record_gap_pattern("timeout", "payment-svc", ["logs"])
        with open(p) as f:
            data = json.load(f)
        svc_weights = data.get("_service_weights", {})
        svc_keys = [k for k in svc_weights if "payment-svc" in k]
        assert len(svc_keys) > 0

    def test_unknown_gap_category_noop(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", p)
        # "unknown_evidence_type" maps to no steps — should not crash
        mod.record_gap_pattern("timeout", "svc", ["unknown_evidence_type"])


# ---------------------------------------------------------------------------
# get_service_weight()
# ---------------------------------------------------------------------------

class TestGetServiceWeight:

    def test_returns_1_when_no_data(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", _make_path(tmp_path))
        assert mod.get_service_weight("timeout", "get_traces", "svc") == 1.0

    def test_returns_1_when_insufficient_calls(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", p)
        data = {"_service_weights": {
            "timeout.get_traces.svc": {"weight": 0.2, "calls": 2, "ema_signal": 0.2}
        }}
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(data, f)
        assert mod.get_service_weight("timeout", "get_traces", "svc") == 1.0

    def test_returns_stored_weight_when_sufficient_calls(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", p)
        data = {"_service_weights": {
            "timeout.get_traces.svc": {"weight": 0.42, "calls": 10, "ema_signal": 0.42}
        }}
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(data, f)
        assert mod.get_service_weight("timeout", "get_traces", "svc") == pytest.approx(0.42)

    def test_returns_1_when_disabled(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        monkeypatch.setattr(mod, "STRATEGY_EVOLVER_ENABLED", False)
        assert mod.get_service_weight("timeout", "step", "svc") == 1.0

    def test_returns_1_for_empty_service(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", _make_path(tmp_path))
        assert mod.get_service_weight("timeout", "step", "") == 1.0


# ---------------------------------------------------------------------------
# record_outcome() — per-service weights written
# ---------------------------------------------------------------------------

class TestRecordOutcomeWithService:

    def _make_receipts(self, action="get_traces"):
        return [{"tool": "metrics_worker", "action": action, "status": "ok"}]

    def test_service_weight_entry_created(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", p)
        receipts = self._make_receipts()
        for _ in range(6):
            mod.record_outcome("timeout", receipts, 0.80, service="payment-svc")
        with open(p) as f:
            data = json.load(f)
        svc_key = "timeout.get_traces.payment-svc"
        assert svc_key in data.get("_service_weights", {})

    def test_service_weight_converges_with_high_scores(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", p)
        receipts = self._make_receipts()
        for _ in range(20):
            mod.record_outcome("timeout", receipts, 0.90, service="payment-svc")
        svc_w = mod.get_service_weight("timeout", "get_traces", "payment-svc")
        # High scores → weight should be > 1.0
        assert svc_w > 1.0

    def test_no_service_skips_service_weight_update(self, tmp_path, monkeypatch):
        import supervisor.strategy_evolver as mod
        p = _make_path(tmp_path)
        monkeypatch.setattr(mod, "EVOLVED_STRATEGY_PATH", p)
        receipts = self._make_receipts()
        mod.record_outcome("timeout", receipts, 0.80, service="")
        with open(p) as f:
            data = json.load(f)
        assert "_service_weights" not in data or data["_service_weights"] == {}
