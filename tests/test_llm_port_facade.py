"""Slice 1 exit criterion: investigate() runs with null and bedrock ports.

Does not change SRE-domain code. Uses the existing INC12345 mock workers.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import supervisor.llm as llm_module
from supervisor.agent import SentinalAISupervisor
from tests.fixtures.expected_rca_outputs import EXPECTED_RCA
from tests.test_supervisor import _build_mock_workers


def _run_inc12345() -> dict:
    sup = SentinalAISupervisor()
    _build_mock_workers(sup, "INC12345")
    return sup.investigate("INC12345")


def _assert_inc12345_keywords(result: dict) -> None:
    expected = EXPECTED_RCA["INC12345"]
    root = result.get("root_cause", "").lower()
    for kw in expected["root_cause_keywords"]:
        assert kw.lower() in root, f"missing keyword {kw!r} in {root!r}"
    assert expected["confidence_min"] <= result["confidence"] <= expected["confidence_max"]


class TestInvestigateViaInferencePort:
    @patch.object(llm_module, "LLM_PROVIDER", "null")
    @patch.object(llm_module, "LLM_ENABLED", False)
    def test_investigate_with_llm_off(self):
        _assert_inc12345_keywords(_run_inc12345())

    @patch.object(llm_module, "LLM_PROVIDER", "null")
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_investigate_with_null_provider(self):
        _assert_inc12345_keywords(_run_inc12345())

    @patch.object(llm_module, "LLM_PROVIDER", "bedrock")
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_investigate_with_bedrock_port_mocked(self):
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": ""}]}},
            "usage": {"inputTokens": 1, "outputTokens": 0},
            "stopReason": "end_turn",
        }
        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = _run_inc12345()
        assert "root_cause" in result
        assert "confidence" in result
        # Empty LLM overlay fail-opens to deterministic RCA.
        _assert_inc12345_keywords(result)
        assert mock_client.converse.called

    @patch.object(llm_module, "LLM_PROVIDER", "openai")
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "_OPENAI_AVAILABLE", True)
    @patch.object(llm_module, "MODEL_ID", "gpt-4o")
    def test_investigate_with_openai_port_mocked(self):
        mock_choice = MagicMock()
        mock_choice.message.content = ""
        mock_choice.finish_reason = "stop"
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage = MagicMock(prompt_tokens=1, completion_tokens=0)
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_resp
        with patch.object(llm_module, "_openai_client", return_value=mock_client):
            result = _run_inc12345()
        assert "root_cause" in result
        assert "confidence" in result
        _assert_inc12345_keywords(result)
        assert mock_client.chat.completions.create.called
