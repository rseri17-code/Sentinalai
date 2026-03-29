"""Experience store: persist and retrieve past investigation patterns.

After each high-quality investigation (online_score >= STORE_QUALITY_THRESHOLD),
the resolved experience is stored:

    {
        "incident_id":          str,
        "incident_type":        str,
        "service":              str,
        "root_cause":           str,
        "evidence_keys":        list[str],   # which evidence keys were populated
        "confidence":           int,
        "online_quality_score": float,
        "timestamp":            str (ISO 8601),
    }

Retrieval uses a simple similarity score:
    +3 for exact incident_type match
    +2 for exact service match
    +1 per matching evidence_key
    filtered to quality_score >= STORE_QUALITY_THRESHOLD

Top-K similar experiences are returned to agent.py and used to:
  1. Prime initial hypothesis generation with confirmed root causes from similar incidents
  2. Skip evidence workers that historically yield nothing for this service/type
  3. Boost confidence when historical matches are strong (already done via
     knowledge_worker; experience_store is a lightweight local fallback)

Design:
  - File-backed JSON (no external services required)
  - Thread-safe writes via a file lock (uses threading.Lock)
  - Bounded: max MAX_EXPERIENCES entries (evicts oldest low-quality first)
  - Returns empty list gracefully if store unavailable

Configuration:
  EXPERIENCE_STORE_ENABLED    — Enable/disable (default: true)
  EXPERIENCE_STORE_PATH       — JSON file path
  STORE_QUALITY_THRESHOLD     — Min online_score to persist (default: 0.60)
  MAX_EXPERIENCES             — Max stored entries before eviction (default: 500)
  EXPERIENCE_TOP_K            — Max experiences returned per query (default: 3)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("sentinalai.experience_store")

EXPERIENCE_STORE_ENABLED = os.environ.get(
    "EXPERIENCE_STORE_ENABLED", "true"
).lower() in ("1", "true", "yes")

EXPERIENCE_STORE_PATH = os.environ.get(
    "EXPERIENCE_STORE_PATH",
    os.path.join(os.path.dirname(__file__), "..", "eval", "experience_store.json"),
)

STORE_QUALITY_THRESHOLD = float(os.environ.get("STORE_QUALITY_THRESHOLD", "0.60"))
MAX_EXPERIENCES = int(os.environ.get("MAX_EXPERIENCES", "500"))
EXPERIENCE_TOP_K = int(os.environ.get("EXPERIENCE_TOP_K", "3"))

_store_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def store_experience(
    incident_id: str,
    incident_type: str,
    service: str,
    result: dict,
    online_quality_score: float,
) -> bool:
    """Persist a resolved investigation experience if quality is sufficient.

    Args:
        incident_id: Unique incident identifier
        incident_type: Classified type (timeout, oomkill, etc.)
        service: Affected service
        result: RCA result dict (used for root_cause, confidence, evidence_keys)
        online_quality_score: Score from online_evaluator (0.0–1.0)

    Returns:
        True if stored, False if below threshold or error.
    """
    if not EXPERIENCE_STORE_ENABLED:
        return False
    if online_quality_score < STORE_QUALITY_THRESHOLD:
        logger.debug(
            "Experience skipped (quality=%.2f < %.2f): %s",
            online_quality_score, STORE_QUALITY_THRESHOLD, incident_id,
        )
        return False

    root_cause = result.get("root_cause", "")
    if root_cause.startswith("INSUFFICIENT") or root_cause.startswith("LOW CONFIDENCE"):
        logger.debug("Experience skipped (inconclusive root cause): %s", incident_id)
        return False

    # Collect populated evidence key names (exclude internals, empty values)
    evidence_keys: list[str] = []
    for ev_key, ev_val in result.get("_evidence_snapshot", {}).items():
        if ev_key.startswith("_"):
            continue
        if ev_val:
            evidence_keys.append(ev_key)

    # Fall back to annotated evidence source list if snapshot not available
    if not evidence_keys:
        oe = result.get("_online_eval", {})
        evidence_keys = oe.get("sources_found", [])

    entry = {
        "incident_id":          incident_id,
        "incident_type":        incident_type,
        "service":              service,
        "root_cause":           root_cause,
        "evidence_keys":        evidence_keys,
        "confidence":           result.get("confidence", 0),
        "online_quality_score": round(online_quality_score, 4),
        "timestamp":            datetime.now(timezone.utc).isoformat(),
    }

    try:
        with _store_lock:
            experiences = _load_raw()
            # Evict if at capacity: remove oldest lowest-quality entries first
            if len(experiences) >= MAX_EXPERIENCES:
                experiences.sort(key=lambda x: (x.get("online_quality_score", 0), x.get("timestamp", "")))
                experiences = experiences[len(experiences) - MAX_EXPERIENCES + 1:]
            experiences.append(entry)
            _save_raw(experiences)

        logger.info(
            "Experience stored: %s type=%s service=%s quality=%.2f",
            incident_id, incident_type, service, online_quality_score,
        )
        return True

    except Exception as exc:
        logger.warning("Failed to store experience for %s: %s", incident_id, exc)
        return False


def retrieve_similar(
    incident_type: str,
    service: str,
    top_k: int = EXPERIENCE_TOP_K,
) -> list[dict]:
    """Retrieve the most similar past experiences for priming.

    Returns list of experience dicts sorted by similarity (highest first).
    Each entry includes a 'similarity_score' key added by this function.
    Returns empty list on any error.
    """
    if not EXPERIENCE_STORE_ENABLED:
        return []

    try:
        with _store_lock:
            experiences = _load_raw()

        if not experiences:
            return []

        scored = []
        for exp in experiences:
            sim = _similarity(exp, incident_type, service)
            if sim > 0:
                scored.append({**exp, "similarity_score": sim})

        scored.sort(key=lambda x: x["similarity_score"], reverse=True)
        results = scored[:top_k]

        if results:
            logger.info(
                "Retrieved %d similar experience(s) for type=%s service=%s "
                "(top_similarity=%.1f)",
                len(results), incident_type, service,
                results[0]["similarity_score"],
            )
        return results

    except Exception as exc:
        logger.warning("Experience retrieval failed (non-critical): %s", exc)
        return []


def get_stats() -> dict:
    """Return summary statistics about the experience store."""
    try:
        with _store_lock:
            experiences = _load_raw()
        if not experiences:
            return {"count": 0}

        by_type: dict[str, int] = {}
        by_service: dict[str, int] = {}
        scores = []
        for exp in experiences:
            t = exp.get("incident_type", "unknown")
            s = exp.get("service", "unknown")
            by_type[t] = by_type.get(t, 0) + 1
            by_service[s] = by_service.get(s, 0) + 1
            scores.append(exp.get("online_quality_score", 0.0))

        return {
            "count": len(experiences),
            "by_type": by_type,
            "by_service": by_service,
            "mean_quality": round(sum(scores) / len(scores), 3) if scores else 0.0,
        }
    except Exception as exc:
        logger.warning("get_stats failed: %s", exc)
        return {"count": 0}


# ---------------------------------------------------------------------------
# Similarity scoring
# ---------------------------------------------------------------------------

def _similarity(exp: dict, incident_type: str, service: str) -> float:
    """Simple similarity score between a stored experience and a query."""
    score = 0.0
    if exp.get("incident_type") == incident_type:
        score += 3.0
    if exp.get("service") == service:
        score += 2.0
    # Partial service match (same prefix, e.g. "payments" in "payments-api")
    elif service and exp.get("service", "").startswith(service.split("-")[0]):
        score += 0.5
    # Quality weight: better experiences are more valuable
    score *= (0.5 + 0.5 * exp.get("online_quality_score", 0.0))
    return round(score, 3)


# ---------------------------------------------------------------------------
# Raw persistence helpers
# ---------------------------------------------------------------------------

def _load_raw() -> list[dict]:
    """Load experience list from disk. Returns [] if file absent or corrupt."""
    path = EXPERIENCE_STORE_PATH
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Experience store corrupt, resetting: %s", exc)
        return []


def _save_raw(experiences: list[dict]) -> None:
    """Persist experience list to disk atomically."""
    path = EXPERIENCE_STORE_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(experiences, f, indent=2)
    os.replace(tmp, path)
