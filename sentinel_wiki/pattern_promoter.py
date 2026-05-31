"""Promote recurring RCA receipts into reusable patterns.

When the same root cause hash appears in >= PROMOTE_THRESHOLD receipts,
a pattern YAML is written (or updated) in sentinel_wiki/patterns/.

This is the compression step: many incident receipts → one generalised pattern.
Patterns feed back into the agent's pre-flight context via wiki_context.py.

Patterns are stored as YAML (machine-readable) so they can be loaded without
Markdown parsing.
"""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml as _yaml
    _YAML_OK = True
except ImportError:
    _YAML_OK = False

logger = logging.getLogger("sentinalai.wiki.pattern_promoter")

PROMOTE_THRESHOLD = int(os.getenv("WIKI_PROMOTE_THRESHOLD", "3"))


@dataclass
class PatternEntry:
    pattern_id: str
    root_cause_hash: str
    root_cause_template: str          # most common root_cause string
    services: list[str] = field(default_factory=list)
    incident_types: list[str] = field(default_factory=list)
    observation_count: int = 0
    confidence_avg: float = 0.0
    quality_avg: float = 0.0
    evidence_signals: list[str] = field(default_factory=list)
    receipt_ids: list[str] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""

    def to_dict(self) -> dict:
        return {
            "pattern_id": self.pattern_id,
            "root_cause_hash": self.root_cause_hash,
            "root_cause_template": self.root_cause_template,
            "services": sorted(set(self.services)),
            "incident_types": sorted(set(self.incident_types)),
            "observation_count": self.observation_count,
            "confidence_avg": round(self.confidence_avg, 2),
            "quality_avg": round(self.quality_avg, 4),
            "evidence_signals": self.evidence_signals[:20],
            "receipt_ids": self.receipt_ids[-20:],
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


def promote(base_path: str = "sentinel_wiki") -> list[str]:
    """Scan receipts/ and write patterns/ for root causes with >= PROMOTE_THRESHOLD occurrences.

    Returns list of pattern file paths written/updated.
    """
    try:
        return _promote(base_path)
    except Exception as exc:
        logger.warning("wiki.pattern_promoter: promote failed: %s", exc)
        return []


def _promote(base_path: str) -> list[str]:
    root = Path(base_path)
    receipts_dir = root / "receipts"
    patterns_dir = root / "patterns"
    patterns_dir.mkdir(parents=True, exist_ok=True)

    receipts = _load_receipts(receipts_dir)
    if not receipts:
        return []

    # Group receipts by root_cause_hash
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in receipts:
        h = r.get("root_cause_hash", "")
        if h:
            groups[h].append(r)

    written: list[str] = []
    now = datetime.now(timezone.utc).isoformat()

    for rc_hash, group in groups.items():
        if len(group) < PROMOTE_THRESHOLD:
            continue

        # Load existing pattern if it exists
        pattern_path = patterns_dir / f"{rc_hash}.yaml"
        existing = _load_pattern(pattern_path)

        # Aggregate
        all_receipts = group
        root_causes = [r.get("root_cause", "") for r in all_receipts if r.get("root_cause")]
        services = [r.get("service", "") for r in all_receipts if r.get("service")]
        inc_types = [r.get("incident_type", "") for r in all_receipts if r.get("incident_type")]
        confidences = [float(r.get("confidence", 0)) for r in all_receipts]
        qualities = [float(r.get("quality_score", 0)) for r in all_receipts]
        receipt_ids = [r.get("receipt_id", "") for r in all_receipts if r.get("receipt_id")]
        evidence_sets = [r.get("evidence_keys", []) for r in all_receipts]

        # Most common root cause as template
        from collections import Counter
        most_common_rc = Counter(root_causes).most_common(1)[0][0] if root_causes else "unknown"

        # Common evidence keys across receipts
        flat_evidence: list[str] = [k for keys in evidence_sets for k in (keys or [])]
        evidence_signals = [k for k, _ in Counter(flat_evidence).most_common(10)]

        timestamps = sorted(r.get("timestamp", "") for r in all_receipts if r.get("timestamp"))

        entry = PatternEntry(
            pattern_id=f"PAT_{rc_hash}",
            root_cause_hash=rc_hash,
            root_cause_template=most_common_rc,
            services=services,
            incident_types=inc_types,
            observation_count=len(all_receipts),
            confidence_avg=sum(confidences) / len(confidences) if confidences else 0.0,
            quality_avg=sum(qualities) / len(qualities) if qualities else 0.0,
            evidence_signals=evidence_signals,
            receipt_ids=receipt_ids,
            first_seen=timestamps[0] if timestamps else "",
            last_seen=timestamps[-1] if timestamps else now,
        )

        # Preserve first_seen from existing pattern
        if existing and existing.get("first_seen"):
            entry.first_seen = existing["first_seen"]
        elif not entry.first_seen:
            entry.first_seen = now

        _save_pattern(pattern_path, entry)
        written.append(str(pattern_path))
        logger.debug(
            "wiki.pattern_promoter: wrote pattern %s (count=%d)", rc_hash, len(all_receipts)
        )

    return written


def _load_receipts(receipts_dir: Path) -> list[dict]:
    """Load all receipt front matter from receipts/*.md files."""
    receipts = []
    for f in receipts_dir.glob("*.md"):
        if f.name == "README.md":
            continue
        fm = _parse_front_matter(f.read_text(errors="replace"))
        if fm:
            receipts.append(fm)
    return receipts


def _parse_front_matter(text: str) -> dict | None:
    """Extract YAML front matter from a Markdown file."""
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return None
    fm_text = m.group(1)
    if _YAML_OK:
        try:
            return _yaml.safe_load(fm_text) or {}
        except Exception:
            pass
    # Fallback: simple key: value parsing (no nested structures)
    result: dict = {}
    for line in fm_text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            result[k.strip()] = v.strip()
    return result


def _load_pattern(path: Path) -> dict | None:
    if not path.exists():
        return None
    if _YAML_OK:
        try:
            return _yaml.safe_load(path.read_text()) or {}
        except Exception:
            pass
    return None


def _save_pattern(path: Path, entry: PatternEntry) -> None:
    data = entry.to_dict()
    if _YAML_OK:
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(_yaml.dump(data, default_flow_style=False, sort_keys=False))
        os.replace(tmp, path)
    else:
        import json
        # Fallback: write as JSON with .yaml extension (not ideal but functional)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)
