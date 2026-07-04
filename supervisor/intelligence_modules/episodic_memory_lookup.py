"""EpisodicMemoryLookup runner for the Intelligence Runtime.

Fifth read-path module. Runs at POST_CLASSIFY and consults the JSONL
episodic-memory store for episodes on the same service +
incident_type, returning the most recent (or, when a failure signature
is available, semantically-ranked) matches.

Source queried (verbatim, no schema change):
- ``intelligence.episodic_memory.EpisodicMemory`` — populated inline by
  supervisor.agent (see agent.py:1122 legacy path).

Never raises. Runtime failure isolation catches internal errors.

Feature-flag-gated: ``ENABLE_EPISODIC_MEMORY_LOOKUP``. Default off.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from sentinel_core.runtime import (
    IntelligenceStage,
    ModuleSpec,
    RuntimeContext,
)

logger = logging.getLogger("sentinalai.intelligence_modules.episodic_memory_lookup")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EPISODIC_MEMORY_LOOKUP_FEATURE_FLAG = "ENABLE_EPISODIC_MEMORY_LOOKUP"
LOOKUP_VERSION = 1

_MAX_MATCHES = 5

# EpisodicMemory's default storage path is computed at import time from the
# module file location; provide an env override so tests + prod deployments
# can point to a specific file.
_DEFAULT_STORAGE_PATH_ENV = "EPISODIC_MEMORY_PATH"


# ---------------------------------------------------------------------------
# ModuleSpec
# ---------------------------------------------------------------------------

EPISODIC_MEMORY_LOOKUP_SPEC = ModuleSpec(
    name="episodic_memory_lookup",
    stage=IntelligenceStage.POST_CLASSIFY,
    feature_flag=EPISODIC_MEMORY_LOOKUP_FEATURE_FLAG,
    priority=500,                     # after the other 4 read modules
)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def episodic_memory_lookup_runner(ctx: RuntimeContext) -> dict[str, Any]:
    """Recall episodes matching (service, incident_type).

    Returns:
        {status, service, incident_type,
         episodes: [{episode_id, incident_id, root_cause_head,
                      resolution_action_head, outcome, confidence,
                      recorded_at}],
         match_count, version}

    Statuses:
        success — query succeeded; matches possibly empty
        skipped — no service and no incident_type
        failed  — runtime-captured error
    """
    service = _extract_service(ctx)
    incident_type = _extract_incident_type(ctx)

    if not service and not incident_type:
        return {
            "status":  "skipped",
            "reason":  "no_service_and_no_incident_type",
            "version": LOOKUP_VERSION,
        }

    episodes = _query_episodes(service=service, incident_type=incident_type)

    return {
        "status":        "success",
        "service":       service,
        "incident_type": incident_type,
        "episodes":      episodes,
        "match_count":   len(episodes),
        "version":       LOOKUP_VERSION,
    }


# ---------------------------------------------------------------------------
# Context extractors
# ---------------------------------------------------------------------------

def _extract_service(ctx: RuntimeContext) -> str:
    if ctx.fetch_out and isinstance(ctx.fetch_out, dict):
        v = ctx.fetch_out.get("service", "")
        if v:
            return str(v)
    return ""


def _extract_incident_type(ctx: RuntimeContext) -> str:
    if ctx.cres is not None:
        v = getattr(ctx.cres, "incident_type", "")
        if v:
            return str(v)
    return ""


# ---------------------------------------------------------------------------
# Store query
# ---------------------------------------------------------------------------

def _query_episodes(*, service: str, incident_type: str) -> list[dict[str, Any]]:
    """Query EpisodicMemory for matches. Never raises."""
    try:
        from intelligence.episodic_memory import EpisodicMemory
        # Path resolution:
        # 1. EPISODIC_MEMORY_PATH env var if set
        # 2. else EpisodicMemory's own default (eval/episodic_memory.jsonl)
        path = os.environ.get(_DEFAULT_STORAGE_PATH_ENV)
        mem = EpisodicMemory(storage_path=path) if path else EpisodicMemory()
        eps = mem.search(
            service=service or None,
            incident_type=incident_type or None,
            limit=_MAX_MATCHES,
        )
    except Exception as exc:
        logger.debug("episodic_memory_lookup: query failed: %s", exc)
        return []
    return [_episode_dict(ep) for ep in eps]


def _episode_dict(ep) -> dict[str, Any]:
    """Compact episode representation. Heads truncated so receipt stays bounded."""
    return {
        "episode_id":             ep.episode_id,
        "incident_id":            ep.incident_id,
        "service":                ep.service,
        "incident_type":          ep.incident_type,
        "root_cause_head":        (ep.root_cause or "")[:160],
        "resolution_action_head": (ep.resolution_action or "")[:160],
        "outcome":                ep.outcome,
        "confidence":             round(float(ep.confidence or 0.0), 3),
        "recorded_at":            str(ep.recorded_at or ""),
    }


__all__ = [
    "EPISODIC_MEMORY_LOOKUP_SPEC",
    "EPISODIC_MEMORY_LOOKUP_FEATURE_FLAG",
    "LOOKUP_VERSION",
    "episodic_memory_lookup_runner",
]
