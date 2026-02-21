"""Amazon Bedrock AgentCore runtime adapter for SentinalAI.

Implements the AgentCore HTTP contract:
- POST /invocations  -> run investigation
- GET  /ping         -> health check

Can be run in two modes:
1. With bedrock_agentcore SDK:  BedrockAgentCoreApp wraps this automatically
2. Standalone (FastAPI):        uvicorn agentcore_runtime:app --host 0.0.0.0 --port 8080

The SDK approach is preferred for production deployment on AgentCore.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import time
import uuid
from typing import Any

logger = logging.getLogger("sentinalai.agentcore")

# =========================================================================
# Attempt to use the official AgentCore SDK; fall back to FastAPI
# =========================================================================

_USE_SDK = False

try:
    from bedrock_agentcore.runtime import BedrockAgentCoreApp  # type: ignore[import-untyped]
    _USE_SDK = True
except ImportError:
    _USE_SDK = False


# =========================================================================
# Input validation
# =========================================================================

_INCIDENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,100}$")


def _validate_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    """Validate and extract incident_id from payload.

    Returns (incident_id, error_message). error_message is None on success.
    """
    incident_id = payload.get("incident_id") or payload.get("prompt", "")
    if not incident_id:
        return "", "Missing 'incident_id' in payload"
    if not isinstance(incident_id, str):
        return "", "incident_id must be a string"
    if not _INCIDENT_ID_PATTERN.match(incident_id):
        return "", "incident_id contains invalid characters (allowed: alphanumeric, -, _)"
    return incident_id, None


# =========================================================================
# Shared investigation handler
# =========================================================================

def _handle_invocation(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Process an investigation request.

    Expected payload:
        {
            "incident_id": "INC12345",
            "replay": false  (optional)
        }

    Returns RCA result dict with request metadata.
    """
    request_id = str(uuid.uuid4())
    start_time = time.monotonic()

    # Validate input
    incident_id, error = _validate_payload(payload)
    if error:
        logger.warning("Invalid request %s: %s", request_id, error)
        return {"error": error, "request_id": request_id}

    logger.info(
        "Investigation started: incident=%s request_id=%s",
        incident_id, request_id,
    )

    try:
        from supervisor.agent import SentinalAISupervisor

        replay = payload.get("replay", False)
        replay_dir = os.getenv("SENTINALAI_REPLAY_DIR", "/tmp/sentinalai_replays")

        supervisor = SentinalAISupervisor(replay_dir=replay_dir)
        result = supervisor.investigate(incident_id, replay=replay)

        elapsed_ms = round((time.monotonic() - start_time) * 1000, 1)

        logger.info(
            "Investigation complete: incident=%s confidence=%s elapsed=%sms request_id=%s",
            incident_id, result.get("confidence", 0), elapsed_ms, request_id,
        )

        return {
            "result": result,
            "incident_id": incident_id,
            "request_id": request_id,
            "elapsed_ms": elapsed_ms,
        }

    except Exception as e:
        elapsed_ms = round((time.monotonic() - start_time) * 1000, 1)
        logger.error(
            "Investigation failed: incident=%s error=%s elapsed=%sms request_id=%s",
            incident_id, e, elapsed_ms, request_id,
        )
        return {
            "error": f"Investigation failed: {e}",
            "incident_id": incident_id,
            "request_id": request_id,
            "elapsed_ms": elapsed_ms,
        }


# =========================================================================
# SDK mode (preferred)
# =========================================================================

if _USE_SDK:
    agentcore_app = BedrockAgentCoreApp()

    @agentcore_app.entrypoint
    def invoke(payload: dict, context=None):  # type: ignore[no-untyped-def]
        """AgentCore SDK entrypoint."""
        return _handle_invocation(payload, context)


# =========================================================================
# FastAPI fallback (local dev / non-SDK deployments)
# =========================================================================

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="SentinalAI AgentCore Runtime", version="0.1.0")

    @app.get("/ping")
    async def ping() -> dict:
        """Health check endpoint required by AgentCore.

        Returns component health status for monitoring dashboards.
        """
        health: dict[str, Any] = {"status": "healthy", "service": "sentinalai"}

        # Check database connectivity (non-blocking)
        try:
            from database.connection import check_health
            db_health = check_health()
            health["database"] = db_health.get("database", "unknown")
        except Exception:
            health["database"] = "check_failed"

        # Check AgentCore Memory status
        try:
            from supervisor.memory import is_enabled as memory_enabled
            health["memory"] = "enabled" if memory_enabled() else "disabled"
        except Exception:
            health["memory"] = "check_failed"

        return health

    @app.post("/invocations")
    async def invocations(request: Request) -> JSONResponse:
        """Investigation endpoint following AgentCore contract."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid JSON body"},
            )

        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "Request body must be a JSON object"},
            )

        result = _handle_invocation(body)

        status_code = 200 if "error" not in result else 422
        return JSONResponse(content=result, status_code=status_code)

except ImportError:
    # FastAPI not installed — SDK-only mode
    app = None  # type: ignore[assignment]


# =========================================================================
# Graceful shutdown
# =========================================================================

def _shutdown_handler(signum, frame):
    """Handle SIGTERM for graceful shutdown."""
    logger.info("Received signal %s, shutting down gracefully...", signum)
    try:
        from database.connection import dispose
        dispose()
    except Exception:
        pass
    try:
        from supervisor.memory import dispose as memory_dispose
        memory_dispose()
    except Exception:
        pass
    raise SystemExit(0)


# =========================================================================
# Entrypoint
# =========================================================================

def main() -> None:
    """Start the AgentCore runtime server."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Register graceful shutdown
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    if _USE_SDK:
        logger.info("Starting SentinalAI with AgentCore SDK on port 8080")
        agentcore_app.run()
    elif app is not None:
        import uvicorn
        logger.info("Starting SentinalAI with FastAPI on port 8080")
        uvicorn.run(app, host="0.0.0.0", port=8080)
    else:
        logger.error(
            "Neither bedrock-agentcore nor fastapi is installed. "
            "Install one of: pip install bedrock-agentcore  OR  pip install fastapi uvicorn"
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
