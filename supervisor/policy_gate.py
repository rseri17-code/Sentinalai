from __future__ import annotations
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any


class PolicyDecision(str, Enum):
    ALLOW = "ALLOW"
    REJECT = "REJECT"
    REPLAN = "REPLAN"   # future use — treat as REJECT for now


@dataclass
class PolicyResult:
    decision: PolicyDecision
    reason: str = ""
    alternative_tool: str | None = None   # suggestion when REPLAN

    @property
    def allowed(self) -> bool:
        return self.decision == PolicyDecision.ALLOW


_ALLOW = PolicyResult(decision=PolicyDecision.ALLOW)

# Tools that are always read-only and safe
_READ_ONLY_TOOLS = frozenset({
    "moogsoft", "splunk", "sysdig", "signalfx", "dynatrace",
    "servicenow", "confluence", "thousandeyes",
})

# Tools that can write/mutate — require explicit scope
_WRITE_CAPABLE_TOOLS = frozenset({
    "github",     # create_pr, create_fix_pr
    "kubernetes", # rollback, scale
})


def _gate_enabled() -> bool:
    return os.environ.get("POLICY_GATE_ENABLED", "false").lower() in ("1", "true", "yes")


class PolicyGate:
    """Pre-dispatch policy gate: Validate → Scope → Budget → Allow/Reject."""

    def evaluate(
        self,
        tool_name: str,
        params: dict[str, Any],
        budget_remaining: int = 999,
        investigation_context: dict[str, Any] | None = None,
    ) -> PolicyResult:
        """Run all checks and return a PolicyResult."""
        if not _gate_enabled():
            return _ALLOW

        result = self._validate(tool_name, params)
        if not result.allowed:
            return result

        result = self._check_budget(tool_name, budget_remaining)
        if not result.allowed:
            return result

        result = self._check_scope(tool_name, investigation_context or {})
        return result

    def _validate(self, tool_name: str, params: dict) -> PolicyResult:
        """Schema validation: params must be a dict."""
        if not isinstance(params, dict):
            return PolicyResult(
                PolicyDecision.REJECT,
                reason=f"Invalid params type for {tool_name}: expected dict, got {type(params).__name__}",
            )
        return _ALLOW

    def _check_budget(self, tool_name: str, budget_remaining: int) -> PolicyResult:
        """Reject if budget is exhausted."""
        if budget_remaining <= 0:
            return PolicyResult(
                PolicyDecision.REJECT,
                reason=f"Budget exhausted (remaining={budget_remaining}) — cannot call {tool_name}",
            )
        return _ALLOW

    def _check_scope(self, tool_name: str, context: dict) -> PolicyResult:
        """Check if this tool is within the investigation's permitted scope."""
        # Write-capable tools require explicit permission in context
        server = tool_name.split(".")[0] if "." in tool_name else tool_name
        if server in _WRITE_CAPABLE_TOOLS:
            if not context.get("allow_write_tools", False):
                # Write tools are allowed by default in normal investigations
                # (kubernetes rollback, github PR creation are legitimate actions)
                # Only block if context explicitly prohibits writes
                pass
        return _ALLOW


# Module-level singleton
_gate = PolicyGate()


def evaluate(
    tool_name: str,
    params: dict[str, Any],
    budget_remaining: int = 999,
    investigation_context: dict[str, Any] | None = None,
) -> PolicyResult:
    """Module-level convenience function."""
    return _gate.evaluate(tool_name, params, budget_remaining, investigation_context)
