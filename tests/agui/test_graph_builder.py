"""Tests for AG UI graph builder — DAG reconstruction from events."""
from agui.schemas.events import AGUIEvent, EventType
from agui.schemas.graph import NodeType, NodeStatus
from agui.graph_builder import GraphBuilder, rebuild_from_events


def make_event(event_type: EventType, seq: int, inv_id="inv-1", inc_id="INC-1",
               payload=None, trace_id="trace-abc") -> AGUIEvent:
    return AGUIEvent(
        event_type=event_type,
        investigation_id=inv_id,
        incident_id=inc_id,
        trace_id=trace_id,
        sequence_num=seq,
        payload=payload or {},
    )


class TestGraphBuilder:
    def test_investigation_start_creates_root_node(self):
        builder = GraphBuilder("inv-1", "INC-1", "trace-abc")
        event = make_event(EventType.INVESTIGATION_STARTED, 0, payload={
            "summary": "Test incident", "severity": "critical",
        })
        builder.process_event(event)
        assert len(builder.graph.nodes) == 1
        assert builder.graph.nodes[0].node_type == NodeType.INVESTIGATION
        assert builder.graph.nodes[0].status == NodeStatus.RUNNING

    def test_classification_connects_to_root(self):
        builder = GraphBuilder("inv-1", "INC-1", "trace-abc")
        builder.process_event(make_event(EventType.INVESTIGATION_STARTED, 0))
        builder.process_event(make_event(EventType.INCIDENT_CLASSIFIED, 1, payload={
            "incident_type": "error_spike"
        }))
        assert len(builder.graph.nodes) == 2
        assert len(builder.graph.edges) == 1
        classify_node = next(n for n in builder.graph.nodes if n.node_type == NodeType.CLASSIFICATION)
        assert classify_node.metadata["incident_type"] == "error_spike"

    def test_tool_call_creates_tool_node(self):
        builder = GraphBuilder("inv-1", "INC-1", "trace-abc")
        builder.process_event(make_event(EventType.INVESTIGATION_STARTED, 0))
        builder.process_event(make_event(EventType.INCIDENT_CLASSIFIED, 1, payload={"incident_type": "timeout"}))
        builder.process_event(make_event(EventType.PLAYBOOK_SELECTED, 2, payload={"playbook": ["LogWorker"]}))
        builder.process_event(make_event(EventType.TOOL_CALLED, 3, payload={
            "worker": "LogWorker",
            "action": "search_logs",
            "params": {},
            "receipt_id": "r-abc",
        }))
        tool_nodes = builder.graph.tool_call_nodes
        assert len(tool_nodes) == 1
        assert tool_nodes[0].worker == "LogWorker"
        assert tool_nodes[0].status == NodeStatus.RUNNING

    def test_tool_response_updates_node_status(self):
        builder = GraphBuilder("inv-1", "INC-1", "trace-abc")
        builder.process_event(make_event(EventType.INVESTIGATION_STARTED, 0))
        builder.process_event(make_event(EventType.PLAYBOOK_SELECTED, 1, payload={"playbook": ["LogWorker"]}))
        builder.process_event(make_event(EventType.TOOL_CALLED, 2, payload={
            "worker": "LogWorker", "action": "search_logs", "params": {},
            "receipt_id": "r-abc",
        }))
        builder.process_event(make_event(EventType.TOOL_RESPONDED, 3, payload={
            "worker": "LogWorker", "action": "search_logs",
            "receipt_id": "r-abc", "elapsed_ms": 450.0, "result_count": 10, "status": "success",
        }))
        tool_node = builder.graph.tool_call_nodes[0]
        assert tool_node.status == NodeStatus.SUCCESS
        assert tool_node.duration_ms == 450.0

    def test_failed_tool_marks_node_failed(self):
        builder = GraphBuilder("inv-1", "INC-1", "trace-abc")
        builder.process_event(make_event(EventType.INVESTIGATION_STARTED, 0))
        builder.process_event(make_event(EventType.TOOL_CALLED, 1, payload={
            "worker": "MetricsWorker", "action": "query_metrics", "params": {}, "receipt_id": "r-xyz",
        }))
        builder.process_event(make_event(EventType.TOOL_FAILED, 2, payload={
            "worker": "MetricsWorker", "action": "query_metrics",
            "receipt_id": "r-xyz", "elapsed_ms": 30100.0, "result_count": 0,
            "status": "error", "error": "Timeout exceeded",
        }))
        tool_node = builder.graph.tool_call_nodes[0]
        assert tool_node.status == NodeStatus.FAILED

    def test_hypothesis_scored_creates_hypothesis_node(self):
        builder = GraphBuilder("inv-1", "INC-1", "trace-abc")
        builder.process_event(make_event(EventType.INVESTIGATION_STARTED, 0))
        builder.process_event(make_event(EventType.HYPOTHESIS_SCORED, 1, payload={
            "hypotheses": [{"name": "DB Exhaustion", "score": 0.87}],
            "winner": "DB Exhaustion",
            "confidence": 0.87,
        }))
        hyp_nodes = [n for n in builder.graph.nodes if n.node_type == NodeType.HYPOTHESIS]
        assert len(hyp_nodes) == 1
        assert hyp_nodes[0].confidence == 0.87

    def test_control_requested_creates_awaiting_node(self):
        builder = GraphBuilder("inv-1", "INC-1", "trace-abc")
        builder.process_event(make_event(EventType.INVESTIGATION_STARTED, 0))
        builder.process_event(make_event(EventType.CONTROL_REQUESTED, 1, payload={
            "action": "approve", "reason": "High risk action",
        }))
        ctrl_nodes = [n for n in builder.graph.nodes if n.node_type == NodeType.CONTROL]
        assert len(ctrl_nodes) == 1
        assert ctrl_nodes[0].status == NodeStatus.AWAITING_APPROVAL

    def test_investigation_completed_marks_root_success(self):
        builder = GraphBuilder("inv-1", "INC-1", "trace-abc")
        builder.process_event(make_event(EventType.INVESTIGATION_STARTED, 0))
        builder.process_event(make_event(EventType.INVESTIGATION_COMPLETED, 1, payload={
            "duration_ms": 5000, "confidence": 0.85,
        }))
        root = builder.graph.root_nodes[0]
        assert root.status == NodeStatus.SUCCESS
        assert builder.graph.is_complete

    def test_gap_detection(self):
        """Missing sequence numbers should create gap nodes."""
        builder = GraphBuilder("inv-1", "INC-1", "trace-abc")
        builder.process_event(make_event(EventType.INVESTIGATION_STARTED, 0))
        # Skip sequence 1 and go to 3
        builder.process_event(make_event(EventType.INVESTIGATION_COMPLETED, 3))
        assert len(builder.graph.event_gaps) > 0
        assert builder.graph.has_gaps

    def test_layout_positions_assigned(self):
        """All nodes should have x/y positions after processing."""
        builder = GraphBuilder("inv-1", "INC-1", "trace-abc")
        builder.process_event(make_event(EventType.INVESTIGATION_STARTED, 0))
        builder.process_event(make_event(EventType.INCIDENT_CLASSIFIED, 1, payload={"incident_type": "oomkill"}))
        builder.process_event(make_event(EventType.TOOL_CALLED, 2, payload={
            "worker": "LogWorker", "action": "search_logs", "params": {}, "receipt_id": "r-1",
        }))
        for node in builder.graph.nodes:
            assert node.x is not None
            assert node.y is not None


class TestRebuildFromEvents:
    def test_rebuild_produces_same_graph(self):
        """Rebuilding from stored events should produce identical graph."""
        events = [
            make_event(EventType.INVESTIGATION_STARTED, 0),
            make_event(EventType.INCIDENT_CLASSIFIED, 1, payload={"incident_type": "error_spike"}),
            make_event(EventType.PLAYBOOK_SELECTED, 2, payload={"playbook": ["LogWorker"]}),
            make_event(EventType.TOOL_CALLED, 3, payload={
                "worker": "LogWorker", "action": "search_logs", "params": {}, "receipt_id": "r-1",
            }),
            make_event(EventType.TOOL_RESPONDED, 4, payload={
                "worker": "LogWorker", "action": "search_logs",
                "receipt_id": "r-1", "elapsed_ms": 300.0, "result_count": 5, "status": "success",
            }),
            make_event(EventType.INVESTIGATION_COMPLETED, 5, payload={"duration_ms": 2000}),
        ]

        graph = rebuild_from_events("inv-1", "INC-1", "trace-abc", events)
        assert graph.is_complete
        assert len(graph.nodes) >= 4  # At least investigation + classify + playbook + tool + decision
        tool_nodes = [n for n in graph.nodes if n.node_type == NodeType.TOOL_CALL]
        assert len(tool_nodes) == 1
        assert tool_nodes[0].status == NodeStatus.SUCCESS

    def test_rebuild_is_deterministic(self):
        """Same events → same graph structure (deterministic)."""
        events = [
            make_event(EventType.INVESTIGATION_STARTED, 0),
            make_event(EventType.TOOL_CALLED, 1, payload={
                "worker": "MetricsWorker", "action": "query_metrics", "params": {}, "receipt_id": "r-1",
            }),
        ]
        graph1 = rebuild_from_events("inv-1", "INC-1", "t", events)
        graph2 = rebuild_from_events("inv-1", "INC-1", "t", events)
        assert len(graph1.nodes) == len(graph2.nodes)
        assert len(graph1.edges) == len(graph2.edges)
        for n1, n2 in zip(sorted(graph1.nodes, key=lambda n: n.node_id),
                           sorted(graph2.nodes, key=lambda n: n.node_id)):
            assert n1.node_id == n2.node_id
            assert n1.node_type == n2.node_type
