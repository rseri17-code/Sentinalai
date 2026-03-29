"""AG UI Learning Loop API.

Exposes the self-learning loop metrics for dashboard display:

  GET /api/v1/learning/experience  → Experience store statistics and recent entries
  GET /api/v1/learning/strategy    → Evolved strategy weights
  GET /api/v1/learning/summary     → Combined dashboard summary
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from agui.middleware.auth import ActorContext, get_actor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/learning", tags=["learning"])


def _load_experiences() -> list[dict]:
    """Load raw experience list from disk. Returns [] on error."""
    try:
        from supervisor.experience_store import EXPERIENCE_STORE_PATH, _store_lock
        import json
        with _store_lock:
            with open(EXPERIENCE_STORE_PATH) as f:
                data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.warning("load_experiences failed: %s", exc)
        return []


@router.get("/experience")
async def get_experience_stats(actor: ActorContext = Depends(get_actor)):
    """Return experience store statistics and recent entries."""
    try:
        experiences = _load_experiences()
        total = len(experiences)

        by_type: dict[str, int] = {}
        scores: list[float] = []

        for exp in experiences:
            t = exp.get("incident_type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1
            score = exp.get("online_quality_score")
            if score is not None:
                scores.append(score)

        avg_score = round(sum(scores) / len(scores), 3) if scores else None

        # 5 most recent (sorted by timestamp)
        recent_raw = sorted(experiences, key=lambda e: e.get("timestamp", ""), reverse=True)[:5]
        recent = [
            {
                "incident_id": e.get("incident_id", ""),
                "incident_type": e.get("incident_type", ""),
                "service": e.get("service", ""),
                "confidence": e.get("confidence", 0),
                "root_cause": (e.get("root_cause") or "")[:80],
                "online_quality_score": e.get("online_quality_score"),
                "stored_at": e.get("timestamp", ""),
            }
            for e in recent_raw
        ]

        return {
            "total_experiences": total,
            "avg_quality_score": avg_score,
            "by_incident_type": dict(sorted(by_type.items(), key=lambda x: -x[1])),
            "recent": recent,
        }
    except Exception as exc:
        logger.warning("experience stats failed: %s", exc)
        return {"total_experiences": 0, "avg_quality_score": None, "by_incident_type": {}, "recent": []}


@router.get("/strategy")
async def get_strategy(actor: ActorContext = Depends(get_actor)):
    """Return current evolved strategy weights."""
    try:
        from supervisor.strategy_evolver import get_report
        return get_report()
    except Exception as exc:
        logger.warning("strategy report failed: %s", exc)
        return {"meta": {}, "incident_types": {}}


@router.get("/summary")
async def get_learning_summary(actor: ActorContext = Depends(get_actor)):
    """Combined dashboard summary of the self-learning loop."""
    try:
        from supervisor.strategy_evolver import get_report

        experiences = _load_experiences()
        scores = [e["online_quality_score"] for e in experiences if e.get("online_quality_score") is not None]
        avg_score = round(sum(scores) / len(scores), 3) if scores else None

        by_type: dict[str, int] = {}
        for exp in experiences:
            t = exp.get("incident_type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1

        strategy_report = get_report()
        total_updates = strategy_report.get("meta", {}).get("total_updates", 0)
        evolved_types = len(strategy_report.get("incident_types", {}))

        top_steps: list[dict] = []
        for inc_type, steps in strategy_report.get("incident_types", {}).items():
            for step in (steps or [])[:2]:
                w = step.get("weight", 1.0)
                if w != 1.0:
                    top_steps.append({
                        "incident_type": inc_type,
                        "step": step["step"],
                        "weight": w,
                        "calls": step.get("calls", 0),
                    })
        top_steps.sort(key=lambda x: abs(x["weight"] - 1.0), reverse=True)

        return {
            "experience_store": {
                "total": len(experiences),
                "avg_quality_score": avg_score,
                "by_incident_type": dict(sorted(by_type.items(), key=lambda x: -x[1])),
            },
            "strategy_evolution": {
                "total_updates": total_updates,
                "evolved_incident_types": evolved_types,
                "top_evolved_steps": top_steps[:10],
            },
            "self_learning_active": True,
        }
    except Exception as exc:
        logger.warning("learning summary failed: %s", exc)
        return {
            "experience_store": {"total": 0, "avg_quality_score": None, "by_incident_type": {}},
            "strategy_evolution": {"total_updates": 0, "evolved_incident_types": 0, "top_evolved_steps": []},
            "self_learning_active": False,
        }
