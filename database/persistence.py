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


def persist_eval_result(
    incident_id: str,
    root_cause_match: str,
    root_cause_score: float,
    confidence_error: float,
    evidence_coverage: float,
    actual_correct: bool,
    predicted_confidence: int,
    missing_evidence: list[str] | None = None,
) -> bool:
    """Persist a ground-truth evaluation result.

    These rows feed the confidence calibrator and track accuracy over time.
    The table must already exist (created by schema.sql).

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
                    INSERT INTO eval_results
                        (incident_id, root_cause_match, root_cause_score,
                         confidence_error, evidence_coverage, actual_correct,
                         predicted_confidence, missing_evidence)
                    VALUES
                        (:incident_id, :rc_match, :rc_score,
                         :conf_error, :ev_coverage, :actual_correct,
                         :pred_conf, :missing)
                """),
                {
                    "incident_id": incident_id,
                    "rc_match": root_cause_match,
                    "rc_score": root_cause_score,
                    "conf_error": confidence_error,
                    "ev_coverage": evidence_coverage,
                    "actual_correct": actual_correct,
                    "pred_conf": predicted_confidence,
                    "missing": json.dumps(missing_evidence or []),
                },
            )
            conn.commit()

        logger.info(
            "Eval result persisted: incident=%s match=%s correct=%s",
            incident_id, root_cause_match, actual_correct,
        )
        return True

    except Exception as exc:
        logger.warning("Failed to persist eval result %s: %s", incident_id, exc)
        return False


def load_eval_results_for_calibration(limit: int = 500) -> list[dict]:
    """Load recent eval results for calibration update.

    Returns list of {predicted_confidence, actual_correct} dicts,
    suitable for passing directly to ConfidenceCalibrator.update().
    """
    engine = get_engine()
    if engine is None:
        return []

    try:
        from sqlalchemy import text

        with engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT predicted_confidence, actual_correct
                    FROM eval_results
                    ORDER BY evaluated_at DESC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            return [
                {"predicted_confidence": row[0], "actual_correct": bool(row[1])}
                for row in result.fetchall()
            ]

    except Exception as exc:
        logger.warning("Failed to load eval results for calibration: %s", exc)
        return []


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


# ---------------------------------------------------------------------------
# Knowledge graph persistence
# ---------------------------------------------------------------------------

def persist_kg_snapshot(nodes: list[dict], edges: list[dict]) -> bool:
    """Upsert all nodes and edges to kg_nodes/kg_edges tables.

    Called by KnowledgeGraph.save() when the DB is available.  The JSON file
    remains the primary store for local dev; Postgres is the durable replica.
    """
    engine = get_engine()
    if engine is None:
        return False

    try:
        from sqlalchemy import text
        import json as _json

        with engine.connect() as conn:
            for node in nodes:
                conn.execute(
                    text("""
                        INSERT INTO kg_nodes (node_id, node_type, label, props, created_at)
                        VALUES (:node_id, :node_type, :label, :props::jsonb, to_timestamp(:created_at))
                        ON CONFLICT (node_id) DO UPDATE
                            SET label = EXCLUDED.label,
                                props = EXCLUDED.props
                    """),
                    {
                        "node_id": node["node_id"],
                        "node_type": node["node_type"],
                        "label": node["label"],
                        "props": _json.dumps(node.get("props", {})),
                        "created_at": node.get("created_at", 0),
                    },
                )
            for edge in edges:
                conn.execute(
                    text("""
                        INSERT INTO kg_edges (edge_id, src_id, dst_id, rel_type, weight, props, created_at)
                        VALUES (:edge_id, :src_id, :dst_id, :rel_type, :weight, :props::jsonb, to_timestamp(:created_at))
                        ON CONFLICT (edge_id) DO UPDATE
                            SET weight = EXCLUDED.weight,
                                props = EXCLUDED.props
                    """),
                    {
                        "edge_id": edge["edge_id"],
                        "src_id": edge["src_id"],
                        "dst_id": edge["dst_id"],
                        "rel_type": edge["rel_type"],
                        "weight": edge.get("weight", 1.0),
                        "props": _json.dumps(edge.get("props", {})),
                        "created_at": edge.get("created_at", 0),
                    },
                )
            conn.commit()
        return True

    except Exception as exc:
        logger.warning("Failed to persist KG snapshot to DB: %s", exc)
        return False


def load_kg_snapshot() -> dict | None:
    """Load nodes and edges from Postgres.

    Returns {"nodes": [...], "edges": [...]} or None if DB unavailable/empty.
    """
    engine = get_engine()
    if engine is None:
        return None

    try:
        from sqlalchemy import text
        import json as _json

        with engine.connect() as conn:
            nodes_result = conn.execute(
                text("""
                    SELECT node_id, node_type, label, props,
                           EXTRACT(EPOCH FROM created_at)
                    FROM kg_nodes
                    WHERE ttl_expires_at IS NULL OR ttl_expires_at > NOW()
                    ORDER BY created_at
                """)
            )
            nodes = [
                {
                    "node_id": r[0], "node_type": r[1], "label": r[2],
                    "props": r[3] if isinstance(r[3], dict) else _json.loads(r[3] or "{}"),
                    "created_at": float(r[4] or 0),
                }
                for r in nodes_result.fetchall()
            ]
            if not nodes:
                return None

            node_ids = {n["node_id"] for n in nodes}
            edges_result = conn.execute(
                text("""
                    SELECT edge_id, src_id, dst_id, rel_type, weight, props,
                           EXTRACT(EPOCH FROM created_at)
                    FROM kg_edges
                    WHERE src_id = ANY(:ids) AND dst_id = ANY(:ids)
                    ORDER BY created_at
                """),
                {"ids": list(node_ids)},
            )
            edges = [
                {
                    "edge_id": r[0], "src_id": r[1], "dst_id": r[2],
                    "rel_type": r[3], "weight": float(r[4] or 1.0),
                    "props": r[5] if isinstance(r[5], dict) else _json.loads(r[5] or "{}"),
                    "created_at": float(r[6] or 0),
                }
                for r in edges_result.fetchall()
            ]
        return {"nodes": nodes, "edges": edges}

    except Exception as exc:
        logger.warning("Failed to load KG snapshot from DB: %s", exc)
        return None


def evict_expired_kg_nodes() -> int:
    """Delete KG nodes whose TTL has expired. Returns count deleted."""
    engine = get_engine()
    if engine is None:
        return 0
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            result = conn.execute(
                text("DELETE FROM kg_nodes WHERE ttl_expires_at IS NOT NULL AND ttl_expires_at <= NOW()")
            )
            conn.commit()
            return result.rowcount
    except Exception as exc:
        logger.warning("Failed to evict expired KG nodes: %s", exc)
        return 0
