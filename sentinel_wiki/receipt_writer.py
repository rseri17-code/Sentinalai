"""Write RCA investigation receipts to sentinel_wiki/receipts/.

Every completed investigation produces a receipt — a permanent, structured
record of what happened, what was found, and what confidence the agent had.
Receipts are the primary write path that grows the wiki over time.

Obsidian-compatible Markdown with YAML front matter.
Safe to call non-blocking from _persist_results; never raises.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("sentinalai.wiki.receipt_writer")


def root_cause_hash(root_cause: str) -> str:
    """Stable 8-char hash of a normalized root cause string."""
    normalized = root_cause.lower().strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:8]


def write_receipt(
    incident_id: str,
    service: str,
    incident_type: str,
    result: dict,
    evidence: dict | None = None,
    base_path: str = "sentinel_wiki",
) -> str | None:
    """Write an RCA receipt for a completed investigation.

    Args:
        incident_id:   Incident identifier.
        service:       Service under investigation.
        incident_type: Incident type (error_spike, timeout, etc.).
        result:        Full result dict from the investigation.
        evidence:      Evidence dict (optional — used for signal extraction).
        base_path:     sentinel_wiki root directory.

    Returns:
        Path to the written receipt, or None on failure.
    """
    try:
        return _write(incident_id, service, incident_type, result, evidence or {}, base_path)
    except Exception as exc:
        logger.warning("wiki.receipt_writer: write_receipt failed for %s: %s", incident_id, exc)
        return None


def _write(
    incident_id: str,
    service: str,
    incident_type: str,
    result: dict,
    evidence: dict,
    base_path: str,
) -> str:
    root = Path(base_path)
    receipts_dir = root / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).isoformat()
    date_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"{incident_id}_{date_tag}.md"
    # If same incident re-ingested same day, overwrite
    receipt_path = receipts_dir / filename

    root_cause = result.get("root_cause", "unknown")
    confidence = result.get("confidence", 0)
    quality = result.get("online_quality_score", result.get("_online_quality_score", 0.0))
    rc_hash = root_cause_hash(root_cause)
    evidence_keys = result.get("evidence_keys", [])
    if not evidence_keys:
        evidence_keys = list(evidence.keys())[:20]

    tags = ["receipt", incident_type, service]
    if quality >= 0.70:
        tags.append("high-quality")
    elif quality < 0.50:
        tags.append("low-quality")

    # Evidence signals (lightweight extraction)
    signals = _extract_evidence_signals(evidence)

    front_matter = (
        f"---\n"
        f"receipt_id: {incident_id}_{date_tag}\n"
        f"incident_id: {incident_id}\n"
        f"service: {service}\n"
        f"incident_type: {incident_type}\n"
        f"root_cause_hash: {rc_hash}\n"
        f"confidence: {confidence}\n"
        f"quality_score: {round(float(quality), 4)}\n"
        f"evidence_keys: {json.dumps(evidence_keys[:20])}\n"
        f"verified: false\n"
        f"timestamp: {now}\n"
        f"related_patterns: []\n"
        f"tags: {json.dumps(tags)}\n"
        f"status: auto-generated\n"
        f"---\n"
    )

    # Reasoning excerpt (first 400 chars)
    reasoning = result.get("reasoning", result.get("summary", ""))
    reasoning_excerpt = (reasoning[:400] + "...") if len(reasoning) > 400 else reasoning

    body = f"""# RCA Receipt: {incident_id}

## Root Cause
{root_cause}

## Confidence & Quality
- Stated confidence: {confidence}%
- Investigation quality: {round(float(quality), 3)}

## Evidence Gathered
{_bullet(evidence_keys[:20])}

## Signals
{_bullet(signals) if signals else "_None extracted_"}

## Reasoning Excerpt
{reasoning_excerpt or "_Not available_"}

## Resolution
<!-- Filled in after fix verification -->

## Pattern Links
<!-- Auto-populated by pattern_promoter -->

## Related Notes
<!-- Cross-links to wiki notes and decisions -->

## Source Reference
Incident: {incident_id}
Service: {service}
Type: {incident_type}
Root cause hash: {rc_hash}
Written: {now}
"""

    receipt_path.write_text(front_matter + "\n" + body)
    logger.debug("wiki.receipt_writer: wrote %s", receipt_path)
    return str(receipt_path)


def _extract_evidence_signals(evidence: dict) -> list[str]:
    signals: list[str] = []
    for key, val in evidence.items():
        if not isinstance(val, dict):
            continue
        # Golden signals
        for metric in ("error_rate", "latency_p99", "rps", "saturation"):
            v = val.get(metric)
            if v is not None:
                signals.append(f"{metric}={v}")
        # Error counts
        for field in ("total_errors", "error_count", "anomalies"):
            v = val.get(field)
            if v is not None and v != 0:
                signals.append(f"{key}.{field}={v}")
        if len(signals) >= 12:
            break
    return signals[:12]


def _bullet(items: list) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "_None_"
