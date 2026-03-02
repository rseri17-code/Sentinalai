"""Severity detection and investigation budget scaling for SentinalAI.

Provides:
- Normalized severity model for investigation depth control
- Auto-detection from Moogsoft incident data and ServiceNow CI metadata
- Budget scaling: critical incidents get more investigation calls
- Deep dive and LLM reasoning toggles based on severity

Severity sources:
    - Moogsoft: incident severity field (1-5 or string labels)
    - ServiceNow: CI tier from CMDB (tier-1 through tier-4)

Composite logic: highest severity wins. A tier-1 CI always escalates
to critical regardless of Moogsoft severity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from supervisor.guardrails import ExecutionBudget

logger = logging.getLogger(__name__)

# =========================================================================
# Severity labels by level
# =========================================================================

_LEVEL_LABELS: dict[int, str] = {
    1: "critical",
    2: "high",
    3: "medium",
    4: "low",
    5: "info",
}

# =========================================================================
# Moogsoft string-to-level mapping
# =========================================================================

_MOOGSOFT_STRING_MAP: dict[str, int] = {
    "critical": 1,
    "major": 2,
    "warning": 3,
    "minor": 4,
    "info": 5,
}

# =========================================================================
# Budget configuration per severity level
# =========================================================================

_BUDGET_CONFIG: dict[int, dict] = {
    1: {"budget": 35, "deep_dive_enabled": True, "deep_dive_bonus": 15, "llm_reasoning": True},
    2: {"budget": 30, "deep_dive_enabled": True, "deep_dive_bonus": 10, "llm_reasoning": True},
    3: {"budget": 25, "deep_dive_enabled": True, "deep_dive_bonus": 10, "llm_reasoning": False},
    4: {"budget": 20, "deep_dive_enabled": False, "deep_dive_bonus": 0, "llm_reasoning": False},
    5: {"budget": 15, "deep_dive_enabled": False, "deep_dive_bonus": 0, "llm_reasoning": False},
}

_DEFAULT_LEVEL = 3


# =========================================================================
# Severity model
# =========================================================================

@dataclass
class InvestigationSeverity:
    """Normalized severity for investigation depth control."""

    level: int  # 1 (critical) to 5 (info)
    label: str  # "critical", "high", "medium", "low", "info"
    source: str  # "moogsoft", "itsm", "composite"
    budget: int  # Total investigation call budget
    deep_dive_enabled: bool  # Whether deep dive triggers on low confidence
    deep_dive_bonus: int  # Extra budget for deep dive pass
    llm_reasoning: bool  # Whether LLM reasoning is forced on


# =========================================================================
# Normalization helpers
# =========================================================================

def normalize_moogsoft_severity(severity_value) -> int:
    """Normalize a Moogsoft severity value to an integer level 1-5.

    Handles:
        - int: clamp to 1-5 range
        - str: map "critical"/"major"/"warning"/"minor"/"info" (case-insensitive)
        - None or unrecognized: return default level (3)

    Returns:
        Integer severity level between 1 and 5 inclusive.
    """
    if severity_value is None:
        return _DEFAULT_LEVEL

    if isinstance(severity_value, int):
        if severity_value < 1 or severity_value > 5:
            logger.warning("Moogsoft severity %d out of range 1-5, defaulting to %d", severity_value, _DEFAULT_LEVEL)
            return _DEFAULT_LEVEL
        return severity_value

    if isinstance(severity_value, str):
        cleaned = severity_value.strip().lower()
        if cleaned in _MOOGSOFT_STRING_MAP:
            return _MOOGSOFT_STRING_MAP[cleaned]
        # Try parsing as integer string
        try:
            int_val = int(cleaned)
            if 1 <= int_val <= 5:
                return int_val
            logger.warning("Moogsoft severity string '%s' out of range 1-5, defaulting to %d", severity_value, _DEFAULT_LEVEL)
            return _DEFAULT_LEVEL
        except ValueError:
            pass
        logger.warning("Unrecognized Moogsoft severity '%s', defaulting to %d", severity_value, _DEFAULT_LEVEL)
        return _DEFAULT_LEVEL

    logger.warning("Unexpected Moogsoft severity type %s, defaulting to %d", type(severity_value).__name__, _DEFAULT_LEVEL)
    return _DEFAULT_LEVEL


def normalize_itsm_tier(tier_value) -> int | None:
    """Normalize a ServiceNow CI tier value to an integer 1-4.

    Handles:
        - "tier-1", "tier-2", etc.
        - "Tier 1", "Tier 2", etc.
        - Bare int or int-string: "1", 1
        - "critical" -> 1
        - None or unrecognized: return None

    Returns:
        Integer tier between 1 and 4, or None if not determinable.
    """
    if tier_value is None:
        return None

    if isinstance(tier_value, int):
        if 1 <= tier_value <= 4:
            return tier_value
        return None

    if isinstance(tier_value, str):
        cleaned = tier_value.strip().lower()

        if not cleaned:
            return None

        # "critical" maps to tier 1
        if cleaned == "critical":
            return 1

        # "tier-N" or "tier N" patterns
        for sep in ("-", " "):
            prefix = f"tier{sep}"
            if cleaned.startswith(prefix):
                rest = cleaned[len(prefix):].strip()
                try:
                    val = int(rest)
                    if 1 <= val <= 4:
                        return val
                except ValueError:
                    pass

        # Bare integer string
        try:
            val = int(cleaned)
            if 1 <= val <= 4:
                return val
        except ValueError:
            pass

        return None

    return None


# =========================================================================
# Main detection function
# =========================================================================

def detect_severity(
    incident: dict,
    itsm_context: dict | None = None,
) -> InvestigationSeverity:
    """Detect investigation severity from Moogsoft incident and optional ITSM context.

    Logic:
        1. Normalize Moogsoft severity from incident["severity"]
        2. Normalize ServiceNow tier from itsm_context["ci"]["tier"]
        3. Highest severity wins (lowest level number)
        4. Apply budget scaling based on final level

    Args:
        incident: Moogsoft incident dict. Expected key: "severity".
        itsm_context: Optional ServiceNow CI details dict.
            Expected structure: {"ci": {"tier": "tier-1", ...}}

    Returns:
        InvestigationSeverity with level, budget, and investigation controls.
    """
    # Step 1: Moogsoft severity
    moogsoft_raw = incident.get("severity") if incident else None
    moogsoft_level = normalize_moogsoft_severity(moogsoft_raw)

    # Step 2: ITSM tier
    itsm_tier = None
    source = "moogsoft"
    if itsm_context:
        ci = itsm_context.get("ci") or {}
        itsm_tier = normalize_itsm_tier(ci.get("tier"))

    # Step 3: Composite — highest severity wins
    level = moogsoft_level

    if itsm_tier is not None:
        if itsm_tier == 1:
            # Tier-1 always escalates to critical
            level = 1
            source = "itsm" if moogsoft_level != 1 else "composite"
        elif itsm_tier == 2:
            # Tier-2: at most high severity
            if level > 2:
                level = 2
                source = "itsm"
            else:
                source = "composite" if moogsoft_raw is not None else "itsm"
        else:
            # Tier-3 and tier-4: never change level, never downgrade
            source = "composite" if moogsoft_raw is not None else "moogsoft"

        # If both sources contributed to final level
        if moogsoft_raw is not None and itsm_tier is not None:
            if source == "moogsoft":
                source = "composite"

    # Step 4: Build severity with budget config
    config = _BUDGET_CONFIG[level]
    label = _LEVEL_LABELS[level]

    return InvestigationSeverity(
        level=level,
        label=label,
        source=source,
        budget=config["budget"],
        deep_dive_enabled=config["deep_dive_enabled"],
        deep_dive_bonus=config["deep_dive_bonus"],
        llm_reasoning=config["llm_reasoning"],
    )


# =========================================================================
# Budget helper
# =========================================================================

def get_budget_for_severity(severity: InvestigationSeverity) -> ExecutionBudget:
    """Create an ExecutionBudget instance scaled to the given severity.

    Args:
        severity: An InvestigationSeverity instance.

    Returns:
        ExecutionBudget with max_calls set to the severity-specific budget.
    """
    return ExecutionBudget(max_calls=severity.budget)
