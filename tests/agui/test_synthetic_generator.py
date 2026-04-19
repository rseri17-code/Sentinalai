"""Tests for AG UI synthetic incident generator."""
import pytest
from agui.synthetic_generator import SyntheticIncidentGenerator, INCIDENT_SCENARIOS
from agui.schemas.events import AGUIEvent, EventType
from agui.replay_engine import ReplayEngine


class TestSyntheticGenerator:
    @pytest.mark.asyncio
    async def test_generate_error_spike(self):
        gen = SyntheticIncidentGenerator(seed=42)
        inv_id, events = await gen.generate_investigation("error_spike")
        assert inv_id
        assert len(events) > 0

    @pytest.mark.asyncio
    async def test_all_events_are_valid_schema(self):
        gen = SyntheticIncidentGenerator(seed=42)
        _, events = await gen.generate_investigation("oomkill")
        for event in events:
            assert isinstance(event, AGUIEvent)
            assert event.investigation_id
            assert event.incident_id
            assert event.trace_id
            assert event.sequence_num >= 0

    @pytest.mark.asyncio
    async def test_sequences_are_ordered(self):
        gen = SyntheticIncidentGenerator(seed=42)
        _, events = await gen.generate_investigation("latency")
        seqs = [e.sequence_num for e in events]
        assert seqs == sorted(seqs), "Events must be in sequence order"

    @pytest.mark.asyncio
    async def test_required_event_types_present(self):
        """Generated investigation should contain all key lifecycle events."""
        gen = SyntheticIncidentGenerator(seed=42)
        _, events = await gen.generate_investigation("timeout")
        event_types = {e.event_type for e in events}

        assert EventType.INVESTIGATION_STARTED in event_types
        assert EventType.TOOL_CALLED in event_types
        assert EventType.TOOL_RESPONDED in event_types
        assert EventType.HYPOTHESIS_SCORED in event_types
        assert EventType.INVESTIGATION_COMPLETED in event_types

    @pytest.mark.asyncio
    async def test_includes_llm_events(self):
        gen = SyntheticIncidentGenerator(seed=42)
        _, events = await gen.generate_investigation("error_spike")
        event_types = {e.event_type for e in events}
        assert EventType.LLM_INVOKED in event_types
        assert EventType.LLM_RESPONDED in event_types

    @pytest.mark.asyncio
    async def test_includes_memory_events(self):
        gen = SyntheticIncidentGenerator(seed=42)
        _, events = await gen.generate_investigation("error_spike")
        event_types = {e.event_type for e in events}
        assert EventType.MEMORY_QUERIED in event_types

    @pytest.mark.asyncio
    async def test_circuit_breaker_variant(self):
        gen = SyntheticIncidentGenerator(seed=42)
        _, events = await gen.generate_investigation(
            "error_spike", include_circuit_breaker=True
        )
        event_types = {e.event_type for e in events}
        assert EventType.CIRCUIT_BREAKER_TRIPPED in event_types

    @pytest.mark.asyncio
    async def test_control_gate_variant(self):
        gen = SyntheticIncidentGenerator(seed=42)
        _, events = await gen.generate_investigation(
            "error_spike", include_control_gate=True
        )
        event_types = {e.event_type for e in events}
        assert EventType.CONTROL_REQUESTED in event_types
        assert EventType.CONTROL_APPROVED in event_types

    @pytest.mark.asyncio
    async def test_budget_warning_variant(self):
        gen = SyntheticIncidentGenerator(seed=42)
        _, events = await gen.generate_investigation(
            "error_spike", include_budget_warning=True
        )
        event_types = {e.event_type for e in events}
        assert EventType.BUDGET_WARNING in event_types

    @pytest.mark.asyncio
    async def test_all_scenarios_generate_valid_events(self):
        gen = SyntheticIncidentGenerator(seed=42)
        for scenario_type in INCIDENT_SCENARIOS:
            inv_id, events = await gen.generate_investigation(scenario_type)
            assert len(events) > 5, f"Scenario {scenario_type} should produce >5 events"
            assert events[0].event_type == EventType.INVESTIGATION_STARTED
            assert events[-1].event_type == EventType.INVESTIGATION_COMPLETED

    @pytest.mark.asyncio
    async def test_generated_events_pass_replay_validation(self):
        """Synthetic events should be valid for replay."""
        gen = SyntheticIncidentGenerator(seed=42)
        inv_id, events = await gen.generate_investigation("error_spike")

        engine = ReplayEngine()
        snapshot = engine.build_snapshot(
            investigation_id=inv_id,
            incident_id=events[0].incident_id,
            trace_id=events[0].trace_id,
            events=events,
        )
        result = engine.validate(snapshot)
        assert result.is_valid, f"Validation failed: {result.errors}"
        assert result.gaps == []

    @pytest.mark.asyncio
    async def test_deterministic_with_seed(self):
        """Same seed → same investigation ID pattern."""
        gen1 = SyntheticIncidentGenerator(seed=123)
        gen2 = SyntheticIncidentGenerator(seed=123)
        # Same seed should produce same random choices (not same UUIDs, but same structure)
        _, events1 = await gen1.generate_investigation("oomkill", investigation_id="fixed-id")
        _, events2 = await gen2.generate_investigation("oomkill", investigation_id="fixed-id")
        # Same structure
        assert len(events1) == len(events2)
        assert [e.event_type for e in events1] == [e.event_type for e in events2]
