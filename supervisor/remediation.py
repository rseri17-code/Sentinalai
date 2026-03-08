"""Remediation Engine for SentinalAI.

Generates structured remediation guidance from RCA results. Combines:
1. Code-default templates (REMEDIATION_TEMPLATES) for each incident type
2. YAML overrides (remediation_templates.yaml) ops teams can edit without code changes
3. LLM enrichment to customize generic templates with specific context

Usage:
    from supervisor.remediation import generate_remediation

    result = generate_remediation(
        incident_type="oomkill",
        root_cause="memory leak in payment-svc",
        confidence=85,
        evidence_summary="Pod payment-svc-abc restarted 3 times ...",
        itsm_context={"rollback_plan": "revert PR #847"},
        devops_context={"deployment_version": "v2.3.1"},
    )
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

import yaml

from supervisor.llm import converse, is_enabled as llm_is_enabled

logger = logging.getLogger("sentinalai.remediation")

# ---------------------------------------------------------------------------
# Code-default remediation templates
# ---------------------------------------------------------------------------

REMEDIATION_TEMPLATES: dict[str, dict[str, Any]] = {
    "timeout": {
        "immediate_actions": [
            "Check downstream dependencies",
            "Increase timeout thresholds",
        ],
        "permanent_fix": [
            "Add circuit breakers",
            "Optimize slow queries",
        ],
        "risk_level": "medium",
        "verify_before_acting": False,
        "runbook_hint": "runbook/timeout-investigation.md",
    },
    "oomkill": {
        "immediate_actions": [
            "Restart pod",
            "Increase memory limits",
        ],
        "permanent_fix": [
            "Profile for memory leaks",
            "Implement heap dumps on OOM",
        ],
        "risk_level": "high",
        "verify_before_acting": True,
        "runbook_hint": "runbook/oomkill-response.md",
    },
    "error_spike": {
        "immediate_actions": [
            "Rollback last deployment",
            "Enable feature flag rollback",
        ],
        "permanent_fix": [
            "Improve test coverage",
            "Add canary deployments",
        ],
        "risk_level": "high",
        "verify_before_acting": True,
        "runbook_hint": "runbook/error-spike-response.md",
    },
    "latency": {
        "immediate_actions": [
            "Scale horizontally",
            "Check database connections",
        ],
        "permanent_fix": [
            "Optimize queries",
            "Add caching layer",
            "Review connection pool sizing",
        ],
        "risk_level": "medium",
        "verify_before_acting": False,
        "runbook_hint": "runbook/latency-investigation.md",
    },
    "saturation": {
        "immediate_actions": [
            "Scale up/out",
            "Kill runaway processes",
        ],
        "permanent_fix": [
            "Implement autoscaling",
            "Add resource quotas",
            "Right-size instances",
        ],
        "risk_level": "high",
        "verify_before_acting": True,
        "runbook_hint": "runbook/saturation-response.md",
    },
    "network": {
        "immediate_actions": [
            "Check DNS resolution",
            "Verify network policies",
            "Restart affected pods",
        ],
        "permanent_fix": [
            "Implement DNS caching",
            "Add network redundancy",
        ],
        "risk_level": "medium",
        "verify_before_acting": False,
        "runbook_hint": "runbook/network-troubleshooting.md",
    },
    "cascading": {
        "immediate_actions": [
            "Isolate failing service",
            "Activate circuit breakers",
        ],
        "permanent_fix": [
            "Add bulkheads",
            "Implement graceful degradation",
            "Review dependency graph",
        ],
        "risk_level": "critical",
        "verify_before_acting": True,
        "runbook_hint": "runbook/cascading-failure-response.md",
    },
    "missing_data": {
        "immediate_actions": [
            "Check data pipeline health",
            "Verify connectivity to data sources",
        ],
        "permanent_fix": [
            "Add data freshness monitoring",
            "Implement fallback data sources",
        ],
        "risk_level": "medium",
        "verify_before_acting": False,
        "runbook_hint": "runbook/missing-data-investigation.md",
    },
    "flapping": {
        "immediate_actions": [
            "Stabilize with fixed scaling",
            "Disable problematic health checks",
        ],
        "permanent_fix": [
            "Fix connection pool leaks",
            "Tune health check thresholds",
        ],
        "risk_level": "medium",
        "verify_before_acting": False,
        "runbook_hint": "runbook/flapping-service-response.md",
    },
    "silent_failure": {
        "immediate_actions": [
            "Restart stale pipeline jobs",
            "Invalidate caches",
        ],
        "permanent_fix": [
            "Add throughput alerting",
            "Implement data freshness SLOs",
        ],
        "risk_level": "high",
        "verify_before_acting": True,
        "runbook_hint": "runbook/silent-failure-response.md",
    },
}

# All valid incident types (matches tool_selector.py playbooks)
VALID_INCIDENT_TYPES = set(REMEDIATION_TEMPLATES.keys())

# ---------------------------------------------------------------------------
# YAML override loading
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_YAML_PATH = _PROJECT_ROOT / "remediation_templates.yaml"


def _load_yaml_overrides(yaml_path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load YAML overrides and merge with code defaults.

    Looks for ``remediation_templates.yaml`` in the project root (or the
    supplied *yaml_path*).  YAML values override code defaults on a
    per-field basis within each incident type.

    Returns:
        Merged template dict.  If the YAML file is missing or malformed
        the code defaults are returned silently.
    """
    path = yaml_path or _YAML_PATH
    templates = copy.deepcopy(REMEDIATION_TEMPLATES)

    if not path.exists():
        logger.debug("No YAML override file at %s — using code defaults", path)
        return templates

    try:
        with open(path, "r") as fh:
            raw = yaml.safe_load(fh)
    except Exception as exc:
        logger.warning("Failed to parse YAML overrides at %s: %s — using code defaults", path, exc)
        return templates

    if not isinstance(raw, dict):
        logger.warning("YAML override file is not a mapping — using code defaults")
        return templates

    overrides = raw.get("overrides", {})
    if not isinstance(overrides, dict):
        logger.warning("YAML 'overrides' key is not a mapping — using code defaults")
        return templates

    for incident_type, fields in overrides.items():
        if not isinstance(fields, dict):
            logger.warning("Skipping non-dict override for %s", incident_type)
            continue

        if incident_type not in templates:
            # New type added via YAML — seed with empty scaffold
            logger.info("YAML adds new incident type: %s", incident_type)
            templates[incident_type] = {
                "immediate_actions": [],
                "permanent_fix": [],
                "risk_level": "medium",
                "verify_before_acting": False,
                "runbook_hint": "",
            }

        for field, value in fields.items():
            old = templates[incident_type].get(field, "<unset>")
            templates[incident_type][field] = value
            logger.info(
                "YAML override: %s.%s changed from %s to %s",
                incident_type, field, old, value,
            )

    return templates


# ---------------------------------------------------------------------------
# LLM enrichment
# ---------------------------------------------------------------------------

_ENRICHMENT_SYSTEM_PROMPT = """\
You are an expert SRE remediation advisor. You are given a generic remediation \
template for an incident type, plus the specific root cause, evidence, and \
(optionally) ITSM and DevOps context from the investigation.

Your job is to CUSTOMIZE the generic template with specific, actionable guidance \
that references real service names, deployment versions, PR numbers, rollback \
plans, and any other concrete details available in the context.

Rules:
- Keep the same JSON schema as the input template.
- Replace generic actions with specific ones where evidence supports it.
- Do NOT invent facts — only use information present in the context.
- If ITSM context includes a rollback plan, incorporate it into immediate_actions.
- If DevOps context includes deployment version or PR number, reference them.
- Preserve the risk_level and verify_before_acting from the template unless \
  the evidence clearly warrants a change.
- Return ONLY valid JSON. No markdown fences, no explanation outside the JSON.

Output JSON schema:
{
    "immediate_actions": ["string", ...],
    "permanent_fix": ["string", ...],
    "risk_level": "low" | "medium" | "high" | "critical",
    "verify_before_acting": bool,
    "runbook_hint": "string"
}
"""


def enrich_remediation_llm(
    template: dict[str, Any],
    root_cause: str,
    evidence_summary: str,
    itsm_context: dict[str, Any] | None = None,
    devops_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Enrich a remediation template using LLM with investigation context.

    If the LLM call fails for any reason the original *template* is returned
    unchanged (graceful degradation).

    Returns:
        Enriched template dict matching the same schema as the input.
    """
    if not llm_is_enabled():
        logger.debug("LLM disabled — returning template as-is")
        return template

    user_message_parts = [
        f"Root cause: {root_cause}",
        f"\nEvidence summary:\n{evidence_summary}",
    ]
    if itsm_context:
        user_message_parts.append(f"\nITSM context:\n{json.dumps(itsm_context, indent=2)}")
    if devops_context:
        user_message_parts.append(f"\nDevOps context:\n{json.dumps(devops_context, indent=2)}")

    user_message_parts.append(f"\nGeneric template to customize:\n{json.dumps(template, indent=2)}")

    user_message = "\n".join(user_message_parts)

    try:
        result = converse(
            system_prompt=_ENRICHMENT_SYSTEM_PROMPT,
            user_message=user_message,
            temperature=0.0,
        )

        if result.get("error") or not result.get("text"):
            logger.warning("LLM enrichment returned error or empty text — using template")
            return template

        text = result["text"].strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            # Remove opening fence (possibly ```json)
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3].rstrip()

        enriched = json.loads(text)

        # Validate that required keys exist
        required_keys = {"immediate_actions", "permanent_fix", "risk_level",
                         "verify_before_acting", "runbook_hint"}
        if not required_keys.issubset(enriched.keys()):
            logger.warning("LLM enrichment missing required keys — using template")
            return template

        # Validate risk_level value
        if enriched["risk_level"] not in ("low", "medium", "high", "critical"):
            logger.warning("LLM enrichment returned invalid risk_level — using template")
            return template

        logger.info("LLM enrichment successful — customized %d immediate actions, %d permanent fixes",
                     len(enriched.get("immediate_actions", [])),
                     len(enriched.get("permanent_fix", [])))
        return enriched

    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        logger.warning("LLM enrichment parse error: %s — using template", exc)
        return template
    except Exception as exc:
        logger.warning("LLM enrichment unexpected error: %s — using template", exc)
        return template


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_remediation(
    incident_type: str,
    root_cause: str,
    confidence: float,
    evidence_summary: str,
    itsm_context: dict[str, Any] | None = None,
    devops_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate structured remediation guidance for an incident.

    Args:
        incident_type: One of the 10 known incident types (e.g. "oomkill").
        root_cause: The identified root cause string from the RCA.
        confidence: Investigation confidence score (0-100).
        evidence_summary: Human-readable summary of evidence gathered.
        itsm_context: Optional ITSM data (ServiceNow change records, rollback plans).
        devops_context: Optional DevOps data (PR numbers, deployment versions).

    Returns:
        Structured remediation dict with keys: immediate_actions, permanent_fix,
        risk_level, confidence, verify_before_acting, warnings, source,
        runbook_hint.
    """
    # Load templates (code defaults + YAML overrides)
    templates = _load_yaml_overrides()

    # Look up template; fall back to error_spike for unknown types
    if incident_type in templates:
        template = copy.deepcopy(templates[incident_type])
    else:
        logger.warning(
            "Unknown incident type '%s' — falling back to error_spike template",
            incident_type,
        )
        template = copy.deepcopy(templates.get("error_spike", {}))

    # Attempt LLM enrichment
    source = "template_only"
    if llm_is_enabled():
        enriched = enrich_remediation_llm(
            template=template,
            root_cause=root_cause,
            evidence_summary=evidence_summary,
            itsm_context=itsm_context,
            devops_context=devops_context,
        )
        if enriched is not template:
            source = "template+llm"
            template = enriched

    # Build warnings
    warnings: list[str] = []

    if confidence < 50:
        warnings.append(
            "Low confidence RCA - verify root cause before acting on remediation"
        )

    risk_level = template.get("risk_level", "medium")
    verify_before_acting = template.get("verify_before_acting", False)

    # Enforce verify_before_acting for high/critical risk
    if risk_level in ("high", "critical"):
        verify_before_acting = True
        warnings.append(
            f"VERIFY BEFORE ACTING: risk level is {risk_level} — "
            f"confirm root cause and review rollback plan before executing remediation"
        )

    return {
        "immediate_actions": template.get("immediate_actions", []),
        "permanent_fix": template.get("permanent_fix", []),
        "risk_level": risk_level,
        "confidence": confidence,
        "verify_before_acting": verify_before_acting,
        "warnings": warnings,
        "source": source,
        "runbook_hint": template.get("runbook_hint", ""),
    }
