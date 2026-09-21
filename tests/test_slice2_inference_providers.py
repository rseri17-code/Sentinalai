"""Slice 2 acceptance tests (§6.2 A–D).

A. Same investigation, two injected InferencePort classes, identical canned
   InferenceResponse → equal root_cause / confidence / evidence_timeline /
   worker call list. Inject via set_inference_port — do not patch boto3.
B. Provider failure → fail-open (pre-LLM hypotheses kept; result returned).
C. Null / LLM off equals CI path (no network; INC12345 still deterministic).
D. LLM_PROVIDER selects adapter class without SRE-domain code change.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

import supervisor.llm as llm_module
from sentinel_core.models.inference import InferencePort, InferenceResponse, InferenceUsage
from supervisor.agent import SentinalAISupervisor
from supervisor.llm import dispose, set_inference_port
from supervisor.sentinel_config import SentinelConfig, reset_config
from tests.fixtures.expected_rca_outputs import EXPECTED_RCA
from tests.test_supervisor import _build_mock_workers


_REFINE_JSON = json.dumps({
    "hypotheses": [
        {
            "name": "downstream_slow_queries",
            "root_cause": "payment-service database slow queries causing upstream timeouts",
            "score": 95,
            "reasoning": "Canned refine: payment-service latency preceded api-gateway timeouts.",
        },
        {
            "name": "timeout_undetermined",
            "root_cause": "timeout cause undetermined",
            "score": 10,
            "reasoning": "deprioritized by canned overlay",
        },
    ]
})
_REASONING_TEXT = (
    "Canned reasoning: payment-service database slow queries caused "
    "api-gateway timeouts."
)


@pytest.fixture(autouse=True)
def _reset_port():
    dispose()
    yield
    dispose()


def _canned_dict(system_prompt: str) -> dict[str, Any]:
    refine = "refine" in system_prompt.lower() or "re-rank" in system_prompt.lower()
    text = _REFINE_JSON if refine else _REASONING_TEXT
    return InferenceResponse(
        text=text,
        model_id="canned-slice2",
        stop_reason="end_turn",
        latency_ms=1.0,
        usage=InferenceUsage(input_tokens=10, output_tokens=20),
    ).to_dict()


class CannedBedrockPort:
    """Stand-in for the Bedrock adapter returning a frozen InferenceResponse."""

    def __call__(
        self,
        system_prompt: str,
        user_message: str,
        model_id: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        return _canned_dict(system_prompt)


class CannedAnthropicPort:
    """Different class, identical canned InferenceResponse."""

    def __call__(
        self,
        system_prompt: str,
        user_message: str,
        model_id: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        return _canned_dict(system_prompt)


class FailingPort:
    """Provider that returns a taxonomy error (fail-open)."""

    def __call__(
        self,
        system_prompt: str,
        user_message: str,
        model_id: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        return InferenceResponse(
            text="",
            model_id="failing-port",
            stop_reason="error",
            latency_ms=1.0,
            error="unknown",
        ).to_dict()


class RaisingPort:
    def __call__(
        self,
        system_prompt: str,
        user_message: str,
        model_id: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        raise RuntimeError("provider down")


def _run_inc12345() -> dict:
    sup = SentinalAISupervisor()
    # Sequential playbook so worker-call order is stable across two runs.
    sup._parallel_playbook = False
    _build_mock_workers(sup, "INC12345")
    return sup.investigate("INC12345")


def _worker_calls(result: dict) -> list[tuple[str, str]]:
    return sorted(
        (str(r.get("tool", "")), str(r.get("action", "")))
        for r in result.get("receipts", [])
    )


def _timeline_events(result: dict) -> list[tuple[Any, Any, Any]]:
    out = []
    for event in result.get("evidence_timeline", []):
        if isinstance(event, dict):
            out.append((event.get("timestamp"), event.get("source"), event.get("event")))
        else:
            out.append((None, None, str(event)))
    return out


class TestASameInvestigationTwoProviders:
    """§6.2 A — inject ports; do not patch boto3 in the SRE investigation."""

    def test_identical_canned_response_equal_rca(self):
        assert CannedBedrockPort is not CannedAnthropicPort
        assert isinstance(CannedBedrockPort(), InferencePort)
        assert isinstance(CannedAnthropicPort(), InferencePort)

        with patch.object(llm_module, "LLM_ENABLED", True), \
             patch.object(llm_module, "MODEL_ID", "canned-slice2"):
            set_inference_port(CannedBedrockPort())
            bedrock_result = _run_inc12345()
            set_inference_port(CannedAnthropicPort())
            anthropic_result = _run_inc12345()

        assert bedrock_result["root_cause"] == anthropic_result["root_cause"]
        assert bedrock_result["confidence"] == anthropic_result["confidence"]
        assert _timeline_events(bedrock_result) == _timeline_events(anthropic_result)
        assert _worker_calls(bedrock_result) == _worker_calls(anthropic_result)
        assert bedrock_result["root_cause"]
        assert _worker_calls(bedrock_result)


class TestBProviderFailureFailOpen:
    """§6.2 B — provider error keeps pre-LLM hypotheses; investigate returns."""

    def test_error_response_keeps_pre_llm_rca(self):
        with patch.object(llm_module, "LLM_ENABLED", False):
            baseline = _run_inc12345()

        with patch.object(llm_module, "LLM_ENABLED", True), \
             patch.object(llm_module, "MODEL_ID", "failing-port"):
            set_inference_port(FailingPort())
            failed = _run_inc12345()

        assert "root_cause" in failed
        assert "confidence" in failed
        assert failed["root_cause"] == baseline["root_cause"]
        assert failed["confidence"] == baseline["confidence"]

    def test_raised_provider_still_returns_result(self):
        with patch.object(llm_module, "LLM_ENABLED", False):
            baseline = _run_inc12345()

        with patch.object(llm_module, "LLM_ENABLED", True), \
             patch.object(llm_module, "MODEL_ID", "raising-port"):
            set_inference_port(RaisingPort())
            failed = _run_inc12345()

        assert failed["root_cause"] == baseline["root_cause"]
        assert failed["confidence"] == baseline["confidence"]


class TestCNullEqualsCiPath:
    """§6.2 C — LLM off / null provider: no network, INC12345 still matches."""

    def test_llm_off_no_network_matches_expected(self):
        expected = EXPECTED_RCA["INC12345"]
        with patch.object(llm_module, "LLM_ENABLED", False), \
             patch.object(llm_module, "LLM_PROVIDER", "null"):
            with patch.object(llm_module, "_get_client", side_effect=AssertionError("boto3")), \
                 patch.object(llm_module, "_anthropic_client", side_effect=AssertionError("anthropic")):
                result = _run_inc12345()
        root = result["root_cause"].lower()
        for kw in expected["root_cause_keywords"]:
            assert kw.lower() in root
        assert expected["confidence_min"] <= result["confidence"] <= expected["confidence_max"]

    def test_null_provider_uses_null_inference(self):
        from supervisor.inference_helpers import NullInference
        with patch.object(llm_module, "LLM_ENABLED", True), \
             patch.object(llm_module, "LLM_PROVIDER", "null"), \
             patch.object(llm_module, "MODEL_ID", "test-model"):
            assert isinstance(llm_module.get_inference_port(), NullInference)
            result = llm_module.converse("sys", "user")
        assert result["stop_reason"] == "disabled"
        assert result["text"] == ""


class TestDConfigSelectsAdapter:
    """§6.2 D — LLM_PROVIDER selects the adapter class; SRE code unchanged."""

    def test_bedrock_vs_anthropic_factory(self):
        with patch.object(llm_module, "LLM_ENABLED", True), \
             patch.object(llm_module, "MODEL_ID", "test-model"), \
             patch.object(llm_module, "_BOTO3_AVAILABLE", True), \
             patch.object(llm_module, "_ANTHROPIC_AVAILABLE", True):
            with patch.object(llm_module, "LLM_PROVIDER", "bedrock"):
                assert isinstance(llm_module.get_inference_port(), llm_module.BedrockInference)
            with patch.object(llm_module, "LLM_PROVIDER", "anthropic"):
                assert isinstance(llm_module.get_inference_port(), llm_module.AnthropicInference)
            with patch.object(llm_module, "LLM_PROVIDER", "openai"):
                from supervisor.inference_helpers import NullInference
                assert isinstance(llm_module.get_inference_port(), NullInference)

    def test_sentinel_config_exposes_provider(self, monkeypatch):
        reset_config()
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
        try:
            cfg = SentinelConfig.from_env()
            assert cfg.supervisor.llm_provider == "anthropic"
            assert cfg.supervisor.llm_model == "claude-sonnet-4-6"
        finally:
            reset_config()

    def test_converse_uses_factory_not_sre_imports(self):
        """converse() is the only door; agent does not import provider SDKs."""
        import inspect
        from supervisor import agent as agent_mod
        src = inspect.getsource(agent_mod)
        assert "import anthropic" not in src
        assert "anthropic.Anthropic" not in src
        assert "openai.OpenAI" not in src
        assert "boto3.client" not in src
