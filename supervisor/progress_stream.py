"""Real-time investigation progress streaming for SentinalAI.

Emits structured events at every meaningful phase of an investigation so
that consumers (WebSocket handlers, SSE endpoints, AGUI event bus, tests)
can track progress without polling.

Event model:
    Every event has a type, investigation_id, phase, payload, and timestamp.
    Events are published to a thread-safe queue and delivered to registered
    subscribers via callbacks.

Event types:
    INVESTIGATION_STARTED   — investigation kicked off
    PHASE_STARTED           — a named phase began (collect, analyze, cite, ...)
    PHASE_COMPLETED         — a named phase finished with summary
    WORKER_CALLED           — a worker tool call was dispatched
    WORKER_COMPLETED        — a worker call returned (with truncated result)
    GATE_EVALUATED          — an evidence gate verdict was reached
    CONFIDENCE_UPDATED      — confidence changed (after calibration, critique, ...)
    CITATION_COMPLETE       — citation annotation finished
    FIX_PROPOSED            — a fix was generated
    FIX_VERIFYING           — verification loop started
    FIX_VERIFIED            — verification loop completed
    INVESTIGATION_COMPLETE  — final result ready
    KG_INGESTED             — knowledge graph updated
    ERROR                   — non-fatal error encountered

Usage (from agent.py):
    from supervisor.progress_stream import get_stream, StreamEvent, EventType

    stream = get_stream()
    stream.emit(incident_id, EventType.PHASE_STARTED, {"phase": "collect"})

    # In a WebSocket handler:
    async def handler(ws):
        investigation_id = ...
        with stream.subscribe(investigation_id) as q:
            while True:
                event = await asyncio.get_event_loop().run_in_executor(None, q.get)
                await ws.send(json.dumps(event.to_dict()))
                if event.event_type == EventType.INVESTIGATION_COMPLETE:
                    break

Configuration:
    PROGRESS_STREAM_ENABLED   — on/off (default: true)
    PROGRESS_STREAM_MAX_QUEUE — max buffered events per subscriber (default: 200)
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Generator

logger = logging.getLogger("sentinalai.progress_stream")

STREAM_ENABLED = os.environ.get("PROGRESS_STREAM_ENABLED", "true").lower() in ("1", "true", "yes")
MAX_QUEUE_SIZE = int(os.environ.get("PROGRESS_STREAM_MAX_QUEUE", "200"))


class EventType(str, Enum):
    INVESTIGATION_STARTED  = "investigation_started"
    PHASE_STARTED          = "phase_started"
    PHASE_COMPLETED        = "phase_completed"
    WORKER_CALLED          = "worker_called"
    WORKER_COMPLETED       = "worker_completed"
    GATE_EVALUATED         = "gate_evaluated"
    CONFIDENCE_UPDATED     = "confidence_updated"
    CITATION_COMPLETE      = "citation_complete"
    FIX_PROPOSED           = "fix_proposed"
    FIX_VERIFYING          = "fix_verifying"
    FIX_VERIFIED           = "fix_verified"
    INVESTIGATION_COMPLETE = "investigation_complete"
    KG_INGESTED            = "kg_ingested"
    GIT_BISECT_COMPLETE    = "git_bisect_complete"
    TRACE_CORRELATED       = "trace_correlated"
    ERROR                  = "error"


@dataclass
class StreamEvent:
    event_type: EventType
    investigation_id: str
    phase: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["event_type"] = self.event_type.value
        return d

    @property
    def is_terminal(self) -> bool:
        return self.event_type == EventType.INVESTIGATION_COMPLETE


# ---------------------------------------------------------------------------
# Subscriber — thread-safe event queue for one investigation consumer
# ---------------------------------------------------------------------------

class _Subscriber:
    """One subscriber listening to events for a specific investigation."""

    def __init__(self, investigation_id: str, max_size: int = MAX_QUEUE_SIZE):
        self.investigation_id = investigation_id
        self._q: queue.Queue[StreamEvent] = queue.Queue(maxsize=max_size)

    def put(self, event: StreamEvent) -> None:
        try:
            self._q.put_nowait(event)
        except queue.Full:
            # Drop oldest event to make room (never block the investigation)
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(event)
            except queue.Full:
                pass

    def get(self, timeout: float | None = None) -> StreamEvent:
        """Blocking get. Raises queue.Empty on timeout."""
        return self._q.get(timeout=timeout)

    def get_nowait(self) -> StreamEvent | None:
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None

    def drain(self) -> list[StreamEvent]:
        """Drain all queued events non-blocking."""
        events = []
        while True:
            ev = self.get_nowait()
            if ev is None:
                break
            events.append(ev)
        return events


# ---------------------------------------------------------------------------
# InvestigationStream — manages subscribers for all active investigations
# ---------------------------------------------------------------------------

class InvestigationStream:
    """Thread-safe publish/subscribe event stream for all investigations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # investigation_id → list[_Subscriber]
        self._subscribers: dict[str, list[_Subscriber]] = {}
        # Callback subscribers: called synchronously on emit
        self._callbacks: list[Callable[[StreamEvent], None]] = []

    # ------------------------------------------------------------------ #
    # Publishing
    # ------------------------------------------------------------------ #

    def emit(
        self,
        investigation_id: str,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
        phase: str = "",
    ) -> StreamEvent:
        """Emit an event. Non-blocking — never raises."""
        if not STREAM_ENABLED:
            return StreamEvent(event_type, investigation_id, phase, payload or {})
        event = StreamEvent(
            event_type=event_type,
            investigation_id=investigation_id,
            phase=phase,
            payload=payload or {},
        )
        try:
            with self._lock:
                subs = list(self._subscribers.get(investigation_id, []))
                callbacks = list(self._callbacks)
            for sub in subs:
                sub.put(event)
            for cb in callbacks:
                try:
                    cb(event)
                except Exception as exc:
                    logger.debug("Stream callback error: %s", exc)
        except Exception as exc:
            logger.debug("Stream emit error (non-critical): %s", exc)
        return event

    # Convenience wrappers
    def emit_phase(self, investigation_id: str, phase: str, **payload: Any) -> StreamEvent:
        return self.emit(investigation_id, EventType.PHASE_STARTED, payload, phase=phase)

    def emit_phase_done(self, investigation_id: str, phase: str, **payload: Any) -> StreamEvent:
        return self.emit(investigation_id, EventType.PHASE_COMPLETED, payload, phase=phase)

    def emit_gate(
        self,
        investigation_id: str,
        gate_name: str,
        verdict: str,
        reason: str,
        **extra: Any,
    ) -> StreamEvent:
        return self.emit(
            investigation_id,
            EventType.GATE_EVALUATED,
            {"gate": gate_name, "verdict": verdict, "reason": reason, **extra},
            phase="gate",
        )

    def emit_worker(
        self,
        investigation_id: str,
        worker: str,
        action: str,
        success: bool,
        elapsed_ms: float = 0.0,
        result_preview: str = "",
    ) -> StreamEvent:
        return self.emit(
            investigation_id,
            EventType.WORKER_COMPLETED,
            {
                "worker": worker, "action": action,
                "success": success, "elapsed_ms": round(elapsed_ms, 1),
                "preview": result_preview[:200] if result_preview else "",
            },
            phase="collect",
        )

    def emit_confidence(
        self,
        investigation_id: str,
        confidence: int,
        source: str = "",
        previous: int | None = None,
    ) -> StreamEvent:
        payload: dict[str, Any] = {"confidence": confidence, "source": source}
        if previous is not None:
            payload["previous"] = previous
        return self.emit(investigation_id, EventType.CONFIDENCE_UPDATED, payload, phase="analyze")

    def emit_complete(
        self,
        investigation_id: str,
        root_cause: str,
        confidence: int,
        citation_coverage: float,
        fix_proposed: bool,
        elapsed_ms: float,
    ) -> StreamEvent:
        return self.emit(
            investigation_id,
            EventType.INVESTIGATION_COMPLETE,
            {
                "root_cause": root_cause[:200],
                "confidence": confidence,
                "citation_coverage": citation_coverage,
                "fix_proposed": fix_proposed,
                "elapsed_ms": round(elapsed_ms, 1),
            },
            phase="complete",
        )

    # ------------------------------------------------------------------ #
    # Subscribing
    # ------------------------------------------------------------------ #

    @contextmanager
    def subscribe(
        self, investigation_id: str, max_size: int = MAX_QUEUE_SIZE
    ) -> Generator[_Subscriber, None, None]:
        """Context manager: subscribe to events for one investigation.

        Usage:
            with stream.subscribe("INC001") as q:
                while True:
                    event = q.get(timeout=30)
                    process(event)
                    if event.is_terminal:
                        break
        """
        sub = _Subscriber(investigation_id, max_size=max_size)
        with self._lock:
            self._subscribers.setdefault(investigation_id, []).append(sub)
        try:
            yield sub
        finally:
            with self._lock:
                subs = self._subscribers.get(investigation_id, [])
                try:
                    subs.remove(sub)
                except ValueError:
                    pass
                if not subs:
                    self._subscribers.pop(investigation_id, None)

    def add_callback(self, callback: Callable[[StreamEvent], None]) -> None:
        """Register a global callback invoked synchronously on every event."""
        with self._lock:
            self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[[StreamEvent], None]) -> None:
        with self._lock:
            try:
                self._callbacks.remove(callback)
            except ValueError:
                pass

    def subscriber_count(self, investigation_id: str) -> int:
        with self._lock:
            return len(self._subscribers.get(investigation_id, []))

    def active_investigations(self) -> list[str]:
        with self._lock:
            return list(self._subscribers.keys())


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_stream: InvestigationStream | None = None
_stream_lock = threading.Lock()


def get_stream() -> InvestigationStream:
    """Return the module-level singleton stream."""
    global _stream
    if _stream is None:
        with _stream_lock:
            if _stream is None:
                _stream = InvestigationStream()
    return _stream
