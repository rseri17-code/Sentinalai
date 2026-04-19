"""Tests for AG UI data contract schemas."""
import json
import time
from agui.schemas.events import AGUIEvent, EventType
from agui.schemas.receipts import UIReceipt
from agui.schemas.graph import GraphNode, GraphEdge, ExecutionGraph, NodeType, NodeStatus
from agui.schemas.incidents import IncidentState


class TestAGUIEventSchema:
    def test_event_creation_with_defaults(self):
        event = AGUIEvent(
            event_type=EventType.INVESTIGATION_STARTED,
            investigation_id="inv-123",
            incident_id="INC-001",
        )
        assert event.event_id
        assert event.schema_version == "1.0"
        assert event.idempotency_key  # Auto-computed
        assert event.timestamp
        assert event.ttl > int(time.time())

    def test_idempotency_key_deterministic(self):
        """Same investigation + sequence should produce same key."""
        e1 = AGUIEvent(
            event_type=EventType.TOOL_CALLED,
            investigation_id="inv-123",
            incident_id="INC-001",
            sequence_num=5,
        )
        e2 = AGUIEvent(
            event_type=EventType.TOOL_RESPONDED,
            investigation_id="inv-123",
            incident_id="INC-001",
            sequence_num=5,
        )
        assert e1.idempotency_key == e2.idempotency_key

    def test_to_ws_message_is_valid_json(self):
        event = AGUIEvent(
            event_type=EventType.HYPOTHESIS_SCORED,
            investigation_id="inv-123",
            incident_id="INC-001",
            payload={"winner": "hypothesis_1", "confidence": 0.87},
        )
        msg = event.to_ws_message()
        parsed = json.loads(msg)
        assert parsed["event_type"] == "hypothesis.scored"
        assert parsed["payload"]["confidence"] == 0.87

    def test_to_dynamo_adds_pk_sk(self):
        event = AGUIEvent(
            event_type=EventType.TOOL_CALLED,
            investigation_id="inv-abc",
            incident_id="INC-002",
            sequence_num=3,
        )
        dynamo = event.to_dynamo()
        assert dynamo["pk"] == "INVESTIGATION#inv-abc"
        assert "sk" in dynamo
        assert dynamo["sk"].startswith("EVENT#")

    def test_make_tool_called_factory(self):
        event = AGUIEvent.make_tool_called(
            investigation_id="inv-1",
            incident_id="INC-1",
            trace_id="trace-123",
            span_id="span-456",
            sequence_num=2,
            worker="LogWorker",
            action="search_logs",
            params={"index": "main", "query": "error"},
            receipt_id="receipt-789",
        )
        assert event.event_type == EventType.TOOL_CALLED
        assert event.payload["worker"] == "LogWorker"
        assert event.payload["receipt_id"] == "receipt-789"

    def test_all_event_types_are_valid(self):
        """Ensure every EventType value can be used in an event."""
        for event_type in EventType:
            event = AGUIEvent(
                event_type=event_type,
                investigation_id="inv-test",
                incident_id="INC-test",
            )
            assert event.event_type == event_type

    def test_extra_fields_allowed(self):
        """Schema should handle extra fields gracefully."""
        event = AGUIEvent(
            event_type=EventType.HEARTBEAT,
            investigation_id="inv-1",
            incident_id="INC-1",
            future_field="future_value",  # Unknown field
        )
        assert event.event_type == EventType.HEARTBEAT


class TestUIReceiptSchema:
    def test_receipt_creation(self):
        receipt = UIReceipt(
            investigation_id="inv-1",
            incident_id="INC-1",
            sequence_num=3,
            trace_id="trace-abc",
            worker="LogWorker",
            tool="Splunk",
            action="search_logs",
            wall_clock_start="2025-01-01T10:00:00Z",
            wall_clock_end="2025-01-01T10:00:01Z",
            elapsed_ms=1000.0,
            status="success",
            result_count=42,
        )
        assert receipt.receipt_id
        assert receipt.payload_hash  # Auto-computed
        assert receipt.schema_version == "1.0"

    def test_hash_integrity(self):
        receipt = UIReceipt(
            investigation_id="inv-1",
            incident_id="INC-1",
            sequence_num=1,
            trace_id="trace-x",
            worker="MetricsWorker",
            tool="Sysdig",
            action="query_metrics",
            wall_clock_start="2025-01-01T10:00:00Z",
            wall_clock_end="2025-01-01T10:00:02Z",
            elapsed_ms=2000.0,
            status="success",
        )
        # Hash should be reproducible
        computed = receipt.compute_hash()
        assert computed == receipt.payload_hash

    def test_to_dynamo_structure(self):
        receipt = UIReceipt(
            investigation_id="inv-abc",
            incident_id="INC-1",
            sequence_num=5,
            trace_id="t",
            worker="ApmWorker",
            tool="Dynatrace",
            action="get_golden_signals",
            wall_clock_start="2025-01-01T10:00:00Z",
            wall_clock_end="2025-01-01T10:00:01Z",
            elapsed_ms=500.0,
            status="success",
        )
        dynamo = receipt.to_dynamo()
        assert dynamo["pk"] == "INVESTIGATION#inv-abc"
        assert dynamo["sk"].startswith("RECEIPT#")
        assert "gsi1pk" in dynamo

    def test_from_supervisor_receipt_bridge(self):
        """Test bridge from existing supervisor Receipt."""
        class MockReceipt:
            tool = "Splunk"
            action = "search_logs"
            params = {"query": "error"}
            status = "success"
            elapsed_ms = 300.0
            result_count = 5
            trace_id = "trace-abc"
            correlation_id = "corr-123"
            wall_clock_start = "2025-01-01T10:00:00Z"
            wall_clock_end = "2025-01-01T10:00:01Z"
            error = None

        ui_receipt = UIReceipt.from_supervisor_receipt(
            receipt=MockReceipt(),
            investigation_id="inv-1",
            incident_id="INC-1",
            sequence_num=2,
            worker="LogWorker",
        )
        assert ui_receipt.worker == "LogWorker"
        assert ui_receipt.trace_id == "trace-abc"
        assert ui_receipt.elapsed_ms == 300.0


class TestExecutionGraphSchema:
    def test_graph_add_node(self):
        graph = ExecutionGraph(
            investigation_id="inv-1",
            incident_id="INC-1",
            trace_id="trace-1",
        )
        node = GraphNode(
            node_id="n1",
            node_type=NodeType.INVESTIGATION,
            label="Investigation",
            investigation_id="inv-1",
            trace_id="trace-1",
            status=NodeStatus.RUNNING,
            sequence_num=0,
        )
        graph.add_node(node)
        assert len(graph.nodes) == 1

    def test_graph_prevents_duplicate_nodes(self):
        graph = ExecutionGraph(
            investigation_id="inv-1",
            incident_id="INC-1",
            trace_id="trace-1",
        )
        node = GraphNode(
            node_id="n1",
            node_type=NodeType.TOOL_CALL,
            label="Tool",
            investigation_id="inv-1",
            trace_id="trace-1",
            status=NodeStatus.PENDING,
            sequence_num=1,
        )
        graph.add_node(node)
        graph.add_node(node)  # Duplicate
        assert len(graph.nodes) == 1

    def test_graph_add_edge_updates_parent_child(self):
        graph = ExecutionGraph(
            investigation_id="inv-1",
            incident_id="INC-1",
            trace_id="trace-1",
        )
        n1 = GraphNode(node_id="n1", node_type=NodeType.INVESTIGATION, label="Root",
                       investigation_id="inv-1", trace_id="t", status=NodeStatus.RUNNING, sequence_num=0)
        n2 = GraphNode(node_id="n2", node_type=NodeType.TOOL_CALL, label="Tool",
                       investigation_id="inv-1", trace_id="t", status=NodeStatus.PENDING, sequence_num=1)
        graph.add_node(n1)
        graph.add_node(n2)
        edge = GraphEdge(edge_id="e1", source_id="n1", target_id="n2")
        graph.add_edge(edge)
        assert "n2" in graph.get_node("n1").child_ids
        assert "n1" in graph.get_node("n2").parent_ids

    def test_root_and_leaf_nodes(self):
        graph = ExecutionGraph(
            investigation_id="inv-1",
            incident_id="INC-1",
            trace_id="trace-1",
        )
        n1 = GraphNode(node_id="n1", node_type=NodeType.INVESTIGATION, label="Root",
                       investigation_id="inv-1", trace_id="t", status=NodeStatus.RUNNING, sequence_num=0)
        n2 = GraphNode(node_id="n2", node_type=NodeType.RCA, label="RCA",
                       investigation_id="inv-1", trace_id="t", status=NodeStatus.SUCCESS, sequence_num=5)
        graph.add_node(n1)
        graph.add_node(n2)
        graph.add_edge(GraphEdge(edge_id="e1", source_id="n1", target_id="n2"))
        assert n1 in graph.root_nodes
        assert n2 in graph.leaf_nodes


class TestIncidentStateSchema:
    def test_incident_state_creation(self):
        state = IncidentState(
            incident_id="INC-001",
            trace_id="trace-abc",
            summary="Error spike",
            affected_service="payments-service",
            severity="critical",
        )
        assert state.investigation_id
        assert state.confidence == 0.0
        assert state.budget_used == 0
        assert state.budget_max == 20

    def test_budget_pct_calculation(self):
        state = IncidentState(incident_id="INC-1", budget_used=10, budget_max=20)
        assert state.budget_pct == 50.0

    def test_is_stale(self):
        state = IncidentState(incident_id="INC-1", stale_sources=["LogWorker"])
        assert state.is_stale

    def test_to_dynamo_structure(self):
        state = IncidentState(
            investigation_id="inv-abc",
            incident_id="INC-1",
            trace_id="t",
            started_at="2025-01-01T10:00:00Z",
            status="running",
        )
        dynamo = state.to_dynamo()
        assert dynamo["pk"] == "INVESTIGATION#inv-abc"
        assert dynamo["sk"] == "STATE"
        assert "gsi1pk" in dynamo
        assert "gsi2pk" in dynamo
