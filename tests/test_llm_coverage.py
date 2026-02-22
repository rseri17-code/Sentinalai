"""Extended tests for supervisor/llm.py — covering _get_client and edge cases.

Covers:
- _get_client lazy init (cached, boto3 unavailable, creation error)
- converse with temperature and max_tokens overrides
- refine_hypothesis when LLM returns error
- generate_reasoning max_tokens override
"""

import json
import pytest
from unittest.mock import patch, MagicMock

import supervisor.llm as llm_module
from supervisor.llm import (
    converse,
    refine_hypothesis,
    generate_reasoning,
    is_enabled,
    _disabled_response,
)


class TestGetClientPaths:
    """Tests for _get_client lazy initialization."""

    def teardown_method(self):
        llm_module._client = None

    def test_returns_cached_client(self):
        sentinel = MagicMock()
        llm_module._client = sentinel
        result = llm_module._get_client()
        assert result is sentinel

    def test_returns_none_when_boto3_unavailable(self):
        llm_module._client = None
        with patch.object(llm_module, "_BOTO3_AVAILABLE", False):
            result = llm_module._get_client()
        assert result is None

    def test_creates_client_when_boto3_available(self):
        llm_module._client = None
        mock_client = MagicMock()
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client
        # Inject boto3 and BotoConfig into the module namespace since they're
        # not present when the real import fails
        with patch.object(llm_module, "_BOTO3_AVAILABLE", True):
            original_boto3 = getattr(llm_module, "boto3", None)
            original_config = getattr(llm_module, "BotoConfig", None)
            try:
                llm_module.boto3 = mock_boto3
                llm_module.BotoConfig = MagicMock()
                result = llm_module._get_client()
            finally:
                if original_boto3 is None:
                    delattr(llm_module, "boto3") if hasattr(llm_module, "boto3") else None
                else:
                    llm_module.boto3 = original_boto3
                if original_config is None:
                    delattr(llm_module, "BotoConfig") if hasattr(llm_module, "BotoConfig") else None
                else:
                    llm_module.BotoConfig = original_config
        assert result is mock_client

    def test_handles_client_creation_error(self):
        llm_module._client = None
        mock_boto3 = MagicMock()
        mock_boto3.client.side_effect = RuntimeError("AWS config error")
        with patch.object(llm_module, "_BOTO3_AVAILABLE", True):
            original_boto3 = getattr(llm_module, "boto3", None)
            original_config = getattr(llm_module, "BotoConfig", None)
            try:
                llm_module.boto3 = mock_boto3
                llm_module.BotoConfig = MagicMock()
                result = llm_module._get_client()
            finally:
                if original_boto3 is None:
                    delattr(llm_module, "boto3") if hasattr(llm_module, "boto3") else None
                else:
                    llm_module.boto3 = original_boto3
                if original_config is None:
                    delattr(llm_module, "BotoConfig") if hasattr(llm_module, "BotoConfig") else None
                else:
                    llm_module.BotoConfig = original_config
        assert result is None


class TestConverseExtended:
    """Extended converse tests for parameter overrides."""

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_temperature_override(self):
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "stopReason": "end_turn",
        }

        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = converse("sys", "msg", temperature=0.5)

        call_kwargs = mock_client.converse.call_args[1]
        assert call_kwargs["inferenceConfig"]["temperature"] == 0.5

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_max_tokens_override(self):
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "stopReason": "end_turn",
        }

        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = converse("sys", "msg", max_tokens=512)

        call_kwargs = mock_client.converse.call_args[1]
        assert call_kwargs["inferenceConfig"]["maxTokens"] == 512

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_empty_content_list(self):
        """converse handles empty content list in response."""
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": []}},
            "usage": {"inputTokens": 10, "outputTokens": 0},
            "stopReason": "end_turn",
        }

        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = converse("sys", "msg")
        assert result["text"] == ""

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_uses_default_temperature_when_none(self):
        """converse uses LLM_TEMPERATURE when temperature is None."""
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "stopReason": "end_turn",
        }

        with patch.object(llm_module, "LLM_TEMPERATURE", 0.0):
            with patch.object(llm_module, "_get_client", return_value=mock_client):
                converse("sys", "msg")

        call_kwargs = mock_client.converse.call_args[1]
        assert call_kwargs["inferenceConfig"]["temperature"] == 0.0


class TestRefineHypothesisExtended:
    """Extended refine_hypothesis tests."""

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_returns_originals_on_error_response(self):
        """refine_hypothesis returns originals when LLM returns error."""
        mock_client = MagicMock()
        mock_client.converse.side_effect = RuntimeError("LLM unavailable")

        originals = [{"name": "h1", "root_cause": "test", "score": 70, "reasoning": "r"}]
        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = refine_hypothesis("timeout", "svc", "summary", "evidence", originals)

        # Should return originals since converse returned error
        assert result["refined_hypotheses"] == originals

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_returns_originals_on_empty_text(self):
        """refine_hypothesis returns originals when LLM text is empty."""
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": ""}]}},
            "usage": {"inputTokens": 50, "outputTokens": 0},
            "stopReason": "max_tokens",
        }

        originals = [{"name": "h1", "root_cause": "test", "score": 70, "reasoning": "r"}]
        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = refine_hypothesis("timeout", "svc", "summary", "evidence", originals)

        assert result["refined_hypotheses"] == originals


class TestGenerateReasoningExtended:
    """Extended generate_reasoning tests."""

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_uses_512_max_tokens(self):
        """generate_reasoning calls converse with max_tokens=512."""
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "Reasoning..."}]}},
            "usage": {"inputTokens": 100, "outputTokens": 50},
            "stopReason": "end_turn",
        }

        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = generate_reasoning("timeout", "svc", "root", "evidence", "timeline")

        call_kwargs = mock_client.converse.call_args[1]
        assert call_kwargs["inferenceConfig"]["maxTokens"] == 512
        assert result["reasoning"] == "Reasoning..."

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_returns_model_id_in_result(self):
        """generate_reasoning includes model_id in result."""
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "Analysis"}]}},
            "usage": {"inputTokens": 80, "outputTokens": 40},
            "stopReason": "end_turn",
        }

        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = generate_reasoning("timeout", "svc", "root", "evidence", "timeline")

        assert result["model_id"] == "test-model"
        assert result["input_tokens"] == 80
        assert result["output_tokens"] == 40
