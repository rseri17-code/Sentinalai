"""Tests for Phase 3 inference contracts, helpers, and backward compatibility.

Covers:
  - InferenceRequest / InferenceResponse / InferenceUsage / StructuredResult
  - InferenceError enum
  - InferencePort protocol
  - parse_llm_json helper (valid, malformed, fenced, required fields)
  - NullInference adapter
  - converse_typed() wrapper
  - Backward compatibility: converse() dict shape unchanged
  - refine_hypothesis() backward compatibility (migrated call site)
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, patch

import supervisor.llm as llm_module
from supervisor.llm import converse, converse_typed, refine_hypothesis
from supervisor.inference_helpers import NullInference, parse_llm_json
from sentinel_core.models.inference import (
    InferenceError,
    InferencePort,
    InferenceRequest,
    InferenceResponse,
    InferenceUsage,
    StructuredResult,
)


# ---------------------------------------------------------------------------
# InferenceUsage
# ---------------------------------------------------------------------------

class TestInferenceUsage:
    def test_defaults_zero(self):
        u = InferenceUsage()
        assert u.input_tokens == 0
        assert u.output_tokens == 0

    def test_total_tokens(self):
        u = InferenceUsage(input_tokens=100, output_tokens=50)
        assert u.total_tokens == 150

    def test_total_tokens_zero(self):
        assert InferenceUsage().total_tokens == 0


# ---------------------------------------------------------------------------
# InferenceRequest
# ---------------------------------------------------------------------------

class TestInferenceRequest:
    def test_required_fields(self):
        req = InferenceRequest(system_prompt="sys", user_message="user")
        assert req.system_prompt == "sys"
        assert req.user_message == "user"

    def test_optional_fields_default_to_none(self):
        req = InferenceRequest(system_prompt="s", user_message="u")
        assert req.model_id is None
        assert req.temperature is None
        assert req.max_tokens is None

    def test_all_fields_set(self):
        req = InferenceRequest(
            system_prompt="sys",
            user_message="user",
            model_id="my-model",
            temperature=0.5,
            max_tokens=1024,
        )
        assert req.model_id == "my-model"
        assert req.temperature == 0.5
        assert req.max_tokens == 1024


# ---------------------------------------------------------------------------
# InferenceResponse
# ---------------------------------------------------------------------------

class TestInferenceResponse:
    def _make(self, **kw):
        defaults = dict(text="hello", model_id="m", stop_reason="end_turn")
        defaults.update(kw)
        return InferenceResponse(**defaults)

    def test_ok_true_when_text_and_no_error(self):
        assert self._make(text="some text", error=None).ok is True

    def test_ok_false_when_empty_text(self):
        assert self._make(text="").ok is False

    def test_ok_false_when_error_set(self):
        assert self._make(text="text", error="bedrock_error: x").ok is False

    def test_to_dict_has_all_required_keys(self):
        r = self._make()
        d = r.to_dict()
        for key in ("text", "input_tokens", "output_tokens", "model_id", "latency_ms", "stop_reason"):
            assert key in d, f"missing key: {key}"

    def test_to_dict_no_error_key_when_none(self):
        d = self._make(error=None).to_dict()
        assert "error" not in d

    def test_to_dict_includes_error_when_set(self):
        d = self._make(error="rate_limited").to_dict()
        assert d["error"] == "rate_limited"

    def test_to_dict_token_values(self):
        r = InferenceResponse(
            text="x", model_id="m", stop_reason="end_turn",
            usage=InferenceUsage(input_tokens=42, output_tokens=7),
        )
        d = r.to_dict()
        assert d["input_tokens"] == 42
        assert d["output_tokens"] == 7

    def test_from_dict_round_trip(self):
        original = {
            "text": "response text",
            "input_tokens": 100,
            "output_tokens": 50,
            "model_id": "claude-test",
            "latency_ms": 123.4,
            "stop_reason": "end_turn",
        }
        r = InferenceResponse.from_dict(original)
        assert r.to_dict() == original

    def test_from_dict_with_error(self):
        d = {
            "text": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "model_id": "m",
            "latency_ms": 5.0,
            "stop_reason": "error",
            "error": "bedrock_error: ThrottlingException",
        }
        r = InferenceResponse.from_dict(d)
        assert r.error == "bedrock_error: ThrottlingException"
        assert r.ok is False
        assert r.to_dict()["error"] == "bedrock_error: ThrottlingException"

    def test_from_dict_disabled_response(self):
        d = {
            "text": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "model_id": "test-model",
            "latency_ms": 0,
            "stop_reason": "disabled",
        }
        r = InferenceResponse.from_dict(d)
        assert r.stop_reason == "disabled"
        assert r.ok is False

    def test_from_dict_missing_keys_uses_defaults(self):
        r = InferenceResponse.from_dict({})
        assert r.text == ""
        assert r.model_id == ""
        assert r.stop_reason == "unknown"
        assert r.usage.total_tokens == 0


# ---------------------------------------------------------------------------
# InferenceError
# ---------------------------------------------------------------------------

class TestInferenceError:
    def test_all_error_values_are_strings(self):
        for e in InferenceError:
            assert isinstance(e.value, str)

    def test_rate_limited(self):
        assert InferenceError.RATE_LIMITED == "rate_limited"

    def test_bedrock_error(self):
        assert InferenceError.BEDROCK_ERROR == "bedrock_error"

    def test_disabled(self):
        assert InferenceError.DISABLED == "disabled"


# ---------------------------------------------------------------------------
# StructuredResult
# ---------------------------------------------------------------------------

class TestStructuredResult:
    def test_ok_true(self):
        r = StructuredResult(ok=True, raw='{"k": 1}', data={"k": 1})
        assert r.ok is True
        assert r.data == {"k": 1}
        assert r.error == ""

    def test_ok_false(self):
        r = StructuredResult(ok=False, raw="bad json", error="json_parse_error: ...")
        assert r.ok is False
        assert r.data is None

    def test_raw_always_present(self):
        r = StructuredResult(ok=False, raw="original text", error="x")
        assert r.raw == "original text"


# ---------------------------------------------------------------------------
# InferencePort protocol
# ---------------------------------------------------------------------------

class TestInferencePort:
    def test_null_inference_satisfies_protocol(self):
        null = NullInference(canned_text="hello")
        assert isinstance(null, InferencePort)

    def test_callable_satisfies_protocol(self):
        def my_fn(system_prompt, user_message, model_id=None, temperature=None, max_tokens=None):
            return {}
        assert isinstance(my_fn, InferencePort)


# ---------------------------------------------------------------------------
# parse_llm_json
# ---------------------------------------------------------------------------

class TestParseLLMJson:
    def test_valid_json_object(self):
        r = parse_llm_json('{"key": "value"}')
        assert r.ok is True
        assert r.data == {"key": "value"}

    def test_malformed_json(self):
        r = parse_llm_json("not valid json")
        assert r.ok is False
        assert "json_parse_error" in r.error

    def test_empty_string(self):
        r = parse_llm_json("")
        assert r.ok is False
        assert "empty" in r.error

    def test_whitespace_only(self):
        r = parse_llm_json("   ")
        assert r.ok is False

    def test_strips_json_markdown_fence(self):
        text = '```json\n{"foo": 1}\n```'
        r = parse_llm_json(text)
        assert r.ok is True
        assert r.data == {"foo": 1}

    def test_strips_plain_markdown_fence(self):
        text = '```\n{"bar": 2}\n```'
        r = parse_llm_json(text)
        assert r.ok is True
        assert r.data == {"bar": 2}

    def test_no_fence_plain_json(self):
        r = parse_llm_json('{"x": true}')
        assert r.ok is True
        assert r.data["x"] is True

    def test_required_fields_present(self):
        r = parse_llm_json('{"a": 1, "b": 2}', required_fields=["a", "b"])
        assert r.ok is True

    def test_required_fields_missing(self):
        r = parse_llm_json('{"a": 1}', required_fields=["a", "b"])
        assert r.ok is False
        assert "b" in r.error
        assert r.data is not None  # partial data still accessible

    def test_required_fields_empty_list_always_ok(self):
        r = parse_llm_json('{"anything": 1}', required_fields=[])
        assert r.ok is True

    def test_json_array_is_rejected(self):
        r = parse_llm_json("[1, 2, 3]")
        assert r.ok is False
        assert "expected JSON object" in r.error

    def test_raw_preserved_on_success(self):
        text = '{"k": "v"}'
        r = parse_llm_json(text)
        assert r.raw == text

    def test_raw_preserved_on_failure(self):
        text = "this is not json"
        r = parse_llm_json(text)
        assert r.raw == text

    def test_none_text(self):
        r = parse_llm_json(None)  # type: ignore[arg-type]
        assert r.ok is False


# ---------------------------------------------------------------------------
# NullInference
# ---------------------------------------------------------------------------

class TestNullInference:
    def test_returns_canned_text(self):
        null = NullInference(canned_text='{"result": "ok"}')
        result = null("sys", "user")
        assert result["text"] == '{"result": "ok"}'

    def test_stop_reason_end_turn_when_text(self):
        null = NullInference(canned_text="hello")
        assert null("s", "u")["stop_reason"] == "end_turn"

    def test_stop_reason_disabled_when_empty(self):
        null = NullInference(canned_text="")
        assert null("s", "u")["stop_reason"] == "disabled"

    def test_returns_all_converse_keys(self):
        null = NullInference(canned_text="hi")
        result = null("s", "u")
        for key in ("text", "input_tokens", "output_tokens", "model_id", "latency_ms", "stop_reason"):
            assert key in result, f"missing key: {key}"

    def test_model_id_passthrough(self):
        null = NullInference()
        result = null("s", "u", model_id="override-model")
        assert result["model_id"] == "override-model"

    def test_default_model_id_when_none_passed(self):
        null = NullInference(model_id="my-null")
        result = null("s", "u")
        assert result["model_id"] == "my-null"

    def test_token_counts_propagated(self):
        null = NullInference(canned_text="x", input_tokens=10, output_tokens=5)
        result = null("s", "u")
        assert result["input_tokens"] == 10
        assert result["output_tokens"] == 5

    def test_satisfies_inference_port_protocol(self):
        assert isinstance(NullInference(), InferencePort)

    def test_accepts_optional_params_without_error(self):
        null = NullInference(canned_text="y")
        result = null("sys", "user", model_id="m", temperature=0.5, max_tokens=512)
        assert result["text"] == "y"


# ---------------------------------------------------------------------------
# converse_typed
# ---------------------------------------------------------------------------

class TestConverseTyped:
    def _mock_client(self, text="response", input_t=100, output_t=50):
        mock = MagicMock()
        mock.converse.return_value = {
            "output": {"message": {"content": [{"text": text}]}},
            "usage": {"inputTokens": input_t, "outputTokens": output_t},
            "stopReason": "end_turn",
        }
        return mock

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_returns_inference_response(self):
        with patch.object(llm_module, "_get_client", return_value=self._mock_client()):
            r = converse_typed("sys", "user")
        assert isinstance(r, InferenceResponse)

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_preserves_token_usage(self):
        with patch.object(llm_module, "_get_client", return_value=self._mock_client(input_t=77, output_t=33)):
            r = converse_typed("sys", "user")
        assert r.usage.input_tokens == 77
        assert r.usage.output_tokens == 33
        assert r.usage.total_tokens == 110

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_preserves_stop_reason(self):
        with patch.object(llm_module, "_get_client", return_value=self._mock_client()):
            r = converse_typed("sys", "user")
        assert r.stop_reason == "end_turn"

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_ok_true_on_success(self):
        with patch.object(llm_module, "_get_client", return_value=self._mock_client(text="hello")):
            r = converse_typed("sys", "user")
        assert r.ok is True

    @patch.object(llm_module, "LLM_ENABLED", False)
    def test_ok_false_when_disabled(self):
        r = converse_typed("sys", "user")
        assert r.ok is False
        assert r.stop_reason == "disabled"

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_to_dict_matches_converse_output(self):
        mock_client = self._mock_client(text="hello", input_t=20, output_t=10)
        with patch.object(llm_module, "_get_client", return_value=mock_client):
            typed_result = converse_typed("sys", "user")
            raw_result   = converse("sys", "user")
        assert typed_result.to_dict() == raw_result


# ---------------------------------------------------------------------------
# Backward compatibility: converse() dict shape unchanged
# ---------------------------------------------------------------------------

class TestConverseBackwardCompatibility:
    @patch.object(llm_module, "LLM_ENABLED", False)
    def test_converse_returns_dict(self):
        result = converse("sys", "user")
        assert isinstance(result, dict)

    @patch.object(llm_module, "LLM_ENABLED", False)
    def test_converse_disabled_dict_keys(self):
        result = converse("sys", "user")
        for key in ("text", "input_tokens", "output_tokens", "model_id", "latency_ms", "stop_reason"):
            assert key in result

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_converse_success_dict_keys(self):
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "ok"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "stopReason": "end_turn",
        }
        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = converse("sys", "user")
        for key in ("text", "input_tokens", "output_tokens", "model_id", "latency_ms", "stop_reason"):
            assert key in result


# ---------------------------------------------------------------------------
# refine_hypothesis backward compatibility (migrated parse_llm_json call site)
# ---------------------------------------------------------------------------

class TestRefineHypothesisBackwardCompatibility:
    _originals = [{"name": "h1", "root_cause": "orig", "score": 70, "reasoning": "orig"}]

    @patch.object(llm_module, "LLM_ENABLED", False)
    def test_returns_originals_when_disabled(self):
        result = refine_hypothesis("timeout", "svc", "sum", "ev", self._originals)
        assert result["refined_hypotheses"] == self._originals

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_parses_valid_json_response(self):
        refined_data = [{"name": "h1", "root_cause": "refined", "score": 95, "reasoning": "r"}]
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": json.dumps({"hypotheses": refined_data})}]}},
            "usage": {"inputTokens": 200, "outputTokens": 100},
            "stopReason": "end_turn",
        }
        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = refine_hypothesis("timeout", "svc", "sum", "ev", self._originals)
        assert result["refined_hypotheses"][0]["score"] == 95
        assert result["input_tokens"] == 200

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_falls_back_on_malformed_json(self):
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "not valid json"}]}},
            "usage": {"inputTokens": 50, "outputTokens": 20},
            "stopReason": "end_turn",
        }
        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = refine_hypothesis("timeout", "svc", "sum", "ev", self._originals)
        assert result["refined_hypotheses"] == self._originals

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_falls_back_when_hypotheses_key_missing(self):
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": '{"other_key": []}'}]}},
            "usage": {"inputTokens": 30, "outputTokens": 10},
            "stopReason": "end_turn",
        }
        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = refine_hypothesis("timeout", "svc", "sum", "ev", self._originals)
        assert result["refined_hypotheses"] == self._originals

    @patch.object(llm_module, "_BOTO3_AVAILABLE", True)
    @patch.object(llm_module, "LLM_ENABLED", True)
    @patch.object(llm_module, "MODEL_ID", "test-model")
    def test_accepts_json_with_markdown_fences(self):
        refined_data = [{"name": "h1", "root_cause": "fenced", "score": 88, "reasoning": "r"}]
        fenced = f"```json\n{json.dumps({'hypotheses': refined_data})}\n```"
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": fenced}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "stopReason": "end_turn",
        }
        with patch.object(llm_module, "_get_client", return_value=mock_client):
            result = refine_hypothesis("timeout", "svc", "sum", "ev", self._originals)
        assert result["refined_hypotheses"][0]["score"] == 88
