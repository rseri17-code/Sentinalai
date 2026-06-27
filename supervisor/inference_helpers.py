"""Inference helpers — structured output parsing and test adapter.

Provides:
  parse_llm_json()  — safe JSON extraction from LLM text responses
  NullInference     — deterministic in-process adapter for tests
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from sentinel_core.models.inference import InferenceResponse, InferenceUsage, StructuredResult


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_llm_json(
    text: str,
    required_fields: list[str] | None = None,
) -> StructuredResult:
    """Safely parse a JSON object from LLM output.

    Strips markdown code fences (```json...``` or ```...```) before parsing.
    Returns StructuredResult with ok=True and data on success, ok=False on any
    failure. The raw text is always preserved for debugging.

    Args:
        text:            Raw text from converse()["text"]
        required_fields: Field names that must be present in the parsed object.
                         If any are missing, ok=False is returned with the
                         partially-parsed data still accessible.
    """
    if not text or not text.strip():
        return StructuredResult(ok=False, raw=text or "", error="empty response")

    fence_match = _FENCE_RE.search(text)
    candidate = fence_match.group(1).strip() if fence_match else text.strip()

    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return StructuredResult(ok=False, raw=text, error=f"json_parse_error: {exc}")

    if not isinstance(data, dict):
        return StructuredResult(
            ok=False, raw=text, error=f"expected JSON object, got {type(data).__name__}"
        )

    if required_fields:
        missing = [f for f in required_fields if f not in data]
        if missing:
            return StructuredResult(
                ok=False, raw=text, data=data,
                error=f"missing required fields: {missing}",
            )

    return StructuredResult(ok=True, raw=text, data=data)


class NullInference:
    """Deterministic in-process inference adapter for tests and LLM-disabled mode.

    Satisfies the InferencePort protocol — callable as a drop-in for converse():

        null = NullInference(canned_text='{"hypotheses": []}')
        result = null(system_prompt="...", user_message="...")
        # result is a dict with the same keys as converse()

    When canned_text is empty the response mirrors a disabled/no-op converse()
    call (stop_reason="disabled").
    """

    def __init__(
        self,
        canned_text: str = "",
        model_id: str = "null-model",
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self._canned = canned_text
        self._model_id = model_id
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens

    def __call__(
        self,
        system_prompt: str,
        user_message: str,
        model_id: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> dict[str, Any]:
        """Return canned response with the same key shape as converse()."""
        return InferenceResponse(
            text=self._canned,
            model_id=model_id or self._model_id,
            stop_reason="end_turn" if self._canned else "disabled",
            latency_ms=0.0,
            usage=InferenceUsage(
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
            ),
        ).to_dict()
