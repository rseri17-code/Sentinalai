"""Tests for supervisor.progress_stream."""
from __future__ import annotations

import os
import queue
import time
import threading
import pytest

os.environ.setdefault("PROGRESS_STREAM_ENABLED", "true")

from supervisor.progress_stream import (
    InvestigationStream,
    EventType,
    StreamEvent,
    get_stream,
    _Subscriber,
)


# ---------------------------------------------------------------------------
# Unit: _Subscriber
# ---------------------------------------------------------------------------

class TestSubscriber:

    def test_put_and_get(self):
        sub = _Subscriber("inv-001", max_size=10)
        ev = StreamEvent(EventType.INVESTIGATION_STARTED, "inv-001", "start")
        sub.put(ev)
        got = sub.get(timeout=0.1)
        assert got is ev

    def test_get_nowait_returns_none_when_empty(self):
        sub = _Subscriber("inv-001", max_size=5)
        assert sub.get_nowait() is None

    def test_drain_returns_all_queued(self):
        sub = _Subscriber("inv-001", max_size=10)
        for i in range(4):
            sub.put(StreamEvent(EventType.PHASE_STARTED, "inv-001", f"p{i}"))
        drained = sub.drain()
        assert len(drained) == 4
        assert sub.get_nowait() is None

    def test_drop_oldest_on_full(self):
        sub = _Subscriber("inv-001", max_size=2)
        for i in range(5):
            sub.put(StreamEvent(EventType.PHASE_STARTED, "inv-001", f"p{i}"))
        # Queue should not block and should still have 2 events
        drained = sub.drain()
        assert len(drained) == 2  # oldest were dropped to make room

    def test_is_terminal(self):
        ev = StreamEvent(EventType.INVESTIGATION_COMPLETE, "inv-001", "complete")
        assert ev.is_terminal is True
        ev2 = StreamEvent(EventType.PHASE_STARTED, "inv-001", "collect")
        assert ev2.is_terminal is False


# ---------------------------------------------------------------------------
# Unit: InvestigationStream emit + subscribe
# ---------------------------------------------------------------------------

class TestInvestigationStream:

    def setup_method(self):
        self.stream = InvestigationStream()

    def test_emit_returns_event(self):
        ev = self.stream.emit("inv-001", EventType.INVESTIGATION_STARTED, {}, phase="start")
        assert isinstance(ev, StreamEvent)
        assert ev.event_type == EventType.INVESTIGATION_STARTED
        assert ev.investigation_id == "inv-001"

    def test_subscribe_receives_events(self):
        received = []
        with self.stream.subscribe("inv-002") as sub:
            self.stream.emit("inv-002", EventType.PHASE_STARTED, {"phase": "collect"}, phase="collect")
            ev = sub.get(timeout=0.5)
            received.append(ev)
        assert len(received) == 1
        assert received[0].event_type == EventType.PHASE_STARTED

    def test_subscribe_isolated_per_investigation(self):
        with self.stream.subscribe("inv-A") as sub_a, self.stream.subscribe("inv-B") as sub_b:
            self.stream.emit("inv-A", EventType.PHASE_STARTED, {}, phase="x")
            self.stream.emit("inv-B", EventType.PHASE_COMPLETED, {}, phase="y")
            ev_a = sub_a.get(timeout=0.3)
            ev_b = sub_b.get(timeout=0.3)
        assert ev_a.event_type == EventType.PHASE_STARTED
        assert ev_b.event_type == EventType.PHASE_COMPLETED

    def test_subscriber_cleaned_up_after_context_exit(self):
        with self.stream.subscribe("inv-003"):
            assert self.stream.subscriber_count("inv-003") == 1
        assert self.stream.subscriber_count("inv-003") == 0

    def test_global_callback_called(self):
        seen = []
        self.stream.add_callback(seen.append)
        self.stream.emit("inv-X", EventType.ERROR, {"msg": "oops"}, phase="err")
        assert len(seen) == 1
        assert seen[0].event_type == EventType.ERROR
        self.stream.remove_callback(seen.append)

    def test_emit_phase_convenience(self):
        with self.stream.subscribe("inv-ph") as sub:
            self.stream.emit_phase("inv-ph", "collect", count=3)
            ev = sub.get(timeout=0.3)
        assert ev.event_type == EventType.PHASE_STARTED
        assert ev.phase == "collect"
        assert ev.payload["count"] == 3

    def test_emit_phase_done_convenience(self):
        with self.stream.subscribe("inv-phd") as sub:
            self.stream.emit_phase_done("inv-phd", "analyze", result="ok")
            ev = sub.get(timeout=0.3)
        assert ev.event_type == EventType.PHASE_COMPLETED

    def test_emit_confidence(self):
        with self.stream.subscribe("inv-conf") as sub:
            self.stream.emit_confidence("inv-conf", 85, source="calibrator", previous=80)
            ev = sub.get(timeout=0.3)
        assert ev.event_type == EventType.CONFIDENCE_UPDATED
        assert ev.payload["confidence"] == 85
        assert ev.payload["previous"] == 80

    def test_emit_complete(self):
        with self.stream.subscribe("inv-done") as sub:
            self.stream.emit_complete(
                "inv-done", "connection pool exhausted", 87, 0.92, True, 12345.0
            )
            ev = sub.get(timeout=0.3)
        assert ev.event_type == EventType.INVESTIGATION_COMPLETE
        assert ev.is_terminal is True
        assert ev.payload["confidence"] == 87

    def test_emit_gate(self):
        with self.stream.subscribe("inv-gate") as sub:
            self.stream.emit_gate("inv-gate", "G1", "pass", "sufficient evidence")
            ev = sub.get(timeout=0.3)
        assert ev.event_type == EventType.GATE_EVALUATED
        assert ev.payload["gate"] == "G1"
        assert ev.payload["verdict"] == "pass"

    def test_active_investigations_tracked(self):
        with self.stream.subscribe("inv-active"):
            assert "inv-active" in self.stream.active_investigations()
        assert "inv-active" not in self.stream.active_investigations()

    def test_to_dict_serialisable(self):
        ev = self.stream.emit("inv-ser", EventType.PHASE_STARTED, {"x": 1}, phase="p")
        d = ev.to_dict()
        assert d["event_type"] == "phase_started"
        assert d["investigation_id"] == "inv-ser"
        assert d["payload"]["x"] == 1

    def test_thread_safe_concurrent_emit(self):
        """Multiple threads emitting to same investigation should not lose events."""
        received = []
        N = 20
        barrier = threading.Barrier(N)

        with self.stream.subscribe("inv-mt") as sub:
            def _emit_one(i):
                barrier.wait()
                self.stream.emit("inv-mt", EventType.WORKER_COMPLETED, {"i": i}, phase="w")

            threads = [threading.Thread(target=_emit_one, args=(i,)) for i in range(N)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            events = sub.drain()

        assert len(events) == N


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

class TestGetStream:

    def test_returns_same_instance(self):
        s1 = get_stream()
        s2 = get_stream()
        assert s1 is s2

    def test_is_investigation_stream(self):
        assert isinstance(get_stream(), InvestigationStream)
