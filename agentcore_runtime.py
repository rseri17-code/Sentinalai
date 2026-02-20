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
# Shared investigation handler
# =========================================================================

def _handle_invocation(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Process an investigation request.

    Expected payload:
        {
            "incident_id": "INC12345",
            "replay": false  (optional)
        }

    Returns RCA result dict.
    """
    from supervisor.agent import SentinalAISupervisor

    incident_id = payload.get("incident_id") or payload.get("prompt", "")
    if not incident_id:
        return {"error": "Missing incident_id in payload"}

    replay = payload.get("replay", False)
    replay_dir = os.getenv("SENTINALAI_REPLAY_DIR", "/tmp/sentinalai_replays")

    supervisor = SentinalAISupervisor(replay_dir=replay_dir)
    result = supervisor.investigate(incident_id, replay=replay)

    return {
        "result": result,
        "incident_id": incident_id,
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

    app = FastAPI(title="SentinalAI AgentCore Runtime", version="1.0.0")

    @app.get("/ping")
    async def ping() -> dict:
        """Health check endpoint required by AgentCore."""
        return {"status": "healthy"}

    @app.post("/invocations")
    async def invocations(request: Request) -> JSONResponse:
        """Investigation endpoint following AgentCore contract."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid JSON body"},
            )
        result = _handle_invocation(body)
        return JSONResponse(content=result)

except ImportError:
    # FastAPI not installed — SDK-only mode
    app = None  # type: ignore[assignment]


# =========================================================================
# Entrypoint
# =========================================================================

def main() -> None:
    """Start the AgentCore runtime server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

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
