"""Gap pattern aggregator for SentinalAI continuous learning.

After each investigation self-critique identifies which evidence categories
were missing or insufficient.  This module aggregates those gaps across
investigations to detect PERSISTENT gaps: evidence sources that are
*chronically absent* for a given incident_type / service combination.

Why it matters
--------------
If auth-service timeout investigations consistently lack APM golden signals,
the strategy evolver should:
  1. Try harder to gather golden signals (raise worker priority)
  2. Or give up earlier (demote weight) if they're unavailable

The gap aggregator answers:  "For (timeout, auth-service) what evidence
categories are missing in >50% of investigations?"

That answer feeds:
  - strategy_evolver: penalise steps that consistently miss
  - tool_selector: re-order to attempt historically-absent sources first
  - adaptive_thresholds: tighten CRITIQUE_THRESHOLD when known gaps exist

Schema (JSON on disk)
---------------------
{
  "<incident_type>:<service>": {
    "<gap_category>": {
      "count": 12,            # total times this gap appeared
      "total_seen": 20,       # total investigations for this type+service
      "frequency": 0.60,      # count / total_seen
      "last_seen": "ISO8601"
    }
  },
  "_meta": { "total_records": N, "last_updated": "ISO8601" }
}

Configuration
-------------
  GAP_AGGREGATOR_ENABLED  — on/off (default: true)
  GAP_AGGREGATOR_PATH     — JSON file (default: eval/gap_patterns.json)
  GAP_PERSISTENT_THRESHOLD — frequency above which a gap is "persistent" (default: 0.50)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.gap_aggregator")

GAP_AGGREGATOR_ENABLED = os.environ.get(
    "GAP_AGGREGATOR_ENABLED", "true"
).lower() in ("1", "true", "yes")

GAP_AGGREGATOR_PATH = os.environ.get(
    "GAP_AGGREGATOR_PATH",
    os.path.join(os.path.dirname(__file__), "..", "eval", "gap_patterns.json"),
)

GAP_PERSISTENT_THRESHOLD = float(os.environ.get("GAP_PERSISTENT_THRESHOLD", "0.50"))

_agg_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_gaps(
    incident_type: str,
    service: str,
    gap_categories: list[str],
) -> None:
    """Record gap categories observed in a single investigation.

    Args:
        incident_type:   Classified incident type (timeout, oomkill, etc.)
        service:         Affected service name
        gap_categories:  List of missing evidence categories identified by
                         self-critique (e.g. ["apm_data", "golden_signals"])
    """
    if not GAP_AGGREGATOR_ENABLED:
        return
    if not incident_type or not service:
        return

    key = _make_key(incident_type, service)
    now = datetime.now(timezone.utc).isoformat()

    try:
        with _agg_lock:
            data = _load()
            bucket = data.setdefault(key, {})

            # Increment total_seen counter for this key
            meta_key = "_total"
            total = bucket.get(meta_key, {}).get("total_seen", 0) + 1
            bucket[meta_key] = {"total_seen": total, "last_seen": now}

            for cat in gap_categories:
                if not cat or cat.startswith("_"):
                    continue
                entry = bucket.setdefault(cat, {"count": 0, "total_seen": 0, "frequency": 0.0})
                entry["count"] += 1
                entry["total_seen"] = total
                entry["frequency"] = round(entry["count"] / total, 4)
                entry["last_seen"] = now

            _save(data)

        if gap_categories:
            logger.debug(
                "Gap patterns recorded: key=%s gaps=%s",
                key, gap_categories,
            )

    except Exception as exc:
        logger.warning("Gap recording failed (non-critical): %s", exc)


def get_persistent_gaps(
    incident_type: str,
    service: str,
    threshold: float | None = None,
) -> list[str]:
    """Return gap categories that appear frequently for this type+service.

    Args:
        incident_type:  Incident type to look up
        service:        Service name to look up
        threshold:      Override module-level GAP_PERSISTENT_THRESHOLD

    Returns:
        List of gap category names that appear in >= threshold fraction of
        investigations. Sorted by frequency descending. Empty list on error.
    """
    if not GAP_AGGREGATOR_ENABLED:
        return []

    freq_cutoff = threshold if threshold is not None else GAP_PERSISTENT_THRESHOLD
    key = _make_key(incident_type, service)

    try:
        with _agg_lock:
            data = _load()

        bucket = data.get(key, {})

        # Also check the broadened key (type only, any service)
        broad_key = _make_key(incident_type, "*")
        broad_bucket = data.get(broad_key, {})

        if not bucket and not broad_bucket:
            return []

        # Always compute frequency from the bucket-level total so that gaps
        # become less prominent as more investigations run without them.
        bucket_total = bucket.get("_total", {}).get("total_seen", 0)

        gaps: list[tuple[str, float]] = []
        for cat, entry in bucket.items():
            if cat.startswith("_"):
                continue
            if not isinstance(entry, dict):
                continue
            count = entry.get("count", 0)
            freq = count / bucket_total if bucket_total > 0 else 0.0
            if freq >= freq_cutoff:
                gaps.append((cat, round(freq, 4)))

        # Merge broad gaps (type-level, lower weight)
        broad_total = broad_bucket.get("_total", {}).get("total_seen", 0)
        seen = {g[0] for g in gaps}
        for cat, entry in broad_bucket.items():
            if cat.startswith("_") or cat in seen:
                continue
            if not isinstance(entry, dict):
                continue
            count = entry.get("count", 0)
            freq = count / broad_total if broad_total > 0 else 0.0
            if freq >= freq_cutoff:
                gaps.append((cat, round(freq * 0.7, 4)))  # discount broad match

        gaps.sort(key=lambda x: -x[1])
        return [g[0] for g in gaps]

    except Exception as exc:
        logger.warning("get_persistent_gaps failed (non-critical): %s", exc)
        return []


def get_gap_report(incident_type: str | None = None) -> dict[str, Any]:
    """Return a summary report of gap patterns for introspection.

    If incident_type is provided, filters to that type only.
    """
    try:
        with _agg_lock:
            data = _load()

        report: dict[str, Any] = {
            "meta": data.get("_meta", {}),
            "patterns": {},
        }
        for key, bucket in data.items():
            if key.startswith("_"):
                continue
            if incident_type and not key.startswith(f"{incident_type}:"):
                continue
            total = bucket.get("_total", {}).get("total_seen", 0)
            patterns = {
                cat: {
                    "count": e["count"],
                    "frequency": round(e["count"] / total, 4) if total > 0 else 0.0,
                    "persistent": (e["count"] / total >= GAP_PERSISTENT_THRESHOLD)
                                  if total > 0 else False,
                }
                for cat, e in bucket.items()
                if not cat.startswith("_") and isinstance(e, dict)
            }
            report["patterns"][key] = {
                "total_investigations": total,
                "gaps": sorted(patterns.items(), key=lambda x: -x[1]["frequency"]),
            }

        return report

    except Exception as exc:
        logger.warning("get_gap_report failed: %s", exc)
        return {}


def record_gaps_from_critique(
    incident_type: str,
    service: str,
    critique: Any,
) -> None:
    """Convenience wrapper: extract gap categories from a CritiqueResult.

    Accepts a CritiqueResult object (supervisor.self_critique) or a plain dict
    with a 'gaps' list.  Parses gap text to extract evidence category names.
    """
    if not GAP_AGGREGATOR_ENABLED:
        return

    gaps_raw: list[str] = []
    if hasattr(critique, "gaps"):
        gaps_raw = critique.gaps or []
    elif isinstance(critique, dict):
        gaps_raw = critique.get("gaps", [])

    categories = _parse_gap_categories(gaps_raw)
    if categories:
        record_gaps(incident_type, service, categories)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_key(incident_type: str, service: str) -> str:
    return f"{incident_type}:{service}"


def _parse_gap_categories(gaps: list[str]) -> list[str]:
    """Extract evidence category names from free-text gap descriptions.

    Maps phrases like "no golden signals collected" → "golden_signals"
    and "missing APM data" → "apm_data".
    """
    _CATEGORY_KEYWORDS: list[tuple[str, str]] = [
        ("golden signal",   "golden_signals"),
        ("golden_signal",   "golden_signals"),
        ("apm",             "apm_data"),
        ("log",             "logs"),
        ("metric",          "metrics"),
        ("change_record",   "change_records"),
        ("change record",   "change_records"),
        ("itsm",            "itsm_context"),
        ("confluence",      "confluence_context"),
        ("devops",          "devops_context"),
        ("git",             "git_context"),
        ("cmdb",            "cmdb_blast_radius"),
        ("trace",           "trace_correlation"),
        ("visual",          "visual_evidence"),
        ("deploy",          "devops_context"),
    ]
    found: set[str] = set()
    for gap_text in gaps:
        text_lower = gap_text.lower()
        for keyword, category in _CATEGORY_KEYWORDS:
            if keyword in text_lower:
                found.add(category)
    return sorted(found)


def _load() -> dict:
    """Load gap data from disk. Returns {} on missing/corrupt file."""
    try:
        with open(GAP_AGGREGATOR_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Gap aggregator data corrupt, resetting: %s", exc)
        return {}


def _save(data: dict) -> None:
    """Persist gap data atomically."""
    data["_meta"] = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_records": sum(
            1 for k in data if not k.startswith("_")
        ),
    }
    os.makedirs(os.path.dirname(GAP_AGGREGATOR_PATH), exist_ok=True)
    tmp = GAP_AGGREGATOR_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, GAP_AGGREGATOR_PATH)
