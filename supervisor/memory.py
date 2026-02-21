"""AgentCore Memory integration for SentinalAI.

Provides short-term memory (recent investigation turns) and long-term memory
(semantic search over historical incidents) using the Bedrock AgentCore
Memory SDK.

Requires:
    - bedrock-agentcore>=1.1.0  (MemoryClient, MemorySessionManager)
    - BEDROCK_AGENTCORE_MEMORY_ID env var set to an active memory resource

Falls back gracefully when the SDK or env var is unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger("sentinalai.memory")

# ---------------------------------------------------------------------------
# Lazy SDK import — graceful degradation when not installed
# ---------------------------------------------------------------------------

_memory_client = None
_session_manager_cls = None
_message_cls = None
_role_cls = None
_sdk_available = False

try:
    from bedrock_agentcore.memory import MemoryClient  # type: ignore[import-untyped]
    from bedrock_agentcore.memory.session import MemorySessionManager  # type: ignore[import-untyped]
    from bedrock_agentcore.memory.constants import (  # type: ignore[import-untyped]
        ConversationalMessage,
        MessageRole,
    )
    _session_manager_cls = MemorySessionManager
    _message_cls = ConversationalMessage
    _role_cls = MessageRole
    _sdk_available = True
except ImportError:
    logger.debug("bedrock-agentcore memory SDK not installed — memory disabled")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MEMORY_ID = os.environ.get("BEDROCK_AGENTCORE_MEMORY_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# STM: how many recent turns to retrieve for context
STM_LAST_K_TURNS = int(os.environ.get("MEMORY_STM_LAST_K_TURNS", "5"))

# LTM: semantic search config
LTM_TOP_K = int(os.environ.get("MEMORY_LTM_TOP_K", "3"))
LTM_RELEVANCE_THRESHOLD = float(os.environ.get("MEMORY_LTM_RELEVANCE_THRESHOLD", "0.5"))

# Namespace patterns
NS_INCIDENTS = "/incidents/"
NS_SERVICES = "/services/{service}/"
NS_PATTERNS = "/patterns/{incident_type}/"


def is_enabled() -> bool:
    """Check whether AgentCore memory is configured and available."""
    return bool(_sdk_available and MEMORY_ID)


# ---------------------------------------------------------------------------
# Client management
# ---------------------------------------------------------------------------

def _get_client():
    """Lazily initialise and return the MemoryClient singleton."""
    global _memory_client
    if _memory_client is not None:
        return _memory_client

    if not is_enabled():
        return None

    try:
        _memory_client = MemoryClient(region_name=AWS_REGION)  # type: ignore[name-defined]
        logger.info("AgentCore MemoryClient initialised (memory_id=%s)", MEMORY_ID)
        return _memory_client
    except Exception as exc:
        logger.warning("Failed to initialise MemoryClient: %s", exc)
        return None


def _get_session(session_id: str, actor_id: str = "sentinalai-agent"):
    """Create a MemorySessionManager for the given session."""
    if _session_manager_cls is None or not MEMORY_ID:
        return None
    try:
        mgr = _session_manager_cls(
            memory_id=MEMORY_ID,
            region_name=AWS_REGION,
        )
        return mgr.create_memory_session(
            actor_id=actor_id,
            session_id=session_id,
        )
    except Exception as exc:
        logger.warning("Failed to create memory session: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Short-Term Memory — store and retrieve recent investigation turns
# ---------------------------------------------------------------------------

def store_investigation_turn(
    session_id: str,
    role: str,
    content: str,
    actor_id: str = "sentinalai-agent",
) -> bool:
    """Store a single conversation turn in short-term memory.

    Args:
        session_id: Investigation or incident identifier
        role: "user" for incoming requests, "assistant" for RCA outputs
        content: The message content
        actor_id: Actor identifier (default: sentinalai-agent)

    Returns:
        True if stored successfully, False otherwise
    """
    if not is_enabled() or _message_cls is None or _role_cls is None:
        return False

    session = _get_session(session_id, actor_id)
    if session is None:
        return False

    try:
        msg_role = (
            _role_cls.USER if role == "user" else _role_cls.ASSISTANT
        )
        session.add_turns(
            messages=[_message_cls(content, msg_role)]
        )
        logger.debug("Stored STM turn: session=%s role=%s", session_id, role)
        return True
    except Exception as exc:
        logger.warning("Failed to store STM turn: %s", exc)
        return False


def get_recent_turns(
    session_id: str,
    k: int | None = None,
    actor_id: str = "sentinalai-agent",
) -> list[dict[str, str]]:
    """Retrieve the last K turns from short-term memory.

    Returns a list of dicts with 'role' and 'content' keys.
    """
    if not is_enabled():
        return []

    session = _get_session(session_id, actor_id)
    if session is None:
        return []

    try:
        turns = session.get_last_k_turns(k=k or STM_LAST_K_TURNS)
        results = []
        for turn in turns:
            if isinstance(turn, dict):
                results.append({
                    "role": turn.get("role", "unknown"),
                    "content": turn.get("content", {}).get("text", str(turn)),
                })
            elif isinstance(turn, list):
                for msg in turn:
                    results.append({
                        "role": msg.get("role", "unknown") if isinstance(msg, dict) else "unknown",
                        "content": (
                            msg.get("content", {}).get("text", str(msg))
                            if isinstance(msg, dict)
                            else str(msg)
                        ),
                    })
        return results
    except Exception as exc:
        logger.warning("Failed to retrieve STM turns: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Long-Term Memory — store completed investigations for future search
# ---------------------------------------------------------------------------

def store_investigation_result(
    incident_id: str,
    incident_type: str,
    service: str,
    root_cause: str,
    confidence: int,
    reasoning: str,
    evidence_summary: str = "",
    session_id: str | None = None,
    actor_id: str = "sentinalai-agent",
) -> bool:
    """Store a completed investigation result in long-term memory.

    This stores the investigation as a conversation turn which the configured
    LTM strategies (semantic, summary) will automatically extract and index.

    Args:
        incident_id: The incident identifier
        incident_type: Classification (timeout, oomkill, etc.)
        service: Affected service name
        root_cause: Determined root cause
        confidence: Confidence score (0-100)
        reasoning: Detailed reasoning
        evidence_summary: Brief summary of evidence collected
        session_id: Override session ID (defaults to incident_id)
        actor_id: Actor identifier

    Returns:
        True if stored successfully
    """
    if not is_enabled() or _message_cls is None or _role_cls is None:
        return False

    sid = session_id or f"investigation-{incident_id}"
    session = _get_session(sid, actor_id)
    if session is None:
        return False

    try:
        # Store the investigation request as a user turn
        request_content = (
            f"Investigate incident {incident_id} "
            f"(type: {incident_type}, service: {service})"
        )
        session.add_turns(
            messages=[_message_cls(request_content, _role_cls.USER)]
        )

        # Store the result as an assistant turn — this feeds LTM extraction
        result_content = json.dumps({
            "incident_id": incident_id,
            "incident_type": incident_type,
            "service": service,
            "root_cause": root_cause,
            "confidence": confidence,
            "reasoning": reasoning,
            "evidence_summary": evidence_summary,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, indent=2)

        session.add_turns(
            messages=[_message_cls(result_content, _role_cls.ASSISTANT)]
        )

        logger.info(
            "Stored investigation in LTM: incident=%s service=%s confidence=%d",
            incident_id, service, confidence,
        )
        return True
    except Exception as exc:
        logger.warning("Failed to store investigation in LTM: %s", exc)
        return False


def search_similar_incidents(
    service: str,
    query: str,
    top_k: int | None = None,
    namespace_prefix: str | None = None,
) -> list[dict[str, Any]]:
    """Search long-term memory for similar historical incidents.

    Args:
        service: Service name to filter results
        query: Natural language query (e.g., incident summary)
        top_k: Number of results to return
        namespace_prefix: LTM namespace to search (default: root)

    Returns:
        List of similar incident records with scores
    """
    if not is_enabled():
        return []

    session = _get_session(f"search-{service}", "sentinalai-agent")
    if session is None:
        return []

    try:
        search_query = f"{service}: {query}"
        results = session.search_long_term_memories(
            query=search_query,
            namespace_prefix=namespace_prefix or NS_INCIDENTS,
            top_k=top_k or LTM_TOP_K,
        )

        incidents = []
        for record in results:
            content = record.get("content", "") if isinstance(record, dict) else str(record)

            # Parse stored JSON content if possible
            try:
                parsed = json.loads(content) if isinstance(content, str) else content
            except (json.JSONDecodeError, TypeError):
                parsed = {"raw": content}

            incidents.append({
                "incident_id": parsed.get("incident_id", "unknown"),
                "incident_type": parsed.get("incident_type", "unknown"),
                "service": parsed.get("service", service),
                "root_cause": parsed.get("root_cause", ""),
                "confidence": parsed.get("confidence", 0),
                "reasoning": parsed.get("reasoning", ""),
                "score": record.get("score", 0.0) if isinstance(record, dict) else 0.0,
            })

        logger.info(
            "LTM search: service=%s query_len=%d results=%d",
            service, len(query), len(incidents),
        )
        return incidents

    except Exception as exc:
        logger.warning("LTM search failed: %s", exc)
        return []


def get_long_term_records(
    namespace_prefix: str = "/",
) -> list[dict[str, Any]]:
    """List all long-term memory records under a namespace.

    Useful for auditing what the agent has memorised.
    """
    if not is_enabled():
        return []

    session = _get_session("audit", "sentinalai-agent")
    if session is None:
        return []

    try:
        records = session.list_long_term_memory_records(
            namespace_prefix=namespace_prefix
        )
        return records if isinstance(records, list) else []
    except Exception as exc:
        logger.warning("Failed to list LTM records: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Cleanup / shutdown
# ---------------------------------------------------------------------------

def dispose() -> None:
    """Release memory client resources."""
    global _memory_client
    _memory_client = None
    logger.debug("Memory client disposed")
