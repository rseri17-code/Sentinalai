"""Database persistence layer for SentinalAI investigations.

Persists investigation results, tool usage, and knowledge base entries
to PostgreSQL. Gracefully degrades when the database is unavailable.

Usage:
    from database.persistence import persist_investigation, is_enabled

    if is_enabled():
        persist_investigation(result, receipts, incident_type, service)
"""

from __future__ import annotations

import json
import logging

from database.connection import get_engine

logger = logging.getLogger("sentinalai.persistence")


def is_enabled() -> bool:
    """Check whether database persistence is available."""
    return get_engine() is not None


def persist_investigation(
    incident_id: str,
    root_cause: str,
    confidence: float,
    reasoning: str,
    evidence_timeline: list[dict],
    tools_used: list[dict],
    elapsed_seconds: float = 0.0,
    incident_type: str = "",
    service: str = "",
    rca_report: dict | None = None,
) -> int | None:
    """Persist an investigation result to the database.

    Returns the investigation row ID, or None on failure.
    """
    engine = get_engine()
    if engine is None:
        return None

    try:
        from sqlalchemy import text

        with engine.connect() as conn:
            result = conn.execute(
                text("""
                    INSERT INTO investigations
                        (incident_id, root_cause, confidence, reasoning,
                         evidence_timeline, tools_used, investigation_time_seconds)
                    VALUES
                        (:incident_id, :root_cause, :confidence, :reasoning,
                         :evidence_timeline, :tools_used, :elapsed)
                    ON CONFLICT (incident_id) DO UPDATE SET
                        root_cause = EXCLUDED.root_cause,
                        confidence = EXCLUDED.confidence,
                        reasoning = EXCLUDED.reasoning,
                        evidence_timeline = EXCLUDED.evidence_timeline,
                        tools_used = EXCLUDED.tools_used,
                        investigation_time_seconds = EXCLUDED.investigation_time_seconds,
                        updated_at = NOW()
                    RETURNING id
                """),
                {
                    "incident_id": incident_id,
                    "root_cause": root_cause,
                    "confidence": confidence,
                    "reasoning": reasoning,
                    "evidence_timeline": json.dumps(evidence_timeline, default=str),
                    "tools_used": json.dumps(tools_used, default=str),
                    "elapsed": elapsed_seconds,
                },
            )
            row = result.fetchone()
            investigation_id = row[0] if row else None
            conn.commit()

            logger.info(
                "Investigation persisted: incident=%s db_id=%s",
                incident_id, investigation_id,
            )
            return investigation_id

    except Exception as exc:
        logger.warning("Failed to persist investigation %s: %s", incident_id, exc)
        return None


def persist_tool_usage(
    investigation_id: int,
    receipts: list[dict],
) -> int:
    """Persist tool usage records from receipts.

    Returns the number of records inserted.
    """
    engine = get_engine()
    if engine is None:
        return 0

    try:
        from sqlalchemy import text

        count = 0
        with engine.connect() as conn:
            for receipt in receipts:
                conn.execute(
                    text("""
                        INSERT INTO tool_usage
                            (investigation_id, tool_name, parameters, response, duration_ms)
                        VALUES
                            (:inv_id, :tool, :params, :response, :duration)
                    """),
                    {
                        "inv_id": investigation_id,
                        "tool": f"{receipt.get('tool', '')}.{receipt.get('action', '')}",
                        "params": json.dumps(receipt.get("params", {}), default=str),
                        "response": json.dumps(
                            {"status": receipt.get("status", ""), "result_count": receipt.get("result_count", 0)},
                            default=str,
                        ),
                        "duration": int(receipt.get("elapsed_ms", 0)),
                    },
                )
                count += 1
            conn.commit()

        logger.debug("Persisted %d tool usage records for investigation %d", count, investigation_id)
        return count

    except Exception as exc:
        logger.warning("Failed to persist tool usage: %s", exc)
        return 0


def persist_knowledge_entry(
    incident_id: str,
    incident_type: str,
    root_cause: str,
    service: str,
    metadata: dict | None = None,
) -> bool:
    """Persist an entry to the knowledge base for future similarity search.

    Note: embedding generation is deferred — the embedding column is left NULL
    until an async embedding pipeline fills it in.

    Returns True on success.
    """
    engine = get_engine()
    if engine is None:
        return False

    try:
        from sqlalchemy import text

        with engine.connect() as conn:
            conn.execute(
                text("""
                    INSERT INTO knowledge_base
                        (incident_id, incident_type, root_cause, service, metadata)
                    VALUES
                        (:incident_id, :incident_type, :root_cause, :service, :metadata)
                """),
                {
                    "incident_id": incident_id,
                    "incident_type": incident_type,
                    "root_cause": root_cause,
                    "service": service,
                    "metadata": json.dumps(metadata or {}, default=str),
                },
            )
            conn.commit()

        logger.info("Knowledge entry persisted: incident=%s type=%s", incident_id, incident_type)
        return True

    except Exception as exc:
        logger.warning("Failed to persist knowledge entry %s: %s", incident_id, exc)
        return False


def load_investigation(incident_id: str) -> dict | None:
    """Load a stored investigation result by incident_id.

    Returns the investigation dict or None if not found.
    """
    engine = get_engine()
    if engine is None:
        return None

    try:
        from sqlalchemy import text

        with engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT incident_id, root_cause, confidence, reasoning,
                           evidence_timeline, tools_used, investigation_time_seconds,
                           created_at, updated_at
                    FROM investigations
                    WHERE incident_id = :incident_id
                """),
                {"incident_id": incident_id},
            )
            row = result.fetchone()
            if not row:
                return None

            return {
                "incident_id": row[0],
                "root_cause": row[1],
                "confidence": row[2],
                "reasoning": row[3],
                "evidence_timeline": json.loads(row[4]) if isinstance(row[4], str) else row[4],
                "tools_used": json.loads(row[5]) if isinstance(row[5], str) else row[5],
                "elapsed_seconds": row[6],
                "created_at": str(row[7]),
                "updated_at": str(row[8]),
            }

    except Exception as exc:
        logger.warning("Failed to load investigation %s: %s", incident_id, exc)
        return None
