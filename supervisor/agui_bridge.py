"""AG UI Bridge — connects the existing supervisor pipeline to the AG UI event bus.

This module is imported by supervisor/agent.py to emit structured events
as the investigation progresses.

Design:
- Thin adapter: does NOT change agent logic
- Thread-safe: all emissions use put_threadsafe() (agent runs in threads)
- Non-blocking: failure to emit never crashes the agent
- Zero dependencies on UI layer when AGUI_ENABLED=false

Usage in agent.py:
    from supervisor.agui_bridge import bridge
    bridge.emit_investigation_started(...)
    bridge.emit_tool_called(...)
    bridge.emit_tool_responded(...)
    # etc.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

AGUI_ENABLED = os.getenv("AGUI_ENABLED", "true").lower() == "true"


class AGUIBridge:
    """
    Thread-safe event emission bridge.

    All methods are no-ops when AGUI_ENABLED=false.
    All exceptions are caught to prevent contaminating agent execution.
    """

    def __init__(self) -> None:
        self._bus = None
        self._sequence_counters: dict[str, int] = {}
        self._enabled = AGUI_ENABLED

    def _get_bus(self):
        if not self._enabled:
            return None
        if self._bus is None:
            try:
                from agui.event_bus import get_bus
                self._bus = get_bus()
            except ImportError:
                logger.debug("AGUIBridge: agui package not available, disabling")
                self._enabled = False
        return self._bus

    def _next_seq(self, investigation_id: str) -> int:
        n = self._sequence_counters.get(investigation_id, 0)
        self._sequence_counters[investigation_id] = n + 1
        return n

    def _emit(self, event) -> None:
        bus = self._get_bus()
        if bus is None:
            return
        try:
            bus.put_threadsafe(event)
        except Exception as e:
            logger.debug("AGUIBridge: emit failed (non-critical): %s", e)

    def emit_investigation_started(
        self,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        summary: str = "",
        severity: str = "",
        source: str = "",
    ) -> None:
        from agui.schemas.events import AGUIEvent, EventType
        self._emit(AGUIEvent(
            event_type=EventType.INVESTIGATION_STARTED,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            span_id=str(uuid.uuid4())[:16],
            sequence_num=self._next_seq(investigation_id),
            payload={
                "summary": summary,
                "severity": severity,
                "source": source,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        ))

    def emit_incident_classified(
        self,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        incident_type: str,
        confidence: float = 1.0,
    ) -> None:
        from agui.schemas.events import AGUIEvent, EventType
        self._emit(AGUIEvent(
            event_type=EventType.INCIDENT_CLASSIFIED,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            sequence_num=self._next_seq(investigation_id),
            payload={"incident_type": incident_type, "confidence": confidence},
        ))

    def emit_playbook_selected(
        self,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        playbook: list[str],
        incident_type: str,
    ) -> None:
        from agui.schemas.events import AGUIEvent, EventType
        self._emit(AGUIEvent(
            event_type=EventType.PLAYBOOK_SELECTED,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            sequence_num=self._next_seq(investigation_id),
            payload={"playbook": playbook, "incident_type": incident_type},
        ))

    def emit_tool_called(
        self,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        worker: str,
        action: str,
        params: dict[str, Any],
        receipt_id: str,
        span_id: str = "",
        parent_span_id: Optional[str] = None,
    ) -> None:
        from agui.schemas.events import AGUIEvent, EventType
        self._emit(AGUIEvent(
            event_type=EventType.TOOL_CALLED,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            span_id=span_id or str(uuid.uuid4())[:16],
            parent_span_id=parent_span_id,
            sequence_num=self._next_seq(investigation_id),
            payload={
                "worker": worker,
                "action": action,
                "params": self._redact(params),
                "receipt_id": receipt_id,
            },
        ))

    def emit_tool_responded(
        self,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        worker: str,
        action: str,
        receipt_id: str,
        elapsed_ms: float,
        result_count: int,
        status: str,
        span_id: str = "",
        error: Optional[str] = None,
        output_summary: Optional[str] = None,
    ) -> None:
        from agui.schemas.events import AGUIEvent, EventType
        etype = (
            EventType.TOOL_RESPONDED if status == "success"
            else EventType.TOOL_TIMEOUT if status == "timeout"
            else EventType.TOOL_FAILED
        )
        self._emit(AGUIEvent(
            event_type=etype,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            span_id=span_id or str(uuid.uuid4())[:16],
            sequence_num=self._next_seq(investigation_id),
            payload={
                "worker": worker,
                "action": action,
                "receipt_id": receipt_id,
                "elapsed_ms": elapsed_ms,
                "result_count": result_count,
                "status": status,
                "error": error,
                "output_summary": output_summary,
            },
        ))

    def emit_hypothesis_scored(
        self,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        hypotheses: list[dict[str, Any]],
        winner: str,
        confidence: float,
    ) -> None:
        from agui.schemas.events import AGUIEvent, EventType
        self._emit(AGUIEvent(
            event_type=EventType.HYPOTHESIS_SCORED,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            sequence_num=self._next_seq(investigation_id),
            payload={
                "hypotheses": [
                    {"name": h.get("name", ""), "score": h.get("score", 0.0)}
                    for h in hypotheses
                ],
                "winner": winner,
                "confidence": confidence,
            },
        ))

    def emit_hypothesis_selected(
        self,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        winner: str,
        confidence: float,
        root_cause: str,
    ) -> None:
        from agui.schemas.events import AGUIEvent, EventType
        self._emit(AGUIEvent(
            event_type=EventType.HYPOTHESIS_SELECTED,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            sequence_num=self._next_seq(investigation_id),
            payload={"winner": winner, "confidence": confidence, "root_cause": root_cause},
        ))

    def emit_llm_invoked(
        self,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        model: str,
        purpose: str = "hypothesis_refinement",
        input_tokens: int = 0,
    ) -> None:
        from agui.schemas.events import AGUIEvent, EventType
        self._emit(AGUIEvent(
            event_type=EventType.LLM_INVOKED,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            sequence_num=self._next_seq(investigation_id),
            payload={"model": model, "purpose": purpose, "input_tokens": input_tokens},
        ))

    def emit_llm_responded(
        self,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        model: str,
        total_tokens: int = 0,
        elapsed_ms: float = 0.0,
    ) -> None:
        from agui.schemas.events import AGUIEvent, EventType
        self._emit(AGUIEvent(
            event_type=EventType.LLM_RESPONDED,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            sequence_num=self._next_seq(investigation_id),
            payload={"model": model, "total_tokens": total_tokens, "elapsed_ms": elapsed_ms},
        ))

    def emit_rca_generated(
        self,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        root_cause: str,
        confidence: float,
        remediation: list[str],
        judge_scores: dict[str, float],
    ) -> None:
        from agui.schemas.events import AGUIEvent, EventType
        self._emit(AGUIEvent(
            event_type=EventType.RCA_GENERATED,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            sequence_num=self._next_seq(investigation_id),
            payload={
                "root_cause": root_cause,
                "confidence": confidence,
                "remediation": remediation,
                "judge_scores": judge_scores,
            },
        ))

    def emit_investigation_completed(
        self,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        duration_ms: float,
        tool_calls_total: int,
        confidence: float,
        service: str = "",
    ) -> None:
        from agui.schemas.events import AGUIEvent, EventType
        self._emit(AGUIEvent(
            event_type=EventType.INVESTIGATION_COMPLETED,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            sequence_num=self._next_seq(investigation_id),
            payload={
                "duration_ms": duration_ms,
                "tool_calls_total": tool_calls_total,
                "confidence": confidence,
            },
        ))
        # Close the prediction feedback loop: mark pending predictions as true positives
        if service and incident_id:
            try:
                from intelligence.background_runner import get_runner
                get_runner().record_outcome(service, incident_id)
            except Exception:
                pass

    def emit_investigation_failed(
        self,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        error: str,
        duration_ms: float = 0.0,
    ) -> None:
        from agui.schemas.events import AGUIEvent, EventType
        self._emit(AGUIEvent(
            event_type=EventType.INVESTIGATION_FAILED,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            sequence_num=self._next_seq(investigation_id),
            payload={"error": error, "duration_ms": duration_ms},
        ))

    def emit_budget_warning(
        self,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        calls_used: int,
        calls_max: int,
    ) -> None:
        from agui.schemas.events import AGUIEvent, EventType
        self._emit(AGUIEvent(
            event_type=EventType.BUDGET_WARNING,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            sequence_num=self._next_seq(investigation_id),
            payload={"calls_used": calls_used, "calls_max": calls_max},
        ))

    def emit_circuit_breaker_tripped(
        self,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        worker: str,
        failure_count: int,
    ) -> None:
        from agui.schemas.events import AGUIEvent, EventType
        self._emit(AGUIEvent(
            event_type=EventType.CIRCUIT_BREAKER_TRIPPED,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            sequence_num=self._next_seq(investigation_id),
            payload={"worker": worker, "failure_count": failure_count},
        ))

    def emit_memory_queried(
        self,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        query: str,
        store_type: str = "ltm",
    ) -> None:
        from agui.schemas.events import AGUIEvent, EventType
        self._emit(AGUIEvent(
            event_type=EventType.MEMORY_QUERIED,
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            sequence_num=self._next_seq(investigation_id),
            payload={"query": query[:200], "store_type": store_type},
        ))

    def reset_investigation(self, investigation_id: str) -> None:
        """Clean up sequence counter after investigation ends."""
        self._sequence_counters.pop(investigation_id, None)

    @staticmethod
    def _redact(params: dict[str, Any]) -> dict[str, Any]:
        """Redact sensitive fields from params before emission."""
        SENSITIVE = {"password", "token", "secret", "api_key", "authorization", "key"}
        return {
            k: ("***REDACTED***" if any(s in k.lower() for s in SENSITIVE) else v)
            for k, v in params.items()
        }


# Global singleton
bridge = AGUIBridge()
