"""Tests for supervisor.progress_stream."""
from __future__ import annotations

import os
import threading

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


# ---------------------------------------------------------------------------
# Coverage gap-fill tests
# ---------------------------------------------------------------------------

class TestSubscriberQueueRacePaths:
    """Cover lines 121-122 and 125-126 (inner queue.Empty / queue.Full races)."""

    def test_inner_queue_empty_after_full(self):
        """Simulate race: put fails Full, then get raises Empty (line 121-122)."""
        import queue as q_module
        from unittest.mock import patch
        sub = _Subscriber("inv-race-empty", max_size=2)
        ev = StreamEvent(EventType.PHASE_STARTED, "inv-race-empty", "p")

        def mock_put_always_full(item):
            raise q_module.Full

        def mock_get_always_empty():
            raise q_module.Empty

        with patch.object(sub._q, "put_nowait", side_effect=mock_put_always_full):
            with patch.object(sub._q, "get_nowait", side_effect=mock_get_always_empty):
                # Should not raise even with both inner exceptions
                sub.put(ev)

    def test_inner_queue_full_on_second_put(self):
        """Simulate race: after draining one item, second put still fails Full (line 125-126)."""
        import queue as q_module
        from unittest.mock import patch
        sub = _Subscriber("inv-race-full", max_size=2)
        ev = StreamEvent(EventType.PHASE_STARTED, "inv-race-full", "p")

        gotten = [False]

        def mock_put_always_full(item):
            raise q_module.Full

        def mock_get_returns_item():
            gotten[0] = True
            return ev  # Successfully drains one item

        with patch.object(sub._q, "put_nowait", side_effect=mock_put_always_full):
            with patch.object(sub._q, "get_nowait", side_effect=mock_get_returns_item):
                # Should not raise; second put_nowait raises Full → silently dropped
                sub.put(ev)

        assert gotten[0]  # get_nowait was called to drain


class TestStreamDisabledPath:
    """Cover line 176: emit() returns early when STREAM_ENABLED=False."""

    def test_emit_returns_event_when_disabled(self, monkeypatch):
        import supervisor.progress_stream as mod
        monkeypatch.setattr(mod, "STREAM_ENABLED", False)
        stream = InvestigationStream()
        ev = stream.emit("inv-off", EventType.PHASE_STARTED, {"x": 1}, phase="p")
        assert ev.event_type == EventType.PHASE_STARTED
        assert ev.investigation_id == "inv-off"
        # No subscribers were notified (stream was disabled)
        assert stream.subscriber_count("inv-off") == 0


class TestCallbackExceptionHandling:
    """Cover lines 192-195: exception in callback and outer emit exception path."""

    def test_callback_exception_does_not_crash_stream(self):
        """Cover lines 192-193: inner callback exception is caught."""
        stream = InvestigationStream()

        def bad_callback(event):
            raise ValueError("callback failure")

        good_results = []
        stream.add_callback(bad_callback)
        stream.add_callback(good_results.append)

        # Should not raise even though bad_callback raises
        ev = stream.emit("inv-cb", EventType.PHASE_STARTED, {})
        assert ev is not None
        # Good callback still ran
        assert len(good_results) == 1

        stream.remove_callback(bad_callback)
        stream.remove_callback(good_results.append)

    def test_emit_outer_exception_caught(self):
        """Cover lines 194-195: outer exception in emit (e.g. sub.put raises)."""
        from unittest.mock import patch
        stream = InvestigationStream()

        with stream.subscribe("inv-outerex") as sub:
            # Make sub.put raise to trigger the outer except
            with patch.object(sub, "put", side_effect=RuntimeError("subscriber broke")):
                ev = stream.emit("inv-outerex", EventType.PHASE_STARTED, {})
        assert ev is not None


class TestEmitWorkerMethod:
    """Cover line 229: emit_worker convenience wrapper."""

    def test_emit_worker_publishes_event(self):
        stream = InvestigationStream()
        with stream.subscribe("inv-wk") as sub:
            stream.emit_worker(
                "inv-wk", "git_worker", "git_log_for_service",
                success=True, elapsed_ms=42.5, result_preview="3 commits",
            )
            ev = sub.get(timeout=0.3)
        assert ev.event_type == EventType.WORKER_COMPLETED
        assert ev.payload["worker"] == "git_worker"
        assert ev.payload["success"] is True
        assert ev.payload["elapsed_ms"] == 42.5


class TestSubscribeCleanupEdgeCases:
    """Cover lines 302-303 and 316-317."""

    def test_subscribe_cleanup_handles_already_removed(self):
        """Cover lines 302-303: ValueError when sub already removed from list."""
        stream = InvestigationStream()
        with stream.subscribe("inv-pre-rm") as sub:
            # Remove the subscriber manually before context manager exits
            with stream._lock:
                subs = stream._subscribers.get("inv-pre-rm", [])
                if sub in subs:
                    subs.remove(sub)
        # Context manager exits and tries to remove sub again → ValueError → caught silently

    def test_remove_nonexistent_callback_does_not_raise(self):
        """Cover lines 316-317: remove_callback for unregistered callback."""
        stream = InvestigationStream()

        def never_added(ev):
            pass

        # Should not raise even though callback was never added
        stream.remove_callback(never_added)
