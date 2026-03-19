"""Tests for AG UI event bus — in-process pub/sub."""
import asyncio
import pytest
from agui.schemas.events import AGUIEvent, EventType
from agui.event_bus import InProcessEventBus


def make_event(seq: int, inv_id="inv-1") -> AGUIEvent:
    return AGUIEvent(
        event_type=EventType.TOOL_CALLED,
        investigation_id=inv_id,
        incident_id="INC-1",
        trace_id="trace-abc",
        sequence_num=seq,
        payload={"seq": seq},
    )


class TestInProcessEventBus:
    @pytest.mark.asyncio
    async def test_publish_and_subscribe(self):
        bus = InProcessEventBus()
        loop = asyncio.get_event_loop()
        bus.set_loop(loop)
        await bus.start()

        received = []

        async def handler(event: AGUIEvent):
            received.append(event)

        bus.subscribe("inv-1", handler)
        await bus.publish(make_event(0))
        await asyncio.sleep(0.05)

        assert len(received) == 1
        assert received[0].sequence_num == 0
        await bus.stop()

    @pytest.mark.asyncio
    async def test_deduplication(self):
        """Same idempotency key should not be delivered twice."""
        bus = InProcessEventBus()
        loop = asyncio.get_event_loop()
        bus.set_loop(loop)
        await bus.start()

        received = []

        async def handler(event: AGUIEvent):
            received.append(event)

        bus.subscribe("inv-1", handler)

        event = make_event(0)
        await bus.publish(event)
        await bus.publish(event)  # Duplicate
        await asyncio.sleep(0.05)

        assert len(received) == 1
        await bus.stop()

    @pytest.mark.asyncio
    async def test_multiple_investigations_isolated(self):
        """Subscribers for inv-1 should not receive inv-2 events."""
        bus = InProcessEventBus()
        loop = asyncio.get_event_loop()
        bus.set_loop(loop)
        await bus.start()

        inv1_received = []
        inv2_received = []

        async def handler1(event): inv1_received.append(event)
        async def handler2(event): inv2_received.append(event)

        bus.subscribe("inv-1", handler1)
        bus.subscribe("inv-2", handler2)

        await bus.publish(make_event(0, "inv-1"))
        await bus.publish(make_event(0, "inv-2"))
        await asyncio.sleep(0.05)

        assert len(inv1_received) == 1
        assert len(inv2_received) == 1
        assert inv1_received[0].investigation_id == "inv-1"
        assert inv2_received[0].investigation_id == "inv-2"
        await bus.stop()

    @pytest.mark.asyncio
    async def test_history_available_for_late_subscribers(self):
        """Events published before subscribe should be in history."""
        bus = InProcessEventBus()
        loop = asyncio.get_event_loop()
        bus.set_loop(loop)
        await bus.start()

        for i in range(5):
            await bus.publish(make_event(i))
        await asyncio.sleep(0.05)

        history = await bus.get_history("inv-1", since_seq=0)
        assert len(history) == 5
        await bus.stop()

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_delivery(self):
        bus = InProcessEventBus()
        loop = asyncio.get_event_loop()
        bus.set_loop(loop)
        await bus.start()

        received = []

        async def handler(event): received.append(event)
        bus.subscribe("inv-1", handler)
        await bus.publish(make_event(0))
        await asyncio.sleep(0.05)
        bus.unsubscribe("inv-1", handler)
        await bus.publish(make_event(1))
        await asyncio.sleep(0.05)

        assert len(received) == 1
        await bus.stop()

    @pytest.mark.asyncio
    async def test_wildcard_subscriber(self):
        """Wildcard '*' subscriber should receive all events."""
        bus = InProcessEventBus()
        loop = asyncio.get_event_loop()
        bus.set_loop(loop)
        await bus.start()

        all_events = []

        async def handler(event): all_events.append(event)
        bus.subscribe("*", handler)

        await bus.publish(make_event(0, "inv-1"))
        await bus.publish(make_event(0, "inv-2"))
        await asyncio.sleep(0.05)

        assert len(all_events) == 2
        await bus.stop()
