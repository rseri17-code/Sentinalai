"""FastAPI Streamable HTTP MCP server for the OSS validation gateway."""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from oss_validation_gateway import __version__
from oss_validation_gateway.backends import Backends, Settings, UrlLibTransport
from oss_validation_gateway.dispatch import dispatch as dispatch_tool
from oss_validation_gateway.names import tool_catalog
from oss_validation_gateway.protocol import encode_sse, handle_rpc

logger = logging.getLogger("sentinalai.oss_validation_gateway")


def _backends_from_settings(settings: Settings) -> Backends:
    transport = UrlLibTransport(
        tls_verify=settings.kube_tls_verify,
        ca_file=settings.kubernetes_ca_file or None,
    )
    return Backends(settings=settings, transport=transport)


def create_app(
    settings: Settings | None = None,
    backends: Backends | None = None,
) -> FastAPI:
    cfg = settings or Settings.from_env()
    gw = backends or _backends_from_settings(cfg)

    app = FastAPI(
        title="SentinalAI OSS Validation Gateway",
        version=__version__,
        description="AgentCore-named MCP shim over Prometheus / Loki / Alertmanager / Kubernetes.",
    )
    app.state.settings = cfg
    app.state.backends = gw
    app.state.sessions = {}

    def _dispatch(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        return dispatch_tool(name, arguments, app.state.backends)

    def _check_auth(request: Request) -> JSONResponse | None:
        token = app.state.settings.gateway_token
        if not token:
            return None
        header = request.headers.get("authorization") or ""
        expected = f"Bearer {token}"
        if header != expected:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return None

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "mode": "oss-validation",
            "version": __version__,
            "backends": {
                "prometheus": app.state.settings.prometheus_url,
                "loki": app.state.settings.loki_url,
                "alertmanager": app.state.settings.alertmanager_url,
                "kubernetes": bool(app.state.settings.kubernetes_api_url),
            },
        }

    @app.get("/tools")
    def tools() -> dict[str, Any]:
        return {"tools": tool_catalog()}

    @app.post("/invoke")
    async def invoke_agentcore(request: Request) -> JSONResponse:
        """Curl-friendly AgentCore-style invoke (not used by McpGateway)."""
        denied = _check_auth(request)
        if denied is not None:
            return denied
        body = await request.json()
        tool_name = str(body.get("toolName") or body.get("name") or "")
        params = body.get("toolInput") or body.get("input") or body.get("arguments") or {}
        if not isinstance(params, dict):
            params = {}
        result = _dispatch(tool_name, params)
        return JSONResponse(result)

    async def _handle_mcp(request: Request) -> Response:
        denied = _check_auth(request)
        if denied is not None:
            return denied
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                status_code=400,
            )
        session = request.headers.get("mcp-session-id")
        messages = payload if isinstance(payload, list) else [payload]
        responses: list[dict[str, Any]] = []
        out_session = session or str(uuid.uuid4())
        for message in messages:
            if not isinstance(message, dict):
                continue
            rpc, out_session = handle_rpc(message, dispatch=_dispatch, session_id=out_session)
            if rpc is not None:
                responses.append(rpc)
        app.state.sessions[out_session] = out_session

        headers = {
            "Mcp-Session-Id": out_session,
            "MCP-Protocol-Version": request.headers.get("mcp-protocol-version", "2025-03-26"),
        }

        if not responses:
            return Response(status_code=202, headers=headers)

        body_obj: Any = responses if isinstance(payload, list) else responses[0]
        accept = (request.headers.get("accept") or "").lower()
        wants_sse = "text/event-stream" in accept and "application/json" not in accept
        if wants_sse:
            chunks = [encode_sse(item) for item in (body_obj if isinstance(body_obj, list) else [body_obj])]
            return PlainTextResponse(
                "".join(chunks),
                media_type="text/event-stream",
                headers=headers,
            )
        return JSONResponse(body_obj, headers=headers)

    @app.post("/mcp")
    async def mcp_post(request: Request) -> Response:
        return await _handle_mcp(request)

    @app.post("/")
    async def mcp_root_post(request: Request) -> Response:
        return await _handle_mcp(request)

    @app.get("/mcp")
    def mcp_get() -> Response:
        # Spec allows 405 when the server does not stream server-initiated messages.
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})

    @app.delete("/mcp")
    def mcp_delete(request: Request) -> Response:
        sid = request.headers.get("mcp-session-id")
        if sid:
            app.state.sessions.pop(sid, None)
        return Response(status_code=200)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("OSS_GATEWAY_HOST", "0.0.0.0")
    port = int(os.environ.get("OSS_GATEWAY_PORT", "9080"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger.info("OSS validation gateway listening on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
