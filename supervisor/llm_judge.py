"""LLM-as-judge deep eval scorer for SentinalAI.

Uses the Bedrock Converse API (via supervisor.llm) to score agent
investigation outputs across multiple quality dimensions. Results are
emitted as OTEL metrics via record_eval_score().

Dimensions scored (each 0.0 to 1.0):
  - root_cause_accuracy:  Does the root cause match the evidence/expected?
  - causal_reasoning:     Is the causal chain clear and complete?
  - evidence_usage:       Are claims backed by specific evidence data?
  - timeline_quality:     Is the timeline ordered and progression clear?
  - actionability:        Could an SRE act on this analysis?

Falls back gracefully to rule-based scoring when Bedrock is unavailable.
All calls emit GenAI semantic convention attributes for OTEL tracing.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from supervisor.llm import converse, is_enabled as llm_is_enabled

logger = logging.getLogger("sentinalai.eval.judge")

# Judge can use a separate model for cost optimisation
JUDGE_MODEL_ID = os.environ.get("EVAL_JUDGE_MODEL_ID", "")

JUDGE_DIMENSIONS = [
    "root_cause_accuracy",
    "causal_reasoning",
    "evidence_usage",
    "timeline_quality",
    "actionability",
    "overall",
]

JUDGE_SYSTEM_PROMPT = """\
You are an expert SRE evaluating the quality of an automated incident \
investigation. Score each dimension from 0.0 to 1.0 with brief justification. \
Return ONLY valid JSON matching the schema below. No markdown, no explanation \
outside the JSON.

Schema:
{
  "root_cause_accuracy": {"score": float, "reason": str},
  "causal_reasoning": {"score": float, "reason": str},
  "evidence_usage": {"score": float, "reason": str},
  "timeline_quality": {"score": float, "reason": str},
  "actionability": {"score": float, "reason": str},
  "overall": {"score": float, "reason": str}
}

Scoring guide:
- root_cause_accuracy: Does the root cause match the expected answer? \
  Are the right services, components, and failure modes identified?
- causal_reasoning: Does the reasoning explain WHY the failure happened? \
  Is there a clear causal chain from trigger to symptom?
- evidence_usage: Does the reasoning cite specific evidence (logs, metrics, \
  signals, changes)? Are claims backed by data?
- timeline_quality: Is the timeline ordered correctly? Does it show \
  progression from cause to effect?
- actionability: Could an SRE act on this analysis? Are next steps clear?
- overall: Weighted average considering all dimensions."""


def _build_judge_prompt(
    incident_id: str,
    expected: dict,
    result: dict,
) -> str:
    """Build the user prompt for the judge LLM."""
    return f"""\
Evaluate this automated investigation result.

## Expected Root Cause
{expected.get('root_cause', 'N/A')}

## Expected Keywords
{', '.join(expected.get('root_cause_keywords', []))}

## Expected Confidence Range
{expected.get('confidence_min', '?')} - {expected.get('confidence_max', '?')}

## Agent Output
- Incident ID: {incident_id}
- Root Cause: {result.get('root_cause', 'N/A')}
- Confidence: {result.get('confidence', 0)}
- Reasoning: {result.get('reasoning', 'N/A')}
- Timeline entries: {len(result.get('evidence_timeline', []))}
- Timeline: {json.dumps(result.get('evidence_timeline', [])[:10], indent=2, default=str)}

Score each dimension 0.0-1.0."""


# =========================================================================
# Core judge function (uses Converse API)
# =========================================================================

def llm_judge_score(
    incident_id: str,
    expected: dict,
    result: dict,
) -> dict[str, Any] | None:
    """Score an investigation result using LLM-as-judge via Bedrock Converse.

    Returns dict with:
        scores: {dimension: score} mapping
        input_tokens, output_tokens, latency_ms: GenAI usage metrics
    Or None if LLM unavailable.
    """
    if not llm_is_enabled():
        return None

    user_prompt = _build_judge_prompt(incident_id, expected, result)

    llm_result = converse(
        JUDGE_SYSTEM_PROMPT,
        user_prompt,
        model_id=JUDGE_MODEL_ID or None,
        temperature=0.0,
        max_tokens=1024,
    )

    if llm_result.get("error") or not llm_result.get("text"):
        logger.warning("LLM-as-judge failed for %s: %s", incident_id, llm_result.get("error", "empty"))
        return None

    content = llm_result["text"].strip()

    # Strip markdown code fences if LLM wraps JSON in ```json ... ```
    md_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", content, re.DOTALL)
    if md_match:
        content = md_match.group(1).strip()

    try:
        parsed = json.loads(content)
        scores = {}
        for dim in JUDGE_DIMENSIONS:
            if dim in parsed and "score" in parsed[dim]:
                scores[dim] = parsed[dim]["score"]
        return {
            "scores": scores,
            "input_tokens": llm_result.get("input_tokens", 0),
            "output_tokens": llm_result.get("output_tokens", 0),
            "latency_ms": llm_result.get("latency_ms", 0),
            "model_id": llm_result.get("model_id", ""),
        }
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        logger.warning("Failed to parse judge response for %s: %s", incident_id, exc)
        return None


# =========================================================================
# Batch scoring with OTEL metrics emission
# =========================================================================

def judge_and_record(
    incident_id: str,
    incident_type: str,
    expected: dict,
    result: dict,
) -> dict[str, Any]:
    """Run LLM-as-judge and emit scores as OTEL metrics.

    Falls back to rule-based scores if Bedrock is unavailable.
    Returns dict with scores, source, and GenAI usage metrics.
    """
    from supervisor.eval_metrics import record_eval_score, record_llm_usage

    # Try LLM-as-judge first
    judge_result = llm_judge_score(incident_id, expected, result)
    source = "llm_judge"

    if judge_result is not None:
        scores = judge_result["scores"]
        # Record GenAI usage metrics for the judge call
        record_llm_usage(
            operation="judge",
            model_id=judge_result.get("model_id", ""),
            input_tokens=judge_result.get("input_tokens", 0),
            output_tokens=judge_result.get("output_tokens", 0),
            latency_ms=judge_result.get("latency_ms", 0),
            incident_type=incident_type,
        )
    else:
        # Fallback: rule-based scoring
        source = "rule_based"
        scores = _rule_based_fallback(incident_id, expected, result)

    # Emit all scores as OTEL metrics
    for dimension, score in scores.items():
        record_eval_score(
            incident_id=incident_id,
            incident_type=incident_type,
            dimension=f"{source}.{dimension}",
            score=score,
        )

    logger.info(
        "eval judge: %s scored %s (source=%s, overall=%.2f)",
        incident_id,
        {k: round(v, 2) for k, v in scores.items()},
        source,
        scores.get("overall", 0),
    )

    return {
        "scores": scores,
        "source": source,
        "input_tokens": judge_result.get("input_tokens", 0) if judge_result else 0,
        "output_tokens": judge_result.get("output_tokens", 0) if judge_result else 0,
        "latency_ms": judge_result.get("latency_ms", 0) if judge_result else 0,
    }


def _rule_based_fallback(
    incident_id: str,
    expected: dict,
    result: dict,
) -> dict[str, float]:
    """Simple rule-based scoring when LLM judge is unavailable."""
    root_cause = result.get("root_cause", "").lower()
    keywords = expected.get("root_cause_keywords", [])
    reasoning = result.get("reasoning", "").lower()
    timeline = result.get("evidence_timeline", [])
    confidence = result.get("confidence", 0)

    # Root cause accuracy: keyword match fraction
    kw_matched = sum(1 for kw in keywords if kw.lower() in root_cause) if keywords else 0
    accuracy = kw_matched / len(keywords) if keywords else 1.0

    # Confidence calibration
    lo = expected.get("confidence_min", 0)
    hi = expected.get("confidence_max", 100)
    if lo <= confidence <= hi:
        calibration = 1.0
    elif confidence < lo:
        calibration = max(0.0, 1.0 - (lo - confidence) / 100)
    else:
        calibration = max(0.0, 1.0 - (confidence - hi) / 100)

    # Causal reasoning: presence of causal language
    causal_terms = ["caused", "led to", "resulted", "because", "due to", "triggered"]
    causal = min(1.0, sum(1 for t in causal_terms if t in reasoning) / 2)

    # Evidence usage: reasoning length + keyword references
    evidence_score = min(1.0, len(reasoning) / 500) * 0.5
    evidence_score += min(0.5, sum(1 for kw in keywords if kw.lower() in reasoning) / max(len(keywords), 1) * 0.5)

    # Timeline quality
    timeline_score = min(1.0, len(timeline) / 3) if timeline else 0.0

    # Actionability: mentions specific services/actions
    action_terms = ["service", "restart", "rollback", "investigate", "deploy", "config", "scale"]
    actionability = min(1.0, sum(1 for t in action_terms if t in reasoning) / 2)

    overall = (accuracy + calibration + causal + evidence_score + timeline_score + actionability) / 6

    return {
        "root_cause_accuracy": round(accuracy, 3),
        "causal_reasoning": round(causal, 3),
        "evidence_usage": round(evidence_score, 3),
        "timeline_quality": round(timeline_score, 3),
        "actionability": round(actionability, 3),
        "overall": round(overall, 3),
    }
