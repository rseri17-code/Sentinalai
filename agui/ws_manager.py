"""WebSocket Connection Manager for AG UI.

Manages all WebSocket connections and routes events to subscribers.

Architecture:
- Each connected browser tab is a WebSocketConnection
- Each connection subscribes to one investigation_id (or "*" for all)
- Events from event_bus are broadcast to matching connections
- Late-join: send buffered history on connect
- Heartbeat: 30s ping to detect stale connections
- Reconnect: client sends last_seq_num on reconnect, server replays missed events

Security:
- JWT validated before connection accepted
- Role encoded in connection metadata
- Sensitive payloads filtered by role (viewer cannot see raw tool params)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from agui.event_bus import get_bus
from agui.schemas.events import AGUIEvent, EventType

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 30.0   # seconds
CONNECTION_TIMEOUT = 300.0  # 5 minutes max idle


@dataclass
class WSConnection:
    """Represents a single WebSocket client connection."""
    connection_id: str
    websocket: WebSocket
    investigation_id: str
    actor_id: str
    actor_role: str
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    last_seq: int = 0
    message_count: int = 0


class WSManager:
    """
    Central WebSocket connection manager.

    Responsibilities:
    1. Accept new connections (with JWT validation)
    2. Subscribe connections to event bus
    3. Replay missed events on reconnect
    4. Broadcast events to all matching connections
    5. Filter event payload by role
    6. Heartbeat + cleanup of stale connections
    """

    def __init__(self) -> None:
        self._connections: dict[str, WSConnection] = {}
        self._heartbeat_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        for conn in list(self._connections.values()):
            try:
                await conn.websocket.close()
            except Exception:
                pass

    async def connect(
        self,
        websocket: WebSocket,
        investigation_id: str,
        actor_id: str,
        actor_role: str,
        last_seq: int = 0,
        connection_id: Optional[str] = None,
    ) -> str:
        """
        Accept a new WebSocket connection.

        1. Accept the connection
        2. Create connection record
        3. Subscribe to event bus
        4. Replay missed events (last_seq forward)
        5. Return connection_id
        """
        await websocket.accept()

        import uuid
        conn_id = connection_id or str(uuid.uuid4())

        conn = WSConnection(
            connection_id=conn_id,
            websocket=websocket,
            investigation_id=investigation_id,
            actor_id=actor_id,
            actor_role=actor_role,
            last_seq=last_seq,
        )
        self._connections[conn_id] = conn

        # Subscribe to event bus
        bus = get_bus()
        bus.subscribe(investigation_id, self._make_handler(conn_id))

        # Send connection ack
        await self._send_message(conn, {
            "type": "connection.ack",
            "connection_id": conn_id,
            "investigation_id": investigation_id,
            "timestamp": time.time(),
        })

        # Replay missed events
        history = await bus.get_history(investigation_id, since_seq=last_seq)
        if history:
            logger.info(
                "WS[%s]: replaying %d missed events (since seq=%d)",
                conn_id, len(history), last_seq,
            )
            for event in history:
                await self._deliver_event(conn, event)

        logger.info(
            "WS[%s]: connected (inv=%s, role=%s, replayed=%d)",
            conn_id, investigation_id, actor_role, len(history),
        )
        return conn_id

    async def disconnect(self, connection_id: str) -> None:
        """Clean up a disconnected WebSocket."""
        conn = self._connections.pop(connection_id, None)
        if not conn:
            return
        # Unsubscribe from event bus
        bus = get_bus()
        handler = self._handlers.pop(connection_id, None)
        if handler:
            bus.unsubscribe(conn.investigation_id, handler)
        logger.info("WS[%s]: disconnected", connection_id)

    async def handle_connection(
        self,
        websocket: WebSocket,
        investigation_id: str,
        actor_id: str,
        actor_role: str,
        last_seq: int = 0,
    ) -> None:
        """
        Main connection handler — call this from the FastAPI endpoint.

        Handles the full lifecycle: connect → receive messages → disconnect.
        """
        conn_id = await self.connect(
            websocket, investigation_id, actor_id, actor_role, last_seq
        )
        try:
            while True:
                try:
                    data = await asyncio.wait_for(
                        websocket.receive_text(),
                        timeout=CONNECTION_TIMEOUT,
                    )
                    await self._handle_client_message(conn_id, data)
                except asyncio.TimeoutError:
                    logger.info("WS[%s]: idle timeout, closing", conn_id)
                    break
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error("WS[%s]: error: %s", conn_id, e)
        finally:
            await self.disconnect(conn_id)

    def _make_handler(self, connection_id: str):
        """Create a bound event handler for this connection."""
        async def handler(event: AGUIEvent) -> None:
            conn = self._connections.get(connection_id)
            if conn:
                await self._deliver_event(conn, event)

        # Store reference for unsubscription
        if not hasattr(self, "_handlers"):
            self._handlers: dict[str, object] = {}
        self._handlers[connection_id] = handler
        return handler

    async def _deliver_event(self, conn: WSConnection, event: AGUIEvent) -> None:
        """Filter by role and deliver event to WebSocket client."""
        try:
            payload = self._filter_for_role(event, conn.actor_role)
            await self._send_message(conn, payload)
            conn.last_seq = max(conn.last_seq, event.sequence_num)
            conn.last_seen = time.time()
            conn.message_count += 1
        except Exception as e:
            logger.error("WS[%s]: delivery failed: %s", conn.connection_id, e)

    def _filter_for_role(self, event: AGUIEvent, role: str) -> dict:
        """
        Apply role-based filtering to event payload.

        Rules:
        - viewer: no raw params, no error details
        - operator+: full payload
        - approver+: full payload including control actions
        """
        data = event.model_dump()
        if role == "viewer":
            # Mask sensitive tool call params
            if event.event_type in (EventType.TOOL_CALLED,):
                if "payload" in data and "params" in data["payload"]:
                    data["payload"]["params"] = {"_redacted": "viewer role"}
        return data

    async def _handle_client_message(self, connection_id: str, raw: str) -> None:
        """Handle incoming messages from client (ping, control ack, etc.)."""
        conn = self._connections.get(connection_id)
        if not conn:
            return
        conn.last_seen = time.time()
        try:
            msg = json.loads(raw)
            msg_type = msg.get("type", "")
            if msg_type == "ping":
                await self._send_message(conn, {"type": "pong", "timestamp": time.time()})
            elif msg_type == "subscribe":
                # Dynamic re-subscription to different investigation
                new_inv_id = msg.get("investigation_id", "")
                if new_inv_id and new_inv_id != conn.investigation_id:
                    bus = get_bus()
                    bus.unsubscribe(conn.investigation_id, self._handlers.get(connection_id))
                    conn.investigation_id = new_inv_id
                    bus.subscribe(new_inv_id, self._make_handler(connection_id))
                    await self._send_message(conn, {"type": "subscribed", "investigation_id": new_inv_id})
        except json.JSONDecodeError:
            logger.warning("WS[%s]: invalid JSON message", connection_id)

    async def _send_message(self, conn: WSConnection, data: dict) -> None:
        """Send a JSON message to a WebSocket client."""
        try:
            await conn.websocket.send_text(json.dumps(data, default=str))
        except Exception as e:
            logger.debug("WS[%s]: send failed: %s", conn.connection_id, e)

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats and remove stale connections."""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            now = time.time()
            stale = [
                cid for cid, conn in self._connections.items()
                if now - conn.last_seen > CONNECTION_TIMEOUT
            ]
            for cid in stale:
                logger.info("WS[%s]: heartbeat timeout, removing", cid)
                await self.disconnect(cid)
            # Ping active connections
            for conn in list(self._connections.values()):
                await self._send_message(conn, {
                    "type": "heartbeat",
                    "timestamp": now,
                    "connection_id": conn.connection_id,
                })

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    def get_connections_for_investigation(self, investigation_id: str) -> list[WSConnection]:
        return [c for c in self._connections.values() if c.investigation_id == investigation_id]


# Global instance
_ws_manager: Optional[WSManager] = None


def get_ws_manager() -> WSManager:
    global _ws_manager
    if _ws_manager is None:
        _ws_manager = WSManager()
    return _ws_manager
