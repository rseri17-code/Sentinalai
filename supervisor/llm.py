"""Investigation LLM facade for SentinalAI.

``converse()`` is the SRE-facing API. It resolves an ``InferencePort`` from
env and returns the same dict shape as the original Bedrock Converse client:

- ``LLM_ENABLED=false`` or ``LLM_PROVIDER=null`` → ``NullInference``
- ``LLM_PROVIDER=bedrock`` (default when enabled) → Bedrock Converse
- ``LLM_PROVIDER=anthropic`` → Anthropic Messages API
- any other value → ``NullInference`` + warning

Callers (hypothesis refine, reasoning, classify fallback, planner, judge)
must not import a provider SDK. Tests inject a port via ``set_inference_port``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger("sentinalai.llm")

# ---------------------------------------------------------------------------
# Optional boto3 import (graceful — tests run without it)
# ---------------------------------------------------------------------------

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError as _ClientError
    _BOTO3_AVAILABLE = True
except ImportError:
    _BOTO3_AVAILABLE = False
    _ClientError = None

try:
    import anthropic as _anthropic_sdk
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _anthropic_sdk = None
    _ANTHROPIC_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
# LLM_MODEL is the portable name; BEDROCK_MODEL_ID remains the Bedrock alias.
MODEL_ID = (
    os.environ.get("LLM_MODEL")
    or os.environ.get("BEDROCK_MODEL_ID")
    or "anthropic.claude-sonnet-4-5-20250929-v1:0"
)
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.0"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "2048"))
# Default false — matches CI, SentinelConfig, and classify_incident.
LLM_ENABLED = os.environ.get("LLM_ENABLED", "false").lower() in ("true", "1", "yes")
# null | none | disabled → NullInference; bedrock (default) → Bedrock Converse;
# anthropic → Anthropic Messages. Unknown values → NullInference + warning.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "bedrock").strip().lower() or "bedrock"
# Token-bucket: max concurrent LLM calls and per-minute call cap
LLM_MAX_CONCURRENT = int(os.environ.get("LLM_MAX_CONCURRENT", "5"))
LLM_MAX_CALLS_PER_MIN = int(os.environ.get("LLM_MAX_CALLS_PER_MIN", "60"))

_NULL_PROVIDERS = frozenset({"null", "none", "disabled"})
_DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
_unknown_provider_warned = False
_port_override: Any | None = None

# ---------------------------------------------------------------------------
# Token-bucket rate limiter — prevents 429s under concurrent investigations
# ---------------------------------------------------------------------------

class _TokenBucket:
    """Thread-safe token-bucket rate limiter."""

    def __init__(self, max_concurrent: int, max_per_minute: int) -> None:
        self._semaphore = threading.Semaphore(max_concurrent)
        self._max_per_minute = max_per_minute
        self._call_times: list[float] = []
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 30.0) -> bool:
        """Block until a call slot is available. Returns False on timeout."""
        deadline = time.monotonic() + timeout
        # Per-minute cap: wait if we've hit the limit in the last 60s
        while True:
            with self._lock:
                now = time.monotonic()
                self._call_times = [t for t in self._call_times if now - t < 60]
                if len(self._call_times) < self._max_per_minute:
                    break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)
        # Concurrency cap
        acquired = self._semaphore.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if acquired:
            with self._lock:
                self._call_times.append(time.monotonic())
        return acquired

    def release(self) -> None:
        self._semaphore.release()


_rate_limiter: _TokenBucket | None = None
_rate_limiter_lock = threading.Lock()


def _get_rate_limiter() -> _TokenBucket:
    global _rate_limiter
    if _rate_limiter is not None:
        return _rate_limiter
    with _rate_limiter_lock:
        if _rate_limiter is None:
            _rate_limiter = _TokenBucket(LLM_MAX_CONCURRENT, LLM_MAX_CALLS_PER_MIN)
    return _rate_limiter


# ---------------------------------------------------------------------------
# Boto3 client (lazy init — lock protects against concurrent first-call races)
# ---------------------------------------------------------------------------

_client = None
_client_lock = threading.Lock()


def _get_client():
    """Lazily create the bedrock-runtime boto3 client (thread-safe)."""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        # Re-check inside the lock to handle the race between the outer check
        # and lock acquisition (double-checked locking pattern).
        if _client is not None:
            return _client
        if not _BOTO3_AVAILABLE:
            logger.debug("boto3 not installed — LLM calls disabled")
            return None
        try:
            _client = boto3.client(
                "bedrock-runtime",
                region_name=AWS_REGION,
                config=BotoConfig(
                    retries={"max_attempts": 2, "mode": "adaptive"},
                    connect_timeout=10,
                    read_timeout=60,
                ),
            )
            logger.info("Bedrock runtime client initialised (model=%s)", MODEL_ID)
            return _client
        except Exception as exc:
            logger.warning("Failed to create bedrock-runtime client: %s", exc)
            return None


def _resolved_provider() -> str:
    """Normalize LLM_PROVIDER. Empty/unset → bedrock."""
    raw = (LLM_PROVIDER or "bedrock").strip().lower()
    if raw in _NULL_PROVIDERS:
        return "null"
    return raw or "bedrock"


def is_enabled() -> bool:
    """Check whether a live (non-null) inference backend is configured.

    True when a test has injected a port, or when LLM_ENABLED is on, a model
    id is set, and the selected provider's SDK is importable. Null/unknown
    providers and the default-off flag keep the investigation path LLM-free.
    """
    if _port_override is not None:
        return True
    if not LLM_ENABLED or not MODEL_ID:
        return False
    provider = _resolved_provider()
    if provider == "bedrock":
        return bool(_BOTO3_AVAILABLE)
    if provider == "anthropic":
        return bool(_ANTHROPIC_AVAILABLE)
    return False


def set_inference_port(port: Any | None) -> None:
    """Inject an InferencePort, bypassing env factory resolution.

    Tests use this to supply canned providers without patching boto3 or
    Anthropic. Pass ``None`` to restore factory resolution.
    """
    global _port_override
    _port_override = port


def get_inference_port() -> Any:
    """Resolve the InferencePort for this process env (no cache — test-safe).

    Returns NullInference when disabled, provider=null, or the provider is
    unknown. Returns BedrockInference / AnthropicInference when that
    provider is selected and its SDK is importable.
    """
    global _unknown_provider_warned
    from supervisor.inference_helpers import NullInference

    if _port_override is not None:
        return _port_override

    provider = _resolved_provider()
    if not LLM_ENABLED or not MODEL_ID or provider == "null":
        return NullInference(model_id=MODEL_ID)
    if provider == "bedrock":
        if not _BOTO3_AVAILABLE:
            return NullInference(model_id=MODEL_ID)
        return _BEDROCK_PORT
    if provider == "anthropic":
        if not _ANTHROPIC_AVAILABLE:
            return NullInference(model_id=MODEL_ID)
        return _ANTHROPIC_PORT
    if not _unknown_provider_warned:
        logger.warning(
            "LLM_PROVIDER=%r is not implemented; using NullInference "
            "(supported: null, bedrock, anthropic)",
            LLM_PROVIDER,
        )
        _unknown_provider_warned = True
    return NullInference(model_id=MODEL_ID)


# ---------------------------------------------------------------------------
# Bedrock InferencePort
# ---------------------------------------------------------------------------

class BedrockInference:
    """InferencePort backed by Amazon Bedrock Converse.

    Request/response translation stays in this adapter. SRE callers use
    converse() and never construct this class themselves.
    """

    def __call__(
        self,
        system_prompt: str,
        user_message: str,
        model_id: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        client = _get_client()
        if client is None:
            return _disabled_response()

        limiter = _get_rate_limiter()
        if not limiter.acquire(timeout=30.0):
            logger.warning(
                "LLM rate limiter timeout — dropping call (too many concurrent requests)"
            )
            return {**_disabled_response(), "error": "rate_limited"}
        try:
            return _do_converse(
                client, system_prompt, user_message, model_id, temperature, max_tokens
            )
        finally:
            limiter.release()


_BEDROCK_PORT = BedrockInference()


# ---------------------------------------------------------------------------
# Anthropic InferencePort
# ---------------------------------------------------------------------------

_ANTHROPIC_STOP = {
    "end_turn": "end_turn",
    "max_tokens": "max_tokens",
    "stop_sequence": "end_turn",
}


def _to_anthropic_model(model_id: str) -> str:
    """Map a portable / Bedrock-style id to a native Anthropic model id."""
    if not model_id:
        return _DEFAULT_ANTHROPIC_MODEL
    if model_id.startswith("claude-"):
        return model_id
    lower = model_id.lower()
    if "haiku" in lower:
        return "claude-haiku-4-5-20251001"
    if "opus" in lower:
        return "claude-opus-4-6"
    if "sonnet" in lower:
        return _DEFAULT_ANTHROPIC_MODEL
    return _DEFAULT_ANTHROPIC_MODEL


def _anthropic_client() -> Any:
    """Build an Anthropic client from call-time env. Never logs the key."""
    if not _ANTHROPIC_AVAILABLE or _anthropic_sdk is None:
        logger.debug("anthropic SDK not installed — LLM calls disabled")
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY is not set — Anthropic calls disabled")
        return None
    return _anthropic_sdk.Anthropic(api_key=api_key, max_retries=2, timeout=60.0)


def _map_anthropic_error(exc: BaseException) -> str:
    """Map SDK exceptions to InferenceError values. Never include secrets."""
    from sentinel_core.models.inference import InferenceError

    if _ANTHROPIC_AVAILABLE and _anthropic_sdk is not None:
        if isinstance(exc, _anthropic_sdk.RateLimitError):
            return InferenceError.RATE_LIMITED.value
        if isinstance(exc, (
            _anthropic_sdk.APITimeoutError,
            _anthropic_sdk.DeadlineExceededError,
        )):
            return InferenceError.TIMEOUT.value
        if isinstance(exc, _anthropic_sdk.APIConnectionError):
            return InferenceError.TIMEOUT.value
        if isinstance(exc, _anthropic_sdk.APIStatusError):
            status = getattr(exc, "status_code", None)
            if status == 429:
                return InferenceError.RATE_LIMITED.value
            if status == 408:
                return InferenceError.TIMEOUT.value
    return InferenceError.UNKNOWN.value


def _anthropic_text(content: Any) -> str:
    parts: list[str] = []
    for block in content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        elif isinstance(block, dict) and block.get("text"):
            parts.append(str(block["text"]))
    return "".join(parts)


def _do_anthropic(
    client: Any,
    system_prompt: str,
    user_message: str,
    model_id: str | None,
    temperature: float | None,
    max_tokens: int | None,
) -> dict[str, Any]:
    native_model = _to_anthropic_model(model_id or MODEL_ID)
    resolved_temp = temperature if temperature is not None else LLM_TEMPERATURE
    resolved_max = max_tokens or LLM_MAX_TOKENS

    start = time.monotonic()
    try:
        response = client.messages.create(
            model=native_model,
            max_tokens=resolved_max,
            temperature=resolved_temp,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        latency_ms = (time.monotonic() - start) * 1000
        text = _anthropic_text(getattr(response, "content", None))
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        raw_stop = getattr(response, "stop_reason", None) or "end_turn"
        stop_reason = _ANTHROPIC_STOP.get(raw_stop, "end_turn")

        logger.info(
            "LLM call: model=%s input_tokens=%d output_tokens=%d latency=%.0fms",
            native_model, input_tokens, output_tokens, latency_ms,
        )
        return {
            "text": text,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model_id": native_model,
            "latency_ms": round(latency_ms, 1),
            "stop_reason": stop_reason,
        }
    except Exception as exc:
        latency_ms = (time.monotonic() - start) * 1000
        error = _map_anthropic_error(exc)
        logger.error("Anthropic Messages failed: %s (%.0fms)", error, latency_ms)
        return {
            "text": "",
            "error": error,
            "input_tokens": 0,
            "output_tokens": 0,
            "model_id": native_model,
            "latency_ms": round(latency_ms, 1),
            "stop_reason": "error",
        }


class AnthropicInference:
    """InferencePort backed by the Anthropic Messages API.

    Credentials are read from ANTHROPIC_API_KEY at call time and are never
    logged. Request/response translation stays in this adapter.
    """

    def __call__(
        self,
        system_prompt: str,
        user_message: str,
        model_id: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        client = _anthropic_client()
        if client is None:
            return _disabled_response()

        limiter = _get_rate_limiter()
        if not limiter.acquire(timeout=30.0):
            logger.warning(
                "LLM rate limiter timeout — dropping call (too many concurrent requests)"
            )
            return {**_disabled_response(), "error": "rate_limited"}
        try:
            return _do_anthropic(
                client, system_prompt, user_message, model_id, temperature, max_tokens
            )
        finally:
            limiter.release()


_ANTHROPIC_PORT = AnthropicInference()


# ---------------------------------------------------------------------------
# Core: converse() facade
# ---------------------------------------------------------------------------

def converse(
    system_prompt: str,
    user_message: str,
    model_id: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Run one inference call via the resolved InferencePort.

    Args:
        system_prompt: System-level instructions
        user_message: User message content
        model_id: Override model ID (default: LLM_MODEL / BEDROCK_MODEL_ID)
        temperature: Override temperature (default: LLM_TEMPERATURE env var)
        max_tokens: Override max tokens (default: LLM_MAX_TOKENS env var)

    Returns:
        Dict with keys:
            text: Response text
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            model_id: Model used
            latency_ms: Call duration in milliseconds
            stop_reason: Why generation stopped
    """
    port = get_inference_port()
    return port(system_prompt, user_message, model_id, temperature, max_tokens)


def _do_converse(
    client: Any,
    system_prompt: str,
    user_message: str,
    model_id: str | None,
    temperature: float | None,
    max_tokens: int | None,
) -> dict[str, Any]:
    resolved_model = model_id or MODEL_ID
    resolved_temp = temperature if temperature is not None else LLM_TEMPERATURE
    resolved_max = max_tokens or LLM_MAX_TOKENS

    start = time.monotonic()
    try:
        response = client.converse(
            modelId=resolved_model,
            messages=[
                {
                    "role": "user",
                    "content": [{"text": user_message}],
                }
            ],
            system=[{"text": system_prompt}],
            inferenceConfig={
                "temperature": resolved_temp,
                "maxTokens": resolved_max,
            },
        )

        latency_ms = (time.monotonic() - start) * 1000

        # Extract response
        output = response.get("output", {})
        message = output.get("message", {})
        content = message.get("content", [{}])
        text = content[0].get("text", "") if content else ""

        usage = response.get("usage", {})
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)
        stop_reason = response.get("stopReason", "unknown")

        logger.info(
            "LLM call: model=%s input_tokens=%d output_tokens=%d latency=%.0fms",
            resolved_model, input_tokens, output_tokens, latency_ms,
        )

        return {
            "text": text,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model_id": resolved_model,
            "latency_ms": round(latency_ms, 1),
            "stop_reason": stop_reason,
        }

    except Exception as exc:
        latency_ms = (time.monotonic() - start) * 1000
        # Handle boto ClientError specifically if available
        if _BOTO3_AVAILABLE and _ClientError and isinstance(exc, _ClientError):
            error_code = exc.response.get("Error", {}).get("Code", "Unknown")
            logger.error("Bedrock Converse failed: %s (%.0fms)", error_code, latency_ms)
            return {
                "text": "",
                "error": f"bedrock_error: {error_code}",
                "input_tokens": 0,
                "output_tokens": 0,
                "model_id": resolved_model,
                "latency_ms": round(latency_ms, 1),
                "stop_reason": "error",
            }
        logger.error("LLM call exception: %s (%.0fms)", exc, latency_ms)
        return {
            "text": "",
            "error": str(exc),
            "input_tokens": 0,
            "output_tokens": 0,
            "model_id": resolved_model,
            "latency_ms": round(latency_ms, 1),
            "stop_reason": "error",
        }


# ---------------------------------------------------------------------------
# Investigation-specific LLM operations
# ---------------------------------------------------------------------------

def refine_hypothesis(
    incident_type: str,
    service: str,
    summary: str,
    evidence_summary: str,
    hypotheses: list[dict[str, Any]],
    pil_context: str = "",
) -> dict[str, Any]:
    """Use LLM to refine and re-rank hypotheses given evidence.

    Returns dict with:
        refined_hypotheses: list of {name, root_cause, score, reasoning}
        input_tokens, output_tokens, latency_ms
    """
    if not is_enabled():
        return {"refined_hypotheses": hypotheses, "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}

    from supervisor.system_prompt import SUPERVISOR_SYSTEM_PROMPT

    system_prompt = (
        SUPERVISOR_SYSTEM_PROMPT + "\n\n"
        "TASK: Refine and re-rank the given hypotheses based on evidence. "
        "Adjust confidence scores (0-100). "
        "Provide concise reasoning for the top hypothesis. "
        "Respond in JSON format: {\"hypotheses\": [{\"name\": str, \"root_cause\": str, "
        "\"score\": int, \"reasoning\": str}]}"
    )

    pil_block = f"\n\n{pil_context}" if pil_context else ""
    user_message = (
        f"Incident type: {incident_type}\n"
        f"Service: {service}\n"
        f"Summary: {summary}\n\n"
        f"Evidence collected:\n{evidence_summary}\n\n"
        f"Initial hypotheses:\n{json.dumps(hypotheses, indent=2)}\n"
        f"{pil_block}\n"
        "Refine and re-rank these hypotheses based on the evidence. "
        "Return JSON with the refined hypotheses."
    )

    result = converse(system_prompt, user_message)

    if result.get("error") or not result.get("text"):
        return {
            "refined_hypotheses": hypotheses,
            "input_tokens": result.get("input_tokens", 0),
            "output_tokens": result.get("output_tokens", 0),
            "latency_ms": result.get("latency_ms", 0),
        }

    # Parse LLM response
    from supervisor.inference_helpers import parse_llm_json
    _parsed = parse_llm_json(result["text"])
    refined = _parsed.data.get("hypotheses", hypotheses) if _parsed.ok else hypotheses

    return {
        "refined_hypotheses": refined,
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "latency_ms": result.get("latency_ms", 0),
        "model_id": result.get("model_id", MODEL_ID),
    }


def generate_reasoning(
    incident_type: str,
    service: str,
    root_cause: str,
    evidence_summary: str,
    timeline_summary: str,
    pil_context: str = "",
) -> dict[str, Any]:
    """Use LLM to generate a detailed, human-readable reasoning narrative.

    Returns dict with:
        reasoning: str (the narrative)
        input_tokens, output_tokens, latency_ms
    """
    if not is_enabled():
        return {"reasoning": "", "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}

    from supervisor.system_prompt import SUPERVISOR_SYSTEM_PROMPT

    system_prompt = (
        SUPERVISOR_SYSTEM_PROMPT + "\n\n"
        "TASK: Write a clear, concise root cause analysis report. "
        "Reference specific evidence from the timeline. "
        "Explain the causal chain. Keep it under 200 words."
    )

    pil_block = f"\n\n{pil_context}" if pil_context else ""
    user_message = (
        f"Incident: {incident_type} on {service}\n"
        f"Root cause: {root_cause}\n\n"
        f"Evidence:\n{evidence_summary}\n\n"
        f"Timeline:\n{timeline_summary}\n"
        f"{pil_block}\n"
        "Write the reasoning section for this RCA report."
    )

    result = converse(system_prompt, user_message, max_tokens=512)

    return {
        "reasoning": result.get("text", ""),
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "latency_ms": result.get("latency_ms", 0),
        "model_id": result.get("model_id", MODEL_ID),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _disabled_response() -> dict[str, Any]:
    """Return a response indicating LLM is disabled."""
    return {
        "text": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "model_id": MODEL_ID,
        "latency_ms": 0,
        "stop_reason": "disabled",
    }


def converse_typed(
    system_prompt: str,
    user_message: str,
    model_id: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> "Any":
    """Like converse() but returns a typed InferenceResponse.

    Existing callers that use converse() are unaffected.  New code can use
    this function for typed access to token usage, stop_reason, and the ok
    property without manually unpacking the dict.
    """
    from sentinel_core.models.inference import InferenceResponse
    return InferenceResponse.from_dict(
        converse(system_prompt, user_message, model_id, temperature, max_tokens)
    )


def dispose() -> None:
    """Release the boto3 client, port override, and unknown-provider latch."""
    global _client, _unknown_provider_warned, _port_override
    _client = None
    _unknown_provider_warned = False
    _port_override = None
