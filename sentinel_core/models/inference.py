"""Inference contracts for SentinalAI LLM integration.

Zero-dependency types that formalize the existing Bedrock Converse dict
shape. All existing converse() callers continue to receive plain dicts;
these types are used by typed helpers and the NullInference adapter.

Dependency rule: imports only stdlib + typing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable


class InferenceError(str, Enum):
    """Canonical error codes returned by converse() in the 'error' key."""
    RATE_LIMITED  = "rate_limited"
    BEDROCK_ERROR = "bedrock_error"
    TIMEOUT       = "timeout"
    DISABLED      = "disabled"
    PARSE_ERROR   = "parse_error"
    UNKNOWN       = "unknown"


@dataclass
class InferenceUsage:
    """Token usage for a single inference call."""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class InferenceRequest:
    """Typed wrapper for converse() parameters."""
    system_prompt: str
    user_message: str
    model_id: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


@dataclass
class InferenceResponse:
    """Typed wrapper for the converse() return dict.

    Preserves the exact keys returned by converse() so callers can
    round-trip via to_dict() / from_dict() without losing information.
    """
    text: str
    model_id: str
    stop_reason: str
    latency_ms: float = 0.0
    usage: InferenceUsage = field(default_factory=InferenceUsage)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        """True when text is non-empty and no error occurred."""
        return bool(self.text) and self.error is None

    def to_dict(self) -> dict[str, Any]:
        """Return dict matching the exact converse() return shape."""
        d: dict[str, Any] = {
            "text": self.text,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "model_id": self.model_id,
            "latency_ms": self.latency_ms,
            "stop_reason": self.stop_reason,
        }
        if self.error is not None:
            d["error"] = self.error
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "InferenceResponse":
        """Construct from a converse() return dict."""
        return cls(
            text=d.get("text", ""),
            model_id=d.get("model_id", ""),
            stop_reason=d.get("stop_reason", "unknown"),
            latency_ms=float(d.get("latency_ms", 0.0)),
            usage=InferenceUsage(
                input_tokens=d.get("input_tokens", 0),
                output_tokens=d.get("output_tokens", 0),
            ),
            error=d.get("error"),
        )


@dataclass
class StructuredResult:
    """Result of parsing an LLM JSON response.

    ok=True  → data holds the parsed dict; raw is the original text
    ok=False → data is None; error describes the failure; raw is preserved
    """
    ok: bool
    raw: str
    data: Optional[dict[str, Any]] = None
    error: str = ""


@runtime_checkable
class InferencePort(Protocol):
    """Protocol satisfied by converse() and NullInference.

    Any callable with this signature can serve as a drop-in replacement
    for converse() in tests or alternative inference backends.
    """

    def __call__(
        self,
        system_prompt: str,
        user_message: str,
        model_id: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> dict[str, Any]: ...
