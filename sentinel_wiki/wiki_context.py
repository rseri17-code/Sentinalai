"""Query the wiki for context relevant to an ongoing investigation.

Called during pre-flight (agent_harness._load_meta_state) to give the agent
prior knowledge before it starts investigating. This closes the read loop:
receipts are written after every investigation and read back before the next.

Returns structured context: recent receipts for the service, matching patterns
for the incident type, and relevant wiki notes.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sentinel_wiki.pattern_promoter import _load_pattern, _parse_front_matter
from sentinel_wiki import indexer

logger = logging.getLogger("sentinalai.wiki.wiki_context")

_MAX_RECEIPTS = 5      # how many recent receipts to surface
_MAX_PATTERNS = 3      # how many patterns to surface
_MAX_NOTES = 3         # how many wiki notes to surface


def get_context(
    service: str,
    incident_type: str,
    base_path: str = "sentinel_wiki",
) -> dict:
    """Return wiki context for an investigation pre-flight.

    Args:
        service:       Service being investigated.
        incident_type: Incident type (error_spike, timeout, etc.).
        base_path:     sentinel_wiki root directory.

    Returns:
        Dict with: recent_receipts, matching_patterns, related_notes,
                   context_summary (plain text for agent injection).
    """
    try:
        return _get_context(service, incident_type, base_path)
    except Exception as exc:
        logger.debug("wiki.wiki_context: get_context failed: %s", exc)
        return _empty()


def _get_context(service: str, incident_type: str, base_path: str) -> dict:
    root = Path(base_path)
    receipts_dir = root / "receipts"
    patterns_dir = root / "patterns"
    indexes_dir = root / "indexes"

    # 1. Recent receipts for this service
    recent_receipts = _find_receipts(receipts_dir, service, incident_type)

    # 2. Matching patterns
    matching_patterns = _find_patterns(patterns_dir, service, incident_type)

    # 3. Related wiki notes (via entity/tag index)
    related_notes = _find_wiki_notes(indexes_dir, service, incident_type)

    # 4. Plain-text summary for agent injection
    summary = _build_summary(service, incident_type, recent_receipts, matching_patterns)

    return {
        "recent_receipts": recent_receipts,
        "matching_patterns": matching_patterns,
        "related_notes": related_notes,
        "context_summary": summary,
        "receipt_count": len(recent_receipts),
        "pattern_count": len(matching_patterns),
    }


def _find_receipts(receipts_dir: Path, service: str, incident_type: str) -> list[dict]:
    if not receipts_dir.exists():
        return []

    matches = []
    for f in sorted(receipts_dir.glob("*.md"), reverse=True):
        if f.name == "README.md":
            continue
        fm = _parse_front_matter(f.read_text(errors="replace"))
        if not fm:
            continue
        if fm.get("service") == service or fm.get("incident_type") == incident_type:
            matches.append({
                "receipt_id": fm.get("receipt_id", f.stem),
                "incident_id": fm.get("incident_id", ""),
                "service": fm.get("service", ""),
                "incident_type": fm.get("incident_type", ""),
                "root_cause": str(fm.get("root_cause", ""))[:120],
                "confidence": fm.get("confidence", 0),
                "quality_score": fm.get("quality_score", 0.0),
                "timestamp": fm.get("timestamp", ""),
                "root_cause_hash": fm.get("root_cause_hash", ""),
            })
        if len(matches) >= _MAX_RECEIPTS:
            break

    return matches


def _find_patterns(patterns_dir: Path, service: str, incident_type: str) -> list[dict]:
    if not patterns_dir.exists():
        return []

    matches = []
    for f in sorted(patterns_dir.glob("*.yaml")):
        pattern = _load_pattern(f)
        if not pattern:
            continue
        services = pattern.get("services", [])
        types = pattern.get("incident_types", [])
        if service in services or incident_type in types:
            matches.append({
                "pattern_id": pattern.get("pattern_id", f.stem),
                "root_cause_template": str(pattern.get("root_cause_template", ""))[:120],
                "observation_count": pattern.get("observation_count", 0),
                "services": services[:5],
                "incident_types": types[:5],
                "confidence_avg": pattern.get("confidence_avg", 0.0),
                "quality_avg": pattern.get("quality_avg", 0.0),
                "evidence_signals": pattern.get("evidence_signals", [])[:6],
            })
        if len(matches) >= _MAX_PATTERNS:
            break

    # Sort by observation_count descending
    matches.sort(key=lambda p: p["observation_count"], reverse=True)
    return matches[:_MAX_PATTERNS]


def _find_wiki_notes(indexes_dir: Path, service: str, incident_type: str) -> list[str]:
    notes: set[str] = set()
    try:
        ent_idx = indexer.load_entity_index(indexes_dir)
        for term in [service, incident_type]:
            for note in ent_idx.get(term, {}).get("notes", [])[:2]:
                notes.add(note)
        tag_idx = indexer.load_tag_index(indexes_dir)
        for note in tag_idx.get(incident_type, [])[:2]:
            notes.add(note)
    except Exception:
        pass
    return sorted(notes)[:_MAX_NOTES]


def _build_summary(
    service: str,
    incident_type: str,
    receipts: list[dict],
    patterns: list[dict],
) -> str:
    parts = []

    if receipts:
        root_causes = [r["root_cause"] for r in receipts if r.get("root_cause")]
        parts.append(
            f"{len(receipts)} prior receipt(s) for {service}/{incident_type}. "
            f"Recent root cause: {root_causes[0][:100] if root_causes else 'unknown'}."
        )

    if patterns:
        p = patterns[0]
        parts.append(
            f"Known pattern '{p['pattern_id']}' seen {p['observation_count']}x: "
            f"{p['root_cause_template'][:100]}."
        )

    if not parts:
        return f"No prior wiki context for {service}/{incident_type}."

    return " ".join(parts)


def _empty() -> dict:
    return {
        "recent_receipts": [],
        "matching_patterns": [],
        "related_notes": [],
        "context_summary": "",
        "receipt_count": 0,
        "pattern_count": 0,
    }
