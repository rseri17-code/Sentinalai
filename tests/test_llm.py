"""Tests for the Bedrock Converse LLM client and GenAI eval metrics."""

import json
import pytest
from unittest.mock import patch, MagicMock

import supervisor.llm as llm_module
from supervisor.llm import (
    converse,
    refine_hypothesis,
    generate_reasoning,
    is_enabled,
    dispose,
    _disabled_response,
)
from supervisor.eval_metrics import (
    record_llm_usage,
    record_judge_scores,
    record_eval_score,
)

try:
    from botocore.exceptions import ClientError
    _HAS_BOTOCORE = True
except ImportError:
    _HAS_BOTOCORE = False


class TestLLMEnabled:
    """Tests for the is_enabled() check."""

    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    def test_enabled_when_all_set(self):
        assert is_enabled() is True

    @patch.object(llm_module, "LLM_ENABLED", False)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_disabled_when_flag_false(self):
        assert is_enabled() is False

    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "")
    def test_disabled_when_no_model(self):
        assert is_enabled() is False

    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test")
    @patch.object(llm_module, "_BOTO3_AVAILABLE", False)
    def test_disabled_when_no_boto3(self):
        assert is_enabled() is False


class TestConverse:
    """Tests for the core converse() function."""

    @patch.object(llm_module, "LLM_ENABLED", False)
    def test_returns_disabled_response_when_off(self):
        result = converse("system", "user")
        assert result["stop_reason"] == "disabled"
        assert result["text"] == ""
        assert result["input_tokens"] == 0

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_successful_converse_call(self):
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": "The root cause is X"}],
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50},
            "stopReason": "end_turn",
        }

        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = converse("system prompt", "user message")

        assert result["text"] == "The root cause is X"
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50
        assert result["stop_reason"] == "end_turn"
        assert result["model_id"] == "test-model"
        assert result["latency_ms"] >= 0

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_model_override(self):
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "stopReason": "end_turn",
        }

        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = converse("sys", "msg", model_id="custom-model")

        call_args = mock_client.converse.call_args
        assert call_args.kwargs["modelId"] == "custom-model"
        assert result["model_id"] == "custom-model"

    @pytest.mark.skipif(not _HAS_BOTOCORE, reason="botocore not installed")
    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_handles_client_error(self):
        mock_client = MagicMock()
        mock_client.converse.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "Converse",
        )

        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = converse("sys", "msg")

        assert "error" in result
        assert "ThrottlingException" in result["error"]
        assert result["text"] == ""

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_handles_generic_exception(self):
        mock_client = MagicMock()
        mock_client.converse.side_effect = RuntimeError("Network failure")

        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = converse("sys", "msg")

        assert "error" in result
        assert "Network failure" in result["error"]

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_returns_disabled_when_client_none(self):
        with patch.object(llm_module, "_get_client", return_value=None):
            result = converse("sys", "msg")
        assert result["stop_reason"] == "disabled"


class TestRefineHypothesis:
    """Tests for the refine_hypothesis function."""

    @patch.object(llm_module, "LLM_ENABLED", False)
    def test_returns_original_when_disabled(self):
        hypotheses = [{"name": "h1", "root_cause": "test", "score": 80, "reasoning": "test"}]
        result = refine_hypothesis("timeout", "svc", "summary", "evidence", hypotheses)
        assert result["refined_hypotheses"] == hypotheses
        assert result["input_tokens"] == 0

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_refines_hypotheses_with_llm(self):
        refined_json = json.dumps({
            "hypotheses": [
                {"name": "h1", "root_cause": "refined cause", "score": 90, "reasoning": "refined"},
            ]
        })
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": refined_json}]}},
            "usage": {"inputTokens": 200, "outputTokens": 100},
            "stopReason": "end_turn",
        }

        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = refine_hypothesis("timeout", "api-gw", "summary", "evidence", [
                {"name": "h1", "root_cause": "orig", "score": 70, "reasoning": "orig"},
            ])

        assert result["refined_hypotheses"][0]["score"] == 90
        assert result["input_tokens"] == 200
        assert result["output_tokens"] == 100

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_returns_originals_on_parse_error(self):
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "not valid json"}]}},
            "usage": {"inputTokens": 50, "outputTokens": 20},
            "stopReason": "end_turn",
        }

        originals = [{"name": "h1", "root_cause": "orig", "score": 70, "reasoning": "orig"}]
        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = refine_hypothesis("timeout", "svc", "summary", "evidence", originals)

        assert result["refined_hypotheses"] == originals


class TestGenerateReasoning:
    """Tests for the generate_reasoning function."""

    @patch.object(llm_module, "LLM_ENABLED", False)
    def test_returns_empty_when_disabled(self):
        result = generate_reasoning("timeout", "svc", "root_cause", "evidence", "timeline")
        assert result["reasoning"] == ""
        assert result["input_tokens"] == 0

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_generates_reasoning_with_llm(self):
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "The incident was caused by a database failure."}]}},
            "usage": {"inputTokens": 150, "outputTokens": 30},
            "stopReason": "end_turn",
        }

        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = generate_reasoning("timeout", "svc", "db failure", "evidence", "timeline")

        assert "database failure" in result["reasoning"]
        assert result["input_tokens"] == 150


class TestDispose:
    """Tests for client cleanup."""

    def test_dispose_resets_client(self):
        llm_module._client = "something"
        dispose()
        assert llm_module._client is None


class TestDisabledResponse:
    """Tests for the _disabled_response helper."""

    def test_disabled_response_structure(self):
        result = _disabled_response()
        assert result["text"] == ""
        assert result["input_tokens"] == 0
        assert result["output_tokens"] == 0
        assert result["stop_reason"] == "disabled"


class TestGenAIEvalMetrics:
    """Tests for GenAI semantic convention metrics functions."""

    def test_record_llm_usage_does_not_raise(self):
        """record_llm_usage should be a no-op when OTEL meter is None."""
        record_llm_usage(
            operation="refine_hypothesis",
            model_id="test-model",
            input_tokens=100,
            output_tokens=50,
            latency_ms=500.0,
            incident_type="timeout",
        )

    def test_record_judge_scores_does_not_raise(self):
        """record_judge_scores should be a no-op when OTEL meter is None."""
        record_judge_scores(
            incident_id="INC001",
            incident_type="timeout",
            scores={"root_cause_accuracy": 0.8, "overall": 0.75},
            source="rule_based",
        )

    def test_record_eval_score_does_not_raise(self):
        """record_eval_score should be a no-op when OTEL meter is None."""
        record_eval_score(
            incident_id="INC001",
            incident_type="timeout",
            dimension="root_cause_accuracy",
            score=0.85,
        )

    def test_record_llm_usage_with_empty_values(self):
        """record_llm_usage handles zero/empty inputs gracefully."""
        record_llm_usage(
            operation="judge",
            model_id="",
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
        )

    def test_record_judge_scores_with_empty_scores(self):
        """record_judge_scores handles empty scores dict."""
        record_judge_scores(
            incident_id="INC001",
            incident_type="timeout",
            scores={},
        )
