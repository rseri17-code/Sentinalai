"""LLM-as-judge deep eval scorer for SentinalAI.

Uses Amazon Bedrock (or any compatible LLM endpoint) to score agent
investigation outputs across multiple quality dimensions. Results are
emitted as OTEL metrics via record_eval_score().

Bedrock config is generic — no org-specific endpoints. Set these env
vars to connect:
    AWS_REGION              - Bedrock region (default: us-east-1)
    BEDROCK_MODEL_ID        - Model for judge (default: anthropic.claude-sonnet-4-5-20250929-v1:0)
    EVAL_JUDGE_MODEL_ID     - Override model specifically for eval judge
    AWS_ACCESS_KEY_ID       - Standard AWS credentials
    AWS_SECRET_ACCESS_KEY
    AWS_SESSION_TOKEN       - (optional, for assumed roles)

Falls back gracefully to rule-based scoring when Bedrock is unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

BEDROCK_TIMEOUT_SECONDS = 30

logger = logging.getLogger("sentinalai.eval.judge")

# =========================================================================
# Generic Bedrock client (no org-specific endpoints)
# =========================================================================

_bedrock_client = None


def _get_bedrock_client():
    """Lazy-init a Bedrock Runtime client. Returns None if unavailable."""
    global _bedrock_client
    if _bedrock_client is not None:
        return _bedrock_client

    try:
        import boto3
        from botocore.config import Config

        region = os.environ.get("AWS_REGION", "us-east-1")
        _bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(
                read_timeout=BEDROCK_TIMEOUT_SECONDS,
                connect_timeout=10,
                retries={"max_attempts": 2, "mode": "adaptive"},
            ),
        )
        logger.info("Bedrock client initialized (region=%s)", region)
        return _bedrock_client
    except Exception as e:
        logger.debug("Bedrock unavailable, LLM-as-judge disabled: %s", e)
        return None


def _get_judge_model_id() -> str:
    """Return the model ID for the judge LLM."""
    return os.environ.get(
        "EVAL_JUDGE_MODEL_ID",
        os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-5-20250929-v1:0"),
    )


# =========================================================================
# LLM-as-judge prompt templates
# =========================================================================

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
# Core judge function
# =========================================================================

def llm_judge_score(
    incident_id: str,
    expected: dict,
    result: dict,
) -> dict[str, float] | None:
    """Score an investigation result using LLM-as-judge via Bedrock.

    Returns dict of {dimension: score} or None if Bedrock unavailable.
    """
    client = _get_bedrock_client()
    if client is None:
        return None

    model_id = _get_judge_model_id()
    user_prompt = _build_judge_prompt(incident_id, expected, result)

    try:
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "temperature": 0.0,
            "system": JUDGE_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_prompt}],
        })

        response = client.invoke_model(
            modelId=model_id,
            contentType="application/json",
            accept="application/json",
            body=body,
        )

        response_body = json.loads(response["body"].read())
        content = response_body.get("content", [{}])[0].get("text", "")

        # Strip markdown code fences if LLM wraps JSON in ```json ... ```
        content = content.strip()
        md_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", content, re.DOTALL)
        if md_match:
            content = md_match.group(1).strip()

        scores = json.loads(content)

        return {
            dim: scores[dim]["score"]
            for dim in [
                "root_cause_accuracy",
                "causal_reasoning",
                "evidence_usage",
                "timeline_quality",
                "actionability",
                "overall",
            ]
            if dim in scores and "score" in scores[dim]
        }

    except Exception as e:
        logger.warning("LLM-as-judge failed for %s: %s", incident_id, e)
        return None


# =========================================================================
# Batch scoring with OTEL metrics emission
# =========================================================================

def judge_and_record(
    incident_id: str,
    incident_type: str,
    expected: dict,
    result: dict,
) -> dict[str, float]:
    """Run LLM-as-judge and emit scores as OTEL metrics.

    Falls back to rule-based scores if Bedrock is unavailable.
    Returns the scores dict regardless of source.
    """
    from supervisor.eval_metrics import record_eval_score

    # Try LLM-as-judge first
    scores = llm_judge_score(incident_id, expected, result)
    source = "llm_judge"

    if scores is None:
        # Fallback: rule-based scoring (imported from eval runner)
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

    return scores


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
