"""AG UI Event Bus — asyncio pub/sub backbone.

Design principles:
1. All agent events flow through this bus to the WebSocket layer
2. Thread-safe: agent runs in sync threads, bus is asyncio
3. Pluggable backends: in-process (default) → EventBridge → Kinesis
4. Idempotency: duplicate events are deduplicated by idempotency_key
5. Ordering: per-investigation FIFO via asyncio.Queue
6. Persistence: events are written to DynamoDB before broadcast

Failure modes:
- DynamoDB write failure: log + continue broadcast (best-effort persistence)
- No subscribers: events queued, delivered on reconnect
- Bus overload: bounded queue (maxsize=10000) drops oldest events

AWS upgrade path:
  Replace InProcessEventBus with KinesisEventBus or EventBridgeEventBus
  by implementing the EventBusBackend protocol.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Awaitable, Callable, Optional, Protocol, runtime_checkable

from agui.schemas.events import AGUIEvent

logger = logging.getLogger(__name__)

# Type alias for event handlers
EventHandler = Callable[[AGUIEvent], Awaitable[None]]

# Per-investigation event queue size
QUEUE_MAX_SIZE = 10_000


@runtime_checkable
class EventBusBackend(Protocol):
    """Pluggable backend for event persistence."""
    async def publish(self, event: AGUIEvent) -> None: ...
    async def get_events(
        self, investigation_id: str, since_seq: int = 0
    ) -> list[AGUIEvent]: ...


class InProcessEventBus:
    """
    Default in-process event bus using asyncio.

    Topology:
    - Producers (agent threads) → put_threadsafe()
    - Internal dispatcher loop → reads queue, deduplicates, broadcasts
    - Consumers (WS connections) → subscribe(investigation_id)

    Scalability note:
    For multi-instance deployments, replace with EventBridge or Kinesis.
    Each BFF instance would subscribe to the shared stream.
    """

    def __init__(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = loop
        # investigation_id → asyncio.Queue
        self._queues: dict[str, asyncio.Queue[AGUIEvent]] = {}
        # investigation_id → set of handler coroutines
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)
        # Deduplication: idempotency_key → True
        self._seen: dict[str, bool] = {}
        # Per-investigation event history (for late subscribers)
        self._history: dict[str, list[AGUIEvent]] = defaultdict(list)
        # Backend for persistence
        self._backend: Optional[EventBusBackend] = None
        self._running = False
        self._dispatch_task: Optional[asyncio.Task] = None

    def set_backend(self, backend: EventBusBackend) -> None:
        self._backend = backend

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def start(self) -> None:
        """Start the dispatcher loop."""
        self._running = True
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        logger.info("EventBus started")

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
        logger.info("EventBus stopped")

    def put_threadsafe(self, event: AGUIEvent) -> None:
        """Thread-safe event publication from sync agent threads."""
        if self._loop is None or self._loop.is_closed():
            logger.warning("EventBus: no event loop, dropping event %s", event.event_id)
            return
        queue = self._get_or_create_queue(event.investigation_id)
        try:
            # Non-blocking: if queue is full, drop the oldest
            if queue.full():
                try:
                    queue.get_nowait()
                    logger.warning("EventBus: queue full, dropped oldest event")
                except asyncio.QueueEmpty:
                    pass
            asyncio.run_coroutine_threadsafe(
                queue.put(event), self._loop
            )
        except Exception as e:
            logger.error("EventBus: failed to enqueue event: %s", e)

    async def publish(self, event: AGUIEvent) -> None:
        """Async event publication from async contexts (BFF, control API)."""
        queue = self._get_or_create_queue(event.investigation_id)
        await queue.put(event)

    def subscribe(self, investigation_id: str, handler: EventHandler) -> None:
        """Register a WebSocket handler for an investigation's events."""
        self._subscribers[investigation_id].append(handler)
        logger.debug(
            "EventBus: subscriber added for %s (total: %d)",
            investigation_id,
            len(self._subscribers[investigation_id]),
        )

    def unsubscribe(self, investigation_id: str, handler: EventHandler) -> None:
        """Deregister a WebSocket handler."""
        subs = self._subscribers.get(investigation_id, [])
        if handler in subs:
            subs.remove(handler)

    async def get_history(
        self, investigation_id: str, since_seq: int = 0
    ) -> list[AGUIEvent]:
        """Return buffered events for late-connecting subscribers."""
        if self._backend:
            return await self._backend.get_events(investigation_id, since_seq)
        history = self._history.get(investigation_id, [])
        return [e for e in history if e.sequence_num >= since_seq]

    def _get_or_create_queue(self, investigation_id: str) -> asyncio.Queue:
        if investigation_id not in self._queues:
            self._queues[investigation_id] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        return self._queues[investigation_id]

    async def _dispatch_loop(self) -> None:
        """Main dispatch loop: deduplicates and broadcasts events."""
        while self._running:
            dispatched = False
            for inv_id, queue in list(self._queues.items()):
                while not queue.empty():
                    try:
                        event = queue.get_nowait()
                        await self._process_event(event)
                        dispatched = True
                    except asyncio.QueueEmpty:
                        break
                    except Exception as e:
                        logger.error("EventBus: dispatch error: %s", e)
            if not dispatched:
                await asyncio.sleep(0.01)  # 10ms poll interval

    async def _process_event(self, event: AGUIEvent) -> None:
        """Deduplicate, persist, and broadcast a single event."""
        # Deduplication
        if event.idempotency_key in self._seen:
            logger.debug("EventBus: duplicate event %s, skipping", event.idempotency_key)
            return
        self._seen[event.idempotency_key] = True

        # Trim seen set to avoid unbounded growth
        if len(self._seen) > 100_000:
            # Remove oldest 10k entries (dict insertion order in Python 3.7+)
            keys_to_remove = list(self._seen.keys())[:10_000]
            for k in keys_to_remove:
                del self._seen[k]

        # Persist to backend (best-effort)
        if self._backend:
            try:
                await self._backend.publish(event)
            except Exception as e:
                logger.error("EventBus: backend persist failed: %s", e)

        # Buffer in history
        self._history[event.investigation_id].append(event)
        # Keep last 10k events per investigation
        if len(self._history[event.investigation_id]) > 10_000:
            self._history[event.investigation_id] = \
                self._history[event.investigation_id][-10_000:]

        # Broadcast to subscribers
        handlers = list(self._subscribers.get(event.investigation_id, []))
        for handler in handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.error("EventBus: handler error: %s", e)

        # Also broadcast to wildcard subscribers (all investigations)
        for handler in list(self._subscribers.get("*", [])):
            try:
                await handler(event)
            except Exception as e:
                logger.error("EventBus: wildcard handler error: %s", e)


# Global bus instance — initialized in main.py
_bus: Optional[InProcessEventBus] = None


def get_bus() -> InProcessEventBus:
    global _bus
    if _bus is None:
        _bus = InProcessEventBus()
    return _bus


def init_bus(loop: asyncio.AbstractEventLoop) -> InProcessEventBus:
    global _bus
    _bus = InProcessEventBus(loop=loop)
    return _bus
