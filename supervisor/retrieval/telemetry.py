"""Structured retrieval telemetry logger.

Writes one JSON line per retrieval event to eval/retrieval_telemetry.jsonl.
Each line is a complete, self-contained record — queryable with grep/jq
and importable as NDJSON.

Schema:
  {
    "ts":               ISO-8601 event timestamp,
    "incident_id":      str,
    "service":          str,
    "incident_type":    str,
    "query":            str (first 200 chars),
    "candidates_in":    int,
    "candidates_out":   int,
    "cache_hit":        bool,
    "cache_key":        str | null,
    "top_doc_id":       str | null,
    "top_score":        float | null,
    "top_source_type":  str | null,
    "stale_count":      int,
    "latency_ms":       float,
    "scores":           [{doc_id, final_score, source_type, is_stale}] (top-5)
  }

Usage:
    from supervisor.retrieval.telemetry import log_retrieval_event
    log_retrieval_event(
        incident_id="INC001",
        service="payment-service",
        incident_type="error_spike",
        query="payment service error spike after deployment",
        candidates_in=20,
        results=ranked_candidates,
        cache_hit=False,
        latency_ms=42.3,
    )
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.retrieval.telemetry")

_DEFAULT_PATH = os.getenv("RETRIEVAL_TELEMETRY_PATH", "eval/retrieval_telemetry.jsonl")
_lock = threading.Lock()


def log_retrieval_event(
    incident_id: str,
    service: str,
    incident_type: str,
    query: str,
    candidates_in: int,
    results: list[Any],                  # RankedCandidate or RerankedCandidate
    cache_hit: bool = False,
    cache_key: str | None = None,
    latency_ms: float = 0.0,
    path: str = _DEFAULT_PATH,
) -> dict[str, Any]:
    """Write one telemetry event. Returns the event dict."""
    top = results[0] if results else None
    stale_count = sum(1 for r in results if getattr(r, "is_stale", False))

    # Build scores list (top-5, serialised)
    scores = []
    for r in results[:5]:
        entry: dict[str, Any] = {"doc_id": getattr(r, "doc_id", "")}
        # Handle both RankedCandidate and RerankedCandidate
        for field in ("final_score", "rerank_score", "hybrid_score"):
            v = getattr(r, field, None)
            if v is not None:
                entry["score"] = round(v, 4)
                break
        entry["source_type"] = getattr(r, "source_type", "")
        entry["is_stale"] = getattr(r, "is_stale", False)
        scores.append(entry)

    event: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "incident_id": incident_id,
        "service": service,
        "incident_type": incident_type,
        "query": query[:200],
        "candidates_in": candidates_in,
        "candidates_out": len(results),
        "cache_hit": cache_hit,
        "cache_key": cache_key,
        "top_doc_id": getattr(top, "doc_id", None),
        "top_score": round(getattr(top, "rerank_score", getattr(top, "final_score", 0.0)), 4) if top else None,
        "top_source_type": getattr(top, "source_type", None),
        "stale_count": stale_count,
        "latency_ms": round(latency_ms, 2),
        "scores": scores,
    }

    _append(event, path)
    logger.debug(
        "retrieval_telemetry: incident=%s cache_hit=%s top=%s score=%.3f latency=%.1fms",
        incident_id, cache_hit,
        event["top_doc_id"], event["top_score"] or 0, latency_ms,
    )
    return event


def load_events(path: str = _DEFAULT_PATH, last_n: int = 100) -> list[dict[str, Any]]:
    """Load the last N telemetry events from the log file."""
    try:
        with open(path) as f:
            lines = [l.strip() for l in f if l.strip()]
        return [json.loads(l) for l in lines[-last_n:]]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _append(event: dict[str, Any], path: str) -> None:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with _lock:
            with open(path, "a") as f:
                f.write(json.dumps(event) + "\n")
    except OSError as exc:
        logger.debug("retrieval_telemetry: write failed: %s", exc)
