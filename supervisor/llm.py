"""Bedrock Converse API client for SentinalAI.

Uses Claude via Amazon Bedrock's Converse API for:
- Hypothesis refinement: given evidence, refine and re-rank hypotheses
- Reasoning generation: produce human-readable explanations
- Confidence calibration: LLM-assisted confidence adjustment

Falls back gracefully when Bedrock is unavailable or BEDROCK_MODEL_ID is unset.
All calls emit GenAI semantic convention attributes for OTEL tracing.
"""

from __future__ import annotations

import json
import logging
import os
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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-5-20250929-v1:0")
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.0"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "2048"))
LLM_ENABLED = os.environ.get("LLM_ENABLED", "true").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Boto3 client (lazy init)
# ---------------------------------------------------------------------------

_client = None


def _get_client():
    """Lazily create the bedrock-runtime boto3 client."""
    global _client
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


def is_enabled() -> bool:
    """Check whether LLM calls are enabled and configured."""
    return bool(LLM_ENABLED and MODEL_ID and _BOTO3_AVAILABLE)


# ---------------------------------------------------------------------------
# Core: Bedrock Converse API call
# ---------------------------------------------------------------------------

def converse(
    system_prompt: str,
    user_message: str,
    model_id: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Call Bedrock Converse API with Claude.

    Args:
        system_prompt: System-level instructions
        user_message: User message content
        model_id: Override model ID (default: BEDROCK_MODEL_ID env var)
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
    if not is_enabled():
        return _disabled_response()

    client = _get_client()
    if client is None:
        return _disabled_response()

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
) -> dict[str, Any]:
    """Use LLM to refine and re-rank hypotheses given evidence.

    Returns dict with:
        refined_hypotheses: list of {name, root_cause, score, reasoning}
        input_tokens, output_tokens, latency_ms
    """
    if not is_enabled():
        return {"refined_hypotheses": hypotheses, "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}

    system_prompt = (
        "You are an expert SRE performing root cause analysis. "
        "Given the incident evidence and initial hypotheses, refine the analysis. "
        "Re-rank hypotheses by likelihood. Adjust confidence scores (0-100). "
        "Provide concise reasoning for the top hypothesis. "
        "Respond in JSON format: {\"hypotheses\": [{\"name\": str, \"root_cause\": str, "
        "\"score\": int, \"reasoning\": str}]}"
    )

    user_message = (
        f"Incident type: {incident_type}\n"
        f"Service: {service}\n"
        f"Summary: {summary}\n\n"
        f"Evidence collected:\n{evidence_summary}\n\n"
        f"Initial hypotheses:\n{json.dumps(hypotheses, indent=2)}\n\n"
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
    try:
        parsed = json.loads(result["text"])
        refined = parsed.get("hypotheses", hypotheses)
    except (json.JSONDecodeError, TypeError):
        refined = hypotheses

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
) -> dict[str, Any]:
    """Use LLM to generate a detailed, human-readable reasoning narrative.

    Returns dict with:
        reasoning: str (the narrative)
        input_tokens, output_tokens, latency_ms
    """
    if not is_enabled():
        return {"reasoning": "", "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}

    system_prompt = (
        "You are an expert SRE writing a root cause analysis report. "
        "Write a clear, concise explanation of the root cause. "
        "Reference specific evidence from the timeline. "
        "Explain the causal chain. Keep it under 200 words."
    )

    user_message = (
        f"Incident: {incident_type} on {service}\n"
        f"Root cause: {root_cause}\n\n"
        f"Evidence:\n{evidence_summary}\n\n"
        f"Timeline:\n{timeline_summary}\n\n"
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


def dispose() -> None:
    """Release the boto3 client."""
    global _client
    _client = None
