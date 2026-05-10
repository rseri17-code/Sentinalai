"""AG UI BFF (Backend for Frontend) — main FastAPI application.

This is the single entry point for the AG UI backend.

Responsibilities:
1. Serve REST API for investigation management, receipts, replay, memory, control
2. Serve WebSocket endpoint for real-time event streaming
3. Initialize event bus, WebSocket manager, state store
4. Bridge agent events to WebSocket clients
5. Enforce authentication (JWT + RBAC)
6. Propagate trace_id across all requests

Port: 8081 (separate from agentcore_runtime.py on 8080)

Security:
- CORS configured for UI origin
- JWT validated on every request
- Rate limiting on control endpoints (future)
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib as _pathlib
import time

import uvicorn
from fastapi import FastAPI, WebSocket, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from agui.event_bus import init_bus, get_bus
from agui.ws_manager import get_ws_manager
from agui.middleware.auth import get_actor, ActorContext
from agui.middleware.trace import TraceMiddleware
from agui.middleware.honeypot import HoneypotMiddleware, generate_invite_token
from agui.api.incidents import router as incidents_router
from agui.api.investigations import router as investigations_router
from agui.api.receipts import router as receipts_router
from agui.api.replay import router as replay_router
from agui.api.memory import router as memory_router
from agui.api.control import router as control_router
from agui.api.learning import router as learning_router
from agui.api.metrics import router as metrics_router
from agui.api.intake import router as intake_router
from agui.api.intelligence import router as intelligence_router
from agui.api.harness import router as harness_router
from agui.api.transparency import router as transparency_router
from agui.api.postmortem import router as postmortem_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

ALLOWED_ORIGINS = os.getenv(
    "AGUI_CORS_ORIGINS",
    "http://localhost:5173,http://localhost:3000,http://localhost:8081",
).split(",")

BFF_PORT = int(os.getenv("AGUI_BFF_PORT", "8081"))
BFF_HOST = os.getenv("AGUI_BFF_HOST", "0.0.0.0")


def create_app() -> FastAPI:
    app = FastAPI(
        title="ObserveAI AG UI — BFF",
        description=(
            "Backend for Frontend for the ObserveAI Agent Control UI. "
            "Provides real-time event streaming, execution graph, receipt evidence, "
            "memory tracing, deterministic replay, and human-in-the-loop controls."
        ),
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    # Middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Trace-Id", "X-XRay-Url"],
    )
    app.add_middleware(TraceMiddleware)

    # Honeypot gate: must be outermost so it intercepts before any API handler
    app.add_middleware(HoneypotMiddleware)

    # REST API routes
    app.include_router(incidents_router)
    app.include_router(investigations_router)
    app.include_router(receipts_router)
    app.include_router(replay_router)
    app.include_router(memory_router)
    app.include_router(control_router)
    app.include_router(learning_router)
    app.include_router(metrics_router)
    app.include_router(intake_router)
    app.include_router(intelligence_router)
    app.include_router(harness_router)
    app.include_router(transparency_router)
    app.include_router(postmortem_router)

    # ── Tenant management endpoints ───────────────────────────────────────
    @app.get("/api/v1/tenants", tags=["tenants"])
    async def list_tenants(actor: ActorContext = Depends(get_actor)):
        """List all configured tenant org IDs."""
        from integrations.tenant_config import list_tenants as _list
        return {"tenants": _list()}

    @app.get("/api/v1/tenants/{org_id}", tags=["tenants"])
    async def get_tenant(org_id: str, actor: ActorContext = Depends(get_actor)):
        """Return config for a specific tenant."""
        from integrations.tenant_config import get_tenant_config
        cfg = get_tenant_config(org_id)
        return cfg.to_dict()

    @app.post("/api/v1/tenants/{org_id}", tags=["tenants"])
    async def upsert_tenant(org_id: str, body: dict, actor: ActorContext = Depends(get_actor)):
        """Create or update a tenant configuration."""
        from agui.middleware.auth import ROLE_HIERARCHY
        if ROLE_HIERARCHY.get(actor.actor_role, 0) < ROLE_HIERARCHY.get("admin", 99):
            return JSONResponse(status_code=403, content={"detail": "Admin only"})
        from integrations.tenant_config import upsert_tenant as _upsert
        cfg = _upsert(org_id, body)
        return {"ok": True, "tenant": cfg.to_dict()}

    @app.post("/api/v1/tenants/{org_id}/seed", tags=["tenants"])
    async def seed_tenant(
        org_id: str,
        force: bool = False,
        actor: ActorContext = Depends(get_actor),
    ):
        """Run the cold-start seeder for a new tenant (idempotent)."""
        from agui.middleware.auth import ROLE_HIERARCHY
        if ROLE_HIERARCHY.get(actor.actor_role, 0) < ROLE_HIERARCHY.get("admin", 99):
            return JSONResponse(status_code=403, content={"detail": "Admin only"})
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: __import__("supervisor.cold_start_seeder", fromlist=["seed_tenant"]).seed_tenant(org_id, force=force),
        )
        return {"ok": True, "org_id": org_id, **result}

    # ── Health endpoints ──────────────────────────────────────────────────
    @app.get("/api/v1/health", tags=["health"])
    async def health_check():
        """Basic liveness probe."""
        return {"status": "ok", "service": "sentinalai-bff", "version": "1.0.0"}

    @app.get("/api/v1/health/tools", tags=["health"])
    async def tools_health():
        """Report connection status for all external tool integrations.

        Returns a dict of tool_name → {connected, mode, env_var, details}.
        Modes:
          live   — real MCP endpoint configured and reachable
          stub   — no env var set; using stub/fixture responses (dev mode)
          error  — env var set but connection failed

        Use this endpoint to verify which tools are connected before
        running investigations in production.
        """
        import os
        from workers.mcp_client import McpGateway

        gateway = McpGateway.get_instance()
        gateway_mode = getattr(gateway, "_mode", "unknown")

        tools = {
            "servicenow": {
                "env_var": "SERVICENOW_MCP_URL",
                "configured": bool(os.environ.get("SERVICENOW_MCP_URL")),
                "description": "CMDB, change records, incident write-back",
            },
            "github": {
                "env_var": "GITHUB_MCP_URL",
                "configured": bool(os.environ.get("GITHUB_MCP_URL")),
                "description": "Deployment history, code diffs, PR creation",
            },
            "splunk": {
                "env_var": "SPLUNK_MCP_URL",
                "configured": bool(os.environ.get("SPLUNK_MCP_URL")),
                "description": "Log aggregation, error search",
            },
            "sysdig": {
                "env_var": "SYSDIG_MCP_URL",
                "configured": bool(os.environ.get("SYSDIG_MCP_URL")),
                "description": "Infrastructure metrics, golden signals",
            },
            "dynatrace": {
                "env_var": "DYNATRACE_MCP_URL",
                "configured": bool(os.environ.get("DYNATRACE_MCP_URL")),
                "description": "APM, distributed tracing, error sampling",
            },
            "moogsoft": {
                "env_var": "MOOGSOFT_MCP_URL",
                "configured": bool(os.environ.get("MOOGSOFT_MCP_URL")),
                "description": "Alert correlation, incident intake",
            },
            "confluence": {
                "env_var": "CONFLUENCE_MCP_URL",
                "configured": bool(os.environ.get("CONFLUENCE_MCP_URL")),
                "description": "Runbooks, post-mortems, knowledge base",
            },
            "kubernetes": {
                "env_var": "KUBERNETES_MCP_URL",
                "configured": bool(os.environ.get("KUBERNETES_MCP_URL")),
                "description": "Pod management, rollback, scaling",
            },
        }

        # LLM connectivity
        llm_provider = os.environ.get("LLM_PROVIDER", "anthropic")
        llm_key_set = bool(
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("BEDROCK_REGION")
        )
        tools["llm"] = {
            "env_var": "ANTHROPIC_API_KEY / OPENAI_API_KEY / BEDROCK_REGION",
            "configured": llm_key_set,
            "description": f"LLM provider ({llm_provider}) for hypothesis reasoning and diff analysis",
        }

        # Compute summary stats
        connected_count = sum(1 for t in tools.values() if t["configured"])
        total = len(tools)
        mode = "live" if gateway_mode == "live" else "stub"

        for tool_name, info in tools.items():
            info["mode"] = "live" if info["configured"] else "stub"
            info["status"] = "connected" if info["configured"] else "stub_mode"

        return {
            "gateway_mode": mode,
            "tools_connected": connected_count,
            "tools_total": total,
            "tools_in_stub_mode": total - connected_count,
            "ready_for_production": connected_count >= 5,
            "tools": tools,
            "setup_instructions": (
                "Set the env vars listed above to connect each tool. "
                "See .env.example for the complete list."
            ),
        }

    return app


app = create_app()


# ── Lifecycle ────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event() -> None:
    """Initialize all async components on startup."""
    loop = asyncio.get_event_loop()

    # Initialize event bus
    bus = init_bus(loop)
    await bus.start()
    logger.info("Event bus started")

    # Initialize WebSocket manager
    ws_manager = get_ws_manager()
    await ws_manager.start()
    logger.info("WebSocket manager started")

    # Wire event bus persistence backend (DynamoDB if available)
    from agui.state_store import get_state_store
    state_store = get_state_store()

    class StateBusBackend:
        async def publish(self, event) -> None:
            await state_store.put_event(event)

        async def get_events(self, investigation_id: str, since_seq: int = 0):
            return await state_store.get_events(investigation_id, since_seq)

    bus.set_backend(StateBusBackend())

    # Start ops persistence (SQLite write queue + integrity check + retention cleanup)
    try:
        from database.ops_persistence import get_ops_store
        get_ops_store()  # triggers start() via singleton initialisation
        logger.info("Ops persistence store ready")
    except Exception as exc:
        logger.warning("Ops persistence startup failed (non-fatal): %s", exc)

    # Start Pattern Intelligence background loop
    try:
        from intelligence.background_runner import get_runner as get_intelligence_runner
        await get_intelligence_runner().start()
        logger.info("Pattern Intelligence runner started")
    except Exception as exc:
        logger.warning("Pattern Intelligence runner failed to start: %s", exc)

    logger.info("AG UI BFF ready on port %d", BFF_PORT)


@app.on_event("shutdown")
async def shutdown_event() -> None:
    """Graceful shutdown."""
    try:
        from database.ops_persistence import get_ops_store
        get_ops_store().stop()
    except Exception:
        pass
    try:
        from intelligence.background_runner import get_runner as get_intelligence_runner
        await get_intelligence_runner().stop()
    except Exception:
        pass
    bus = get_bus()
    await bus.stop()
    ws_manager = get_ws_manager()
    await ws_manager.stop()
    logger.info("AG UI BFF shutdown complete")


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws/investigations/{investigation_id}")
async def ws_investigation(
    websocket: WebSocket,
    investigation_id: str,
    last_seq: int = Query(default=0),
):
    """
    WebSocket endpoint for real-time investigation event streaming.

    Authentication (in priority order):
      1. sentinalai_session cookie (set by honeypot invite-link flow)
      2. Authorization header: "Bearer <JWT>" (sent as WS subprotocol or
         via the Sec-WebSocket-Protocol header from the client)

    Do NOT pass tokens as query parameters — they appear in server logs
    and browser history in plaintext.

    Query params:
      last_seq → Last received sequence number (for reconnect replay)

    Message types received from server:
      connection.ack     → Connection accepted
      {event}            → AGUIEvent
      heartbeat          → Periodic keepalive
      pong               → Response to client ping

    Message types sent by client:
      ping               → Keepalive
      subscribe          → Switch investigation subscription
    """
    # Authenticate WebSocket — prefer cookie, fall back to Authorization header
    actor_id = "anonymous"
    actor_role = "viewer"

    from agui.middleware.auth import AUTH_REQUIRED, _decode_jwt, _get_actor_from_claims
    from agui.middleware.honeypot import validate_session_cookie, SESSION_COOKIE

    # 1. Session cookie (set after invite-link redemption)
    cookie_val = websocket.cookies.get(SESSION_COOKIE, "")
    cookie_actor = validate_session_cookie(cookie_val) if cookie_val else None

    # 2. Authorization header (JWT)
    auth_header = websocket.headers.get("Authorization", "")
    jwt_token = auth_header.removeprefix("Bearer ").strip() if auth_header else ""

    if cookie_actor:
        actor_id = cookie_actor
        actor_role = "operator"
    elif AUTH_REQUIRED and jwt_token:
        try:
            claims = _decode_jwt(jwt_token)
            actor = _get_actor_from_claims(claims)
            actor_id = actor.actor_id
            actor_role = actor.actor_role
        except Exception as e:
            logger.warning("WS auth failed: %s", e)
            await websocket.close(code=4001, reason="Authentication failed")
            return
    elif AUTH_REQUIRED and not cookie_actor:
        await websocket.close(code=4001, reason="Authentication required")
        return
    elif not AUTH_REQUIRED:
        actor_id = "dev-user"
        actor_role = "admin"

    ws_manager = get_ws_manager()
    await ws_manager.handle_connection(
        websocket=websocket,
        investigation_id=investigation_id,
        actor_id=actor_id,
        actor_role=actor_role,
        last_seq=last_seq,
    )


# ── Health + Status ───────────────────────────────────────────────────────────

@app.get("/ping")
async def ping():
    """Health check endpoint."""
    return {"status": "ok", "service": "agui-bff", "timestamp": time.time()}


@app.get("/api/v1/status")
async def status(actor: ActorContext = Depends(get_actor)):
    """System status — active connections, bus health, store health."""
    ws_manager = get_ws_manager()
    bus = get_bus()
    return {
        "service": "agui-bff",
        "version": "1.0.0",
        "timestamp": time.time(),
        "ws_connections": ws_manager.connection_count,
        "bus_investigations": len(bus._queues),
        "bus_history_size": sum(len(v) for v in bus._history.values()),
        "actor": {
            "id": actor.actor_id,
            "role": actor.actor_role,
        },
    }


@app.get("/api/v1/auth/token")
async def get_test_token(
    role: str = Query(default="admin"),
    actor_id: str = Query(default="test-user"),
):
    """
    Generate a test JWT token (DEV MODE ONLY).
    Disabled when AGUI_AUTH_REQUIRED=true.
    """
    from agui.middleware.auth import AUTH_REQUIRED, make_test_token
    if AUTH_REQUIRED:
        return JSONResponse(
            status_code=403,
            content={"detail": "Test token endpoint disabled in production"},
        )
    token = make_test_token(actor_id=actor_id, role=role)
    return {"token": token, "role": role, "actor_id": actor_id}


# ── Synthetic data injection (for testing/demo) ───────────────────────────────

@app.post("/api/v1/dev/inject")
async def inject_synthetic_investigation(
    incident_type: str = Query(default="error_spike"),
    actor: ActorContext = Depends(get_actor),
):
    """
    DEV ONLY: Inject a synthetic investigation for UI testing.
    Streams events to WebSocket subscribers.
    """
    from agui.middleware.auth import AUTH_REQUIRED
    if AUTH_REQUIRED:
        return JSONResponse(status_code=403, content={"detail": "Dev endpoint disabled"})

    from agui.synthetic_generator import SyntheticIncidentGenerator
    gen = SyntheticIncidentGenerator()
    investigation_id, events = await gen.generate_investigation(incident_type)

    bus = get_bus()
    # Stream events with realistic delays
    async def stream():
        for event in events:
            await bus.publish(event)
            await asyncio.sleep(0.3)

    asyncio.create_task(stream())

    return {
        "investigation_id": investigation_id,
        "incident_type": incident_type,
        "event_count": len(events),
        "ws_url": f"/ws/investigations/{investigation_id}",
    }


@app.get("/demo", response_class=HTMLResponse, include_in_schema=False)
async def demo_dashboard():
    """Serve the self-learning demo dashboard (no build step required)."""
    import pathlib
    html_path = pathlib.Path(__file__).parent / "demo.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# ── Invite token management ───────────────────────────────────────────────────

@app.post("/api/v1/admin/invite", include_in_schema=False)
async def create_invite(actor: ActorContext = Depends(get_actor)):
    """Generate a new invite link (admin only)."""
    from agui.middleware.auth import ROLE_HIERARCHY
    if ROLE_HIERARCHY.get(actor.actor_role, 0) < ROLE_HIERARCHY["admin"]:
        return JSONResponse(status_code=403, content={"detail": "Admin only"})
    token = generate_invite_token()
    host = os.getenv("AGUI_PUBLIC_URL", "http://localhost:8081")
    return {
        "token": token,
        "invite_url": f"{host}/?invite={token}",
        "expires_in_days": 7,
    }


# ── React SPA static file serving ────────────────────────────────────────────
# Mount AFTER all API routes so /api/* routes take precedence.
# Serve ui/dist/ at root; catch-all returns index.html for React Router.

_UI_DIST = _pathlib.Path(__file__).parent.parent / "ui" / "dist"

if _UI_DIST.exists():
    # Serve static assets (JS, CSS, images)
    app.mount("/assets", StaticFiles(directory=str(_UI_DIST / "assets")), name="spa-assets")

    @app.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
    async def serve_spa(full_path: str):
        """Catch-all: return React SPA index.html for client-side routing."""
        index = _UI_DIST / "index.html"
        return HTMLResponse(content=index.read_text(encoding="utf-8"))
else:
    logger.warning("React build not found at %s — run 'npm run build' in ui/", _UI_DIST)


if __name__ == "__main__":
    uvicorn.run(
        "agui.main:app",
        host=BFF_HOST,
        port=BFF_PORT,
        reload=os.getenv("AGUI_DEV_RELOAD", "false").lower() == "true",
        log_level="info",
    )
