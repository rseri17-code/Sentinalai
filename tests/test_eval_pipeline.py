"""Integration tests for the eval scoring pipeline.

Verifies that judge scores, GenAI token metrics, and eval dimensions
actually flow through to OTEL histogram.record() calls with correct
attributes — the same attributes SignalFx/Splunk dashboards query.

These tests mock the OTEL meter so we can assert on actual metric
emission without a running collector.
"""

import json
import pytest
from unittest.mock import patch, MagicMock, call

import supervisor.eval_metrics as eval_metrics_module
import supervisor.llm as llm_module
import supervisor.observability as obs_module
from supervisor.eval_metrics import (
    record_investigation,
    record_judge_scores,
    record_llm_usage,
    record_eval_score,
)
from supervisor.llm_judge import (
    judge_and_record,
    _rule_based_fallback,
)
from supervisor.agent import SentinalAISupervisor
from tests.fixtures.mock_mcp_responses import ALL_MOCKS
from tests.test_supervisor import _build_mock_workers


# =========================================================================
# Fixture: Mock OTEL meter that captures histogram/counter calls
# =========================================================================

class MockHistogram:
    """Captures .record() calls for assertion."""
    def __init__(self, name):
        self.name = name
        self.records = []

    def record(self, value, attributes=None):
        self.records.append({"value": value, "attributes": attributes or {}})


class MockCounter:
    """Captures .add() calls for assertion."""
    def __init__(self, name):
        self.name = name
        self.adds = []

    def add(self, value, attributes=None):
        self.adds.append({"value": value, "attributes": attributes or {}})


class MockMeter:
    """OTEL meter mock that creates mock instruments."""
    def __init__(self):
        self.histograms = {}
        self.counters = {}

    def create_histogram(self, name, **kwargs):
        if name not in self.histograms:
            self.histograms[name] = MockHistogram(name)
        return self.histograms[name]

    def create_counter(self, name, **kwargs):
        if name not in self.counters:
            self.counters[name] = MockCounter(name)
        return self.counters[name]

    def create_up_down_counter(self, name, **kwargs):
        if name not in self.counters:
            self.counters[name] = MockCounter(name)
        return self.counters[name]


@pytest.fixture
def mock_meter():
    """Provide a mock OTEL meter and clean up instrument cache after."""
    meter = MockMeter()
    # Clear the instrument cache so new instruments use our mock meter
    old_instruments = eval_metrics_module._instruments.copy()
    eval_metrics_module._instruments.clear()

    with patch.object(obs_module, "_meter", meter):
        yield meter

    # Restore original instruments
    eval_metrics_module._instruments.clear()
    eval_metrics_module._instruments.update(old_instruments)


# =========================================================================
# Test: record_judge_scores emits correct histograms to OTEL
# =========================================================================

class TestJudgeScoresEmission:
    """Verify judge scores reach OTEL histograms with correct attributes."""

    def test_judge_scores_emit_per_dimension(self, mock_meter):
        scores = {
            "root_cause_accuracy": 0.9,
            "causal_reasoning": 0.8,
            "evidence_usage": 0.7,
            "timeline_quality": 0.85,
            "actionability": 0.75,
            "overall": 0.82,
        }
        record_judge_scores(
            incident_id="INC001",
            incident_type="timeout",
            scores=scores,
            source="llm_judge",
        )

        # Verify sentinalai.judge.score histogram was created
        assert "sentinalai.judge.score" in mock_meter.histograms

        h = mock_meter.histograms["sentinalai.judge.score"]
        assert len(h.records) == 6  # one per dimension

        # Check each dimension is recorded with correct attributes
        recorded_dims = {r["attributes"]["judge_dimension"] for r in h.records}
        assert recorded_dims == {"root_cause_accuracy", "causal_reasoning",
                                 "evidence_usage", "timeline_quality",
                                 "actionability", "overall"}

        # Verify incident_type attribute is set on every record
        for rec in h.records:
            assert rec["attributes"]["incident_type"] == "timeout"
            assert rec["attributes"]["judge_source"] == "llm_judge"

        # Verify score values match
        for rec in h.records:
            dim = rec["attributes"]["judge_dimension"]
            assert rec["value"] == scores[dim]

    def test_judge_overall_emits_separately(self, mock_meter):
        record_judge_scores(
            incident_id="INC001",
            incident_type="error_spike",
            scores={"overall": 0.75, "root_cause_accuracy": 0.8},
            source="rule_based",
        )

        # Verify sentinalai.judge.overall histogram
        assert "sentinalai.judge.overall" in mock_meter.histograms
        h = mock_meter.histograms["sentinalai.judge.overall"]
        assert len(h.records) == 1
        assert h.records[0]["value"] == 0.75
        assert h.records[0]["attributes"]["incident_type"] == "error_spike"
        assert h.records[0]["attributes"]["judge_source"] == "rule_based"

    def test_rule_based_source_attribute(self, mock_meter):
        record_judge_scores(
            incident_id="INC002",
            incident_type="latency_spike",
            scores={"overall": 0.5},
            source="rule_based",
        )
        h = mock_meter.histograms["sentinalai.judge.score"]
        assert h.records[0]["attributes"]["judge_source"] == "rule_based"


# =========================================================================
# Test: record_eval_score emits to OTEL
# =========================================================================

class TestEvalScoreEmission:
    """Verify individual eval scores reach OTEL."""

    def test_eval_score_emits_histogram(self, mock_meter):
        record_eval_score(
            incident_id="INC001",
            incident_type="timeout",
            dimension="llm_judge.root_cause_accuracy",
            score=0.92,
        )

        assert "sentinalai.eval.score" in mock_meter.histograms
        h = mock_meter.histograms["sentinalai.eval.score"]
        assert len(h.records) == 1
        assert h.records[0]["value"] == 0.92
        assert h.records[0]["attributes"]["incident_type"] == "timeout"
        assert h.records[0]["attributes"]["eval_dimension"] == "llm_judge.root_cause_accuracy"


# =========================================================================
# Test: GenAI token metrics emit correctly
# =========================================================================

class TestGenAITokenMetrics:
    """Verify LLM token usage metrics follow GenAI semantic conventions."""

    def test_llm_usage_emits_token_histogram(self, mock_meter):
        record_llm_usage(
            operation="refine_hypothesis",
            model_id="anthropic.claude-sonnet-4-5-20250929-v1:0",
            input_tokens=250,
            output_tokens=120,
            latency_ms=1500.0,
            incident_type="timeout",
        )

        # gen_ai.client.token.usage should have 2 records (input + output)
        assert "gen_ai.client.token.usage" in mock_meter.histograms
        h = mock_meter.histograms["gen_ai.client.token.usage"]
        assert len(h.records) == 2

        input_rec = [r for r in h.records if r["attributes"]["gen_ai.token.type"] == "input"]
        output_rec = [r for r in h.records if r["attributes"]["gen_ai.token.type"] == "output"]
        assert len(input_rec) == 1
        assert len(output_rec) == 1
        assert input_rec[0]["value"] == 250
        assert output_rec[0]["value"] == 120

        # Verify GenAI semantic convention attributes
        for rec in h.records:
            assert rec["attributes"]["gen_ai.system"] == "aws.bedrock"
            assert rec["attributes"]["gen_ai.request.model"] == "anthropic.claude-sonnet-4-5-20250929-v1:0"
            assert rec["attributes"]["gen_ai.operation.name"] == "refine_hypothesis"

    def test_llm_usage_emits_duration_histogram(self, mock_meter):
        record_llm_usage(
            operation="generate_reasoning",
            model_id="test-model",
            input_tokens=100,
            output_tokens=50,
            latency_ms=800.0,
        )

        assert "gen_ai.client.operation.duration" in mock_meter.histograms
        h = mock_meter.histograms["gen_ai.client.operation.duration"]
        assert len(h.records) == 1
        assert h.records[0]["value"] == pytest.approx(0.8, abs=0.01)  # ms -> seconds

    def test_llm_usage_emits_call_counter(self, mock_meter):
        record_llm_usage(
            operation="judge",
            model_id="anthropic.claude-haiku-4-5-20251001-v1:0",
            input_tokens=100,
            output_tokens=50,
            latency_ms=500.0,
            incident_type="error_spike",
        )

        assert "sentinalai.llm.calls.total" in mock_meter.counters
        c = mock_meter.counters["sentinalai.llm.calls.total"]
        assert len(c.adds) == 1
        assert c.adds[0]["value"] == 1
        assert c.adds[0]["attributes"]["gen_ai.operation.name"] == "judge"

    def test_llm_usage_emits_token_counter(self, mock_meter):
        record_llm_usage(
            operation="refine_hypothesis",
            model_id="test-model",
            input_tokens=300,
            output_tokens=150,
            latency_ms=1000.0,
        )

        assert "sentinalai.llm.tokens.total" in mock_meter.counters
        c = mock_meter.counters["sentinalai.llm.tokens.total"]
        assert len(c.adds) == 2  # input + output
        input_adds = [a for a in c.adds if a["attributes"]["gen_ai.token.type"] == "input"]
        output_adds = [a for a in c.adds if a["attributes"]["gen_ai.token.type"] == "output"]
        assert input_adds[0]["value"] == 300
        assert output_adds[0]["value"] == 150


# =========================================================================
# Test: judge_and_record() -> record_eval_score() integration
# =========================================================================

class TestJudgeToMetricsPipeline:
    """Verify the judge_and_record function actually calls record_eval_score
    and record_llm_usage, not just returns scores."""

    def test_rule_based_judge_emits_eval_scores(self, mock_meter):
        """When LLM is disabled, rule-based scores should still emit to OTEL."""
        with patch.object(llm_module, "LLM_ENABLED", False):
            result = judge_and_record(
                incident_id="INC001",
                incident_type="timeout",
                expected={"root_cause_keywords": ["timeout"], "confidence_min": 50, "confidence_max": 90},
                result={
                    "root_cause": "timeout in database connection pool",
                    "reasoning": "The database timeout caused cascading failures due to pool exhaustion",
                    "confidence": 75,
                    "evidence_timeline": [{"ts": "1"}, {"ts": "2"}, {"ts": "3"}],
                },
            )

        # Verify scores came back
        assert result["source"] == "rule_based"
        assert "overall" in result["scores"]

        # Verify eval scores were emitted to OTEL histogram
        assert "sentinalai.eval.score" in mock_meter.histograms
        h = mock_meter.histograms["sentinalai.eval.score"]
        assert len(h.records) >= 6  # at least 6 dimensions

        # Verify dimension names include source prefix
        dims = {r["attributes"]["eval_dimension"] for r in h.records}
        assert any("rule_based.root_cause_accuracy" in d for d in dims)
        assert any("rule_based.overall" in d for d in dims)

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_llm_judge_emits_token_metrics(self, mock_meter):
        """When LLM judge runs, token usage must be emitted to OTEL."""
        judge_response = json.dumps({
            "root_cause_accuracy": {"score": 0.9, "reason": "good"},
            "causal_reasoning": {"score": 0.8, "reason": "clear"},
            "evidence_usage": {"score": 0.7, "reason": "ok"},
            "timeline_quality": {"score": 0.85, "reason": "ordered"},
            "actionability": {"score": 0.75, "reason": "clear next steps"},
            "overall": {"score": 0.82, "reason": "solid"},
        })
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": judge_response}]}},
            "usage": {"inputTokens": 400, "outputTokens": 180},
            "stopReason": "end_turn",
        }

        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = judge_and_record(
                incident_id="INC001",
                incident_type="timeout",
                expected={},
                result={"root_cause": "test", "reasoning": "test", "evidence_timeline": []},
            )

        assert result["source"] == "llm_judge"

        # Verify token usage was emitted
        assert "gen_ai.client.token.usage" in mock_meter.histograms
        token_h = mock_meter.histograms["gen_ai.client.token.usage"]
        assert len(token_h.records) == 2

        # Verify operation is "judge"
        for rec in token_h.records:
            assert rec["attributes"]["gen_ai.operation.name"] == "judge"

        # Verify eval scores were emitted with llm_judge prefix
        assert "sentinalai.eval.score" in mock_meter.histograms
        eval_h = mock_meter.histograms["sentinalai.eval.score"]
        dims = {r["attributes"]["eval_dimension"] for r in eval_h.records}
        assert any("llm_judge.root_cause_accuracy" in d for d in dims)


# =========================================================================
# Test: Full investigate() -> judge -> OTEL pipeline
# =========================================================================

class TestFullInvestigationPipeline:
    """End-to-end: investigate() should emit judge scores + GenAI metrics."""

    def test_investigation_emits_judge_scores(self, mock_meter):
        """Full investigation with rule-based judge should emit eval metrics."""
        sup = SentinalAISupervisor()
        _build_mock_workers(sup, "INC12345")

        with patch.object(llm_module, "LLM_ENABLED", False):
            result = sup.investigate("INC12345")

        # Investigation succeeded
        assert "root_cause" in result
        assert result["confidence"] > 0

        # Judge scores should have been emitted
        assert "sentinalai.eval.score" in mock_meter.histograms
        eval_h = mock_meter.histograms["sentinalai.eval.score"]
        assert len(eval_h.records) >= 6  # At least 6 dimensions from rule-based

        # Judge overall score should be emitted
        assert "sentinalai.judge.score" in mock_meter.histograms
        judge_h = mock_meter.histograms["sentinalai.judge.score"]
        assert len(judge_h.records) >= 1

        # Verify attributes match SignalFx query patterns
        for rec in judge_h.records:
            assert "incident_type" in rec["attributes"]
            assert "judge_dimension" in rec["attributes"]
            assert "judge_source" in rec["attributes"]
            assert rec["attributes"]["judge_source"] == "rule_based"

    def test_investigation_emits_core_metrics(self, mock_meter):
        """Core investigation metrics (confidence, duration) must emit."""
        sup = SentinalAISupervisor()
        _build_mock_workers(sup, "INC12345")

        with patch.object(llm_module, "LLM_ENABLED", False):
            result = sup.investigate("INC12345")

        # Core investigation metrics should exist
        assert "sentinalai.confidence.distribution" in mock_meter.histograms
        conf_h = mock_meter.histograms["sentinalai.confidence.distribution"]
        assert len(conf_h.records) >= 1
        assert conf_h.records[0]["value"] == result["confidence"]

    def test_multiple_incidents_emit_distinct_metrics(self, mock_meter):
        """Each incident investigation should emit its own set of metrics."""
        sup = SentinalAISupervisor()

        with patch.object(llm_module, "LLM_ENABLED", False):
            _build_mock_workers(sup, "INC12345")
            sup.investigate("INC12345")
            _build_mock_workers(sup, "INC12346")
            sup.investigate("INC12346")

        # Should have judge scores from both investigations
        assert "sentinalai.eval.score" in mock_meter.histograms
        eval_h = mock_meter.histograms["sentinalai.eval.score"]
        assert len(eval_h.records) >= 12  # 6+ per investigation

    def test_score_values_bounded(self, mock_meter):
        """All emitted judge scores must be between 0.0 and 1.0."""
        sup = SentinalAISupervisor()
        _build_mock_workers(sup, "INC12345")

        with patch.object(llm_module, "LLM_ENABLED", False):
            sup.investigate("INC12345")

        if "sentinalai.judge.score" in mock_meter.histograms:
            for rec in mock_meter.histograms["sentinalai.judge.score"].records:
                assert 0 <= rec["value"] <= 1.0, (
                    f"Score out of bounds: {rec['attributes']['judge_dimension']}={rec['value']}"
                )

        if "sentinalai.eval.score" in mock_meter.histograms:
            for rec in mock_meter.histograms["sentinalai.eval.score"].records:
                assert 0 <= rec["value"] <= 1.0, (
                    f"Eval score out of bounds: {rec['attributes']['eval_dimension']}={rec['value']}"
                )
