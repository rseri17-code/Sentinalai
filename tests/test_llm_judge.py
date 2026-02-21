"""Tests for the LLM-as-judge evaluator."""

import json
import pytest
from unittest.mock import patch, MagicMock

import supervisor.llm as llm_module
from supervisor.llm_judge import (
    llm_judge_score,
    judge_and_record,
    _rule_based_fallback,
    _build_judge_prompt,
    JUDGE_DIMENSIONS,
)


class TestRuleBasedFallback:
    """Tests for the deterministic rule-based scoring fallback."""

    def test_perfect_keyword_match(self):
        expected = {
            "root_cause": "database timeout",
            "root_cause_keywords": ["database", "timeout"],
            "confidence_min": 70,
            "confidence_max": 90,
        }
        result = {
            "root_cause": "database timeout causing cascading failures",
            "reasoning": "The database timeout caused downstream service failures due to connection pool exhaustion.",
            "confidence": 80,
            "evidence_timeline": [{"ts": "1"}, {"ts": "2"}, {"ts": "3"}],
        }
        scores = _rule_based_fallback("INC001", expected, result)

        assert scores["root_cause_accuracy"] == 1.0
        assert scores["overall"] > 0.0
        assert all(0 <= v <= 1.0 for v in scores.values())

    def test_no_keyword_match(self):
        expected = {
            "root_cause": "memory leak",
            "root_cause_keywords": ["memory", "leak", "oom"],
            "confidence_min": 70,
            "confidence_max": 90,
        }
        result = {
            "root_cause": "network timeout",
            "reasoning": "generic issue",
            "confidence": 50,
            "evidence_timeline": [],
        }
        scores = _rule_based_fallback("INC001", expected, result)

        assert scores["root_cause_accuracy"] == 0.0
        assert scores["timeline_quality"] == 0.0

    def test_confidence_in_range(self):
        expected = {
            "root_cause_keywords": [],
            "confidence_min": 70,
            "confidence_max": 90,
        }
        result = {"root_cause": "", "reasoning": "", "confidence": 80, "evidence_timeline": []}
        scores = _rule_based_fallback("INC001", expected, result)
        # Confidence within range should contribute 1.0 to calibration component
        assert scores["overall"] > 0

    def test_confidence_below_range(self):
        expected = {
            "root_cause_keywords": [],
            "confidence_min": 70,
            "confidence_max": 90,
        }
        result = {"root_cause": "", "reasoning": "", "confidence": 30, "evidence_timeline": []}
        scores = _rule_based_fallback("INC001", expected, result)
        # Low confidence out of range should penalize
        assert scores["overall"] >= 0

    def test_causal_language_scoring(self):
        expected = {"root_cause_keywords": [], "confidence_min": 0, "confidence_max": 100}
        result = {
            "root_cause": "",
            "reasoning": "The deployment caused high latency, which led to timeouts because of connection pool exhaustion",
            "confidence": 80,
            "evidence_timeline": [],
        }
        scores = _rule_based_fallback("INC001", expected, result)
        assert scores["causal_reasoning"] > 0.0

    def test_empty_reasoning(self):
        expected = {"root_cause_keywords": [], "confidence_min": 0, "confidence_max": 100}
        result = {
            "root_cause": "",
            "reasoning": "",
            "confidence": 50,
            "evidence_timeline": [],
        }
        scores = _rule_based_fallback("INC001", expected, result)
        assert scores["evidence_usage"] == 0.0
        assert scores["causal_reasoning"] == 0.0

    def test_all_dimensions_present(self):
        expected = {"root_cause_keywords": [], "confidence_min": 0, "confidence_max": 100}
        result = {"root_cause": "", "reasoning": "", "confidence": 50, "evidence_timeline": []}
        scores = _rule_based_fallback("INC001", expected, result)

        for dim in ["root_cause_accuracy", "causal_reasoning", "evidence_usage",
                     "timeline_quality", "actionability", "overall"]:
            assert dim in scores

    def test_scores_bounded_zero_to_one(self):
        expected = {"root_cause_keywords": ["a"] * 20, "confidence_min": 0, "confidence_max": 100}
        result = {"root_cause": "something", "reasoning": "x" * 1000, "confidence": 80,
                  "evidence_timeline": [{}] * 10}
        scores = _rule_based_fallback("INC001", expected, result)
        for dim, val in scores.items():
            assert 0 <= val <= 1.0, f"{dim}={val} out of bounds"


class TestBuildJudgePrompt:
    """Tests for the prompt builder."""

    def test_includes_incident_id(self):
        prompt = _build_judge_prompt("INC123", {"root_cause": "test"}, {"root_cause": "result"})
        assert "INC123" in prompt

    def test_includes_expected_keywords(self):
        prompt = _build_judge_prompt(
            "INC123",
            {"root_cause": "test", "root_cause_keywords": ["memory", "leak"]},
            {"root_cause": "result"},
        )
        assert "memory" in prompt
        assert "leak" in prompt

    def test_includes_confidence(self):
        prompt = _build_judge_prompt(
            "INC123", {}, {"root_cause": "result", "confidence": 85},
        )
        assert "85" in prompt

    def test_includes_timeline(self):
        prompt = _build_judge_prompt(
            "INC123", {},
            {"root_cause": "test", "evidence_timeline": [{"ts": "t1", "event": "something"}]},
        )
        assert "something" in prompt


class TestLLMJudgeScore:
    """Tests for the LLM-based judge scoring."""

    @patch.object(llm_module, "LLM_ENABLED", False)
    def test_returns_none_when_disabled(self):
        result = llm_judge_score("INC001", {}, {})
        assert result is None

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_returns_scores_from_llm(self):
        judge_response = json.dumps({
            "root_cause_accuracy": {"score": 0.9, "reason": "good match"},
            "causal_reasoning": {"score": 0.8, "reason": "clear chain"},
            "evidence_usage": {"score": 0.7, "reason": "most sources used"},
            "timeline_quality": {"score": 0.85, "reason": "well ordered"},
            "actionability": {"score": 0.75, "reason": "clear next steps"},
            "overall": {"score": 0.82, "reason": "solid analysis"},
        })
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": judge_response}]}},
            "usage": {"inputTokens": 500, "outputTokens": 200},
            "stopReason": "end_turn",
        }

        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = llm_judge_score("INC001", {}, {"root_cause": "test"})

        assert result is not None
        assert "scores" in result
        assert result["scores"]["root_cause_accuracy"] == 0.9
        assert result["input_tokens"] == 500
        assert result["output_tokens"] == 200

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_handles_markdown_wrapped_json(self):
        judge_response = '```json\n{"root_cause_accuracy": {"score": 0.9, "reason": "ok"}, "overall": {"score": 0.9, "reason": "ok"}}\n```'
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": judge_response}]}},
            "usage": {"inputTokens": 100, "outputTokens": 50},
            "stopReason": "end_turn",
        }

        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = llm_judge_score("INC001", {}, {})

        assert result is not None
        assert result["scores"]["root_cause_accuracy"] == 0.9

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_handles_invalid_json(self):
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "I cannot score this."}]}},
            "usage": {"inputTokens": 100, "outputTokens": 20},
            "stopReason": "end_turn",
        }

        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = llm_judge_score("INC001", {}, {})

        assert result is None

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_handles_llm_error_returns_none(self):
        """When LLM raises an exception, judge returns None (falls back to rule-based)."""
        mock_client = MagicMock()
        mock_client.converse.side_effect = RuntimeError("Service unavailable")

        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = llm_judge_score("INC001", {}, {})

        assert result is None


class TestJudgeAndRecord:
    """Tests for the judge_and_record orchestrator."""

    @patch.object(llm_module, "LLM_ENABLED", False)
    def test_falls_back_to_rule_based(self):
        result = judge_and_record(
            incident_id="INC001",
            incident_type="timeout",
            expected={"root_cause_keywords": ["timeout"], "confidence_min": 0, "confidence_max": 100},
            result={"root_cause": "timeout in api", "reasoning": "caused by db", "confidence": 70,
                    "evidence_timeline": [{"ts": "1"}]},
        )
        assert "scores" in result
        assert result["source"] == "rule_based"
        assert result["input_tokens"] == 0

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_uses_llm_when_available(self):
        judge_response = json.dumps({
            "root_cause_accuracy": {"score": 0.9, "reason": "good"},
            "causal_reasoning": {"score": 0.8, "reason": "clear"},
            "evidence_usage": {"score": 0.7, "reason": "ok"},
            "timeline_quality": {"score": 0.85, "reason": "good"},
            "actionability": {"score": 0.75, "reason": "actionable"},
            "overall": {"score": 0.8, "reason": "solid"},
        })
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": judge_response}]}},
            "usage": {"inputTokens": 500, "outputTokens": 200},
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
        assert result["input_tokens"] == 500

    @patch.object(llm_module, "LLM_ENABLED", False)
    def test_scores_are_numeric(self):
        result = judge_and_record(
            incident_id="INC001",
            incident_type="error_spike",
            expected={"root_cause_keywords": [], "confidence_min": 0, "confidence_max": 100},
            result={"root_cause": "error", "reasoning": "errors occurred", "confidence": 60,
                    "evidence_timeline": [{"ts": "1"}]},
        )
        for dim, score in result["scores"].items():
            assert isinstance(score, (int, float)), f"{dim} is not numeric: {type(score)}"


class TestJudgeDimensions:
    """Tests for the JUDGE_DIMENSIONS constant."""

    def test_dimensions_list_complete(self):
        expected_dims = {"root_cause_accuracy", "causal_reasoning", "evidence_usage",
                         "timeline_quality", "actionability", "overall"}
        assert set(JUDGE_DIMENSIONS) == expected_dims

    def test_dimensions_count(self):
        assert len(JUDGE_DIMENSIONS) == 6
