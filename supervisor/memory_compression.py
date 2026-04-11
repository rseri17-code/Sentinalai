"""Memory compression for SentinalAI.

Compresses multi-turn investigation context into a semantic digest before
long-term storage. This solves two problems:

  1. Context window exhaustion: large investigations produce thousands of tokens
     of evidence + reasoning that can't fit in a single LLM call for future
     similarity search.

  2. Signal dilution: raw turns include tool call noise (pagination artifacts,
     repeated retries) that reduces retrieval precision.

Compression strategy:
  - Extract structural fields (root_cause, confidence, evidence keys, timeline)
  - LLM-summarise the reasoning (if LLM available) or use extractive fallback
  - Return a flat dict that fits in ~500 tokens

Configuration:
  MEMORY_COMPRESSION_ENABLED     — on/off (default: true)
  MEMORY_COMPRESSION_MAX_TOKENS  — max tokens for reasoning summary (default: 200)
  MEMORY_COMPRESS_AFTER_TURNS    — compress STM when N turns reached (default: 20)
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, asdict
from typing import Any

logger = logging.getLogger("sentinalai.memory_compression")

COMPRESSION_ENABLED = os.environ.get(
    "MEMORY_COMPRESSION_ENABLED", "true"
).lower() in ("1", "true", "yes")

MAX_SUMMARY_TOKENS = int(os.environ.get("MEMORY_COMPRESSION_MAX_TOKENS", "200"))
COMPRESS_AFTER_TURNS = int(os.environ.get("MEMORY_COMPRESS_AFTER_TURNS", "20"))

# Approx chars per token (conservative)
_CHARS_PER_TOKEN = 4


@dataclass
class InvestigationDigest:
    """Compressed representation of a completed investigation."""

    incident_id: str
    incident_type: str
    service: str
    root_cause: str
    confidence: int
    evidence_keys: list[str]
    source_count: int
    citation_coverage: float
    reasoning_digest: str          # compressed reasoning (≤200 tokens)
    timeline_summary: str          # first + last timeline events
    fix_proposed: bool
    fix_type: str                  # rollback | code_fix | none
    quality_score: float
    compressed_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_ltm_text(self) -> str:
        """Single-string representation for LTM indexing and semantic search."""
        return (
            f"Incident: {self.incident_id} | Service: {self.service} | "
            f"Type: {self.incident_type} | RootCause: {self.root_cause} | "
            f"Confidence: {self.confidence}% | Evidence: {', '.join(self.evidence_keys[:6])} | "
            f"Summary: {self.reasoning_digest}"
        )


def compress_investigation(
    incident_id: str,
    incident_type: str,
    service: str,
    result: dict[str, Any],
    online_quality_score: float = 0.0,
) -> InvestigationDigest:
    """Compress a completed investigation result into a memory digest.

    Args:
        incident_id: The incident identifier
        incident_type: Classified type (timeout, oomkill, etc.)
        service: Affected service name
        result: Full investigation result dict from agent.investigate()
        online_quality_score: Quality score from online_evaluator (0–1)

    Returns:
        InvestigationDigest — compact representation ready for LTM storage
    """
    root_cause = result.get("root_cause", "")
    confidence = int(result.get("confidence", 0))
    reasoning = result.get("reasoning", "")
    timeline = result.get("evidence_timeline", [])
    evidence_keys = _extract_evidence_keys(result)
    citation_coverage = float(result.get("citation_coverage", 0.0))

    # Proposed fix metadata
    proposed_fix = result.get("proposed_fix") or {}
    fix_proposed = bool(proposed_fix)
    fix_type = proposed_fix.get("fix_type", "none") if isinstance(proposed_fix, dict) else "none"

    # Count unique evidence sources
    citations = result.get("citations", [])
    source_count = len({c.get("source") for c in citations if c.get("source")}) if citations else 0

    # Compress reasoning
    reasoning_digest = _compress_reasoning(reasoning, root_cause, incident_type)

    # Timeline summary: first + last events
    timeline_summary = _summarise_timeline(timeline)

    return InvestigationDigest(
        incident_id=incident_id,
        incident_type=incident_type,
        service=service,
        root_cause=root_cause,
        confidence=confidence,
        evidence_keys=evidence_keys[:10],
        source_count=source_count,
        citation_coverage=citation_coverage,
        reasoning_digest=reasoning_digest,
        timeline_summary=timeline_summary,
        fix_proposed=fix_proposed,
        fix_type=fix_type,
        quality_score=round(online_quality_score, 3),
        compressed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


def compress_turns(turns: list[dict[str, Any]]) -> str:
    """Compress a list of STM turns into a single context string.

    Used when STM grows beyond COMPRESS_AFTER_TURNS to avoid context
    window exhaustion in the next LLM call.

    Returns:
        A single string summarising all turns (≤ MAX_SUMMARY_TOKENS tokens)
    """
    if not turns:
        return ""

    if not COMPRESSION_ENABLED:
        # Simple truncation fallback
        all_text = " | ".join(
            f"[{t.get('role','?')}]: {str(t.get('content',''))[:100]}"
            for t in turns[-5:]
        )
        return all_text[:MAX_SUMMARY_TOKENS * _CHARS_PER_TOKEN]

    # Try LLM summarisation
    full_text = "\n".join(
        f"{t.get('role','?').upper()}: {t.get('content','')}"
        for t in turns
    )
    summary = _llm_summarise(
        text=full_text,
        instruction=(
            "Summarise this incident investigation conversation in 3-5 sentences. "
            "Keep: service name, root cause hypothesis, evidence gathered, confidence level. "
            "Omit: tool call boilerplate, repeated queries, error noise."
        ),
        max_chars=MAX_SUMMARY_TOKENS * _CHARS_PER_TOKEN,
    )
    return summary or _extractive_fallback(full_text)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compress_reasoning(reasoning: str, root_cause: str, incident_type: str) -> str:
    """Compress the full reasoning into a short digest."""
    if not reasoning:
        return root_cause[:200] if root_cause else ""

    max_chars = MAX_SUMMARY_TOKENS * _CHARS_PER_TOKEN

    if len(reasoning) <= max_chars:
        return reasoning

    # Try LLM first
    summary = _llm_summarise(
        text=f"Root cause: {root_cause}\n\nReasoning: {reasoning}",
        instruction=(
            f"Summarise this {incident_type} incident RCA reasoning in 2-3 sentences. "
            "Keep: the causal chain, key evidence, confidence drivers. "
            "Omit: repetition, hedging language."
        ),
        max_chars=max_chars,
    )
    return summary or _extractive_fallback(reasoning, max_chars=max_chars)


def _llm_summarise(text: str, instruction: str, max_chars: int = 800) -> str:
    """Call LLM to produce a compressed summary. Returns '' on any failure."""
    if not COMPRESSION_ENABLED:
        return ""
    try:
        from supervisor.llm import converse, _llm_enabled
        if not _llm_enabled():
            return ""

        resp = converse(
            system_prompt=instruction,
            user_message=text[:4000],  # cap input
            max_tokens=MAX_SUMMARY_TOKENS,
        )
        content = resp.get("content", "") if isinstance(resp, dict) else str(resp)
        return content[:max_chars].strip()
    except Exception as exc:
        logger.debug("LLM summarisation failed (non-critical): %s", exc)
        return ""


def _extractive_fallback(text: str, max_chars: int = 800) -> str:
    """Extractive summary: keep sentences containing key signal words."""
    key_signals = [
        "root cause", "because", "caused by", "due to", "connection",
        "memory", "timeout", "deploy", "error", "spike", "exhausted",
        "confidence", "evidence", "rollback",
    ]
    sentences = re.split(r"[.!?]\s+", text)
    scored = []
    for s in sentences:
        score = sum(1 for kw in key_signals if kw.lower() in s.lower())
        if score > 0:
            scored.append((score, s.strip()))

    # Take top sentences by score until we hit max_chars
    scored.sort(key=lambda x: -x[0])
    result_parts: list[str] = []
    total = 0
    for _, sentence in scored:
        if total + len(sentence) > max_chars:
            break
        result_parts.append(sentence)
        total += len(sentence)

    return ". ".join(result_parts) if result_parts else text[:max_chars]


def _extract_evidence_keys(result: dict) -> list[str]:
    """Extract unique evidence source keys from the result."""
    keys: list[str] = []
    # Citations sources
    for citation in result.get("citations", []):
        src = citation.get("source", "")
        if src and src not in keys:
            keys.append(src)
    # Timeline sources
    for event in result.get("evidence_timeline", []):
        src = event.get("source", event.get("worker", ""))
        if src and src not in keys:
            keys.append(src)
    return keys


def _summarise_timeline(timeline: list) -> str:
    """Return a one-line summary of the investigation timeline."""
    if not timeline:
        return ""
    events = [str(e.get("event") or e.get("message") or e) for e in timeline]
    if len(events) <= 2:
        return " → ".join(events)
    return f"{events[0]} → ... ({len(events)} events) → {events[-1]}"
