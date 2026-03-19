"""AG UI Execution Graph Builder.

Reconstructs the investigation DAG from the ordered event stream.

Algorithm:
1. Process events in sequence_num order
2. Each event type maps to a node type (or node update)
3. Parallel tool calls (same phase) become sibling nodes with shared parent
4. Missing events (sequence gaps) create "Unknown" gap nodes
5. Control gates become CONTROL nodes that block further edges

Graph layout:
- Root: INVESTIGATION node
- L1: CLASSIFICATION + PLAYBOOK nodes
- L2: TOOL_CALL nodes (parallel workers = same rank)
- L3: HYPOTHESIS nodes
- L4: LLM_INFERENCE (optional refinement)
- L5: DECISION node
- L6: RCA node

ReactFlow positioning:
- X axis: time (sequence_num based)
- Y axis: parallel workers spread vertically
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from typing import Optional

from agui.schemas.events import AGUIEvent, EventType
from agui.schemas.graph import (
    ExecutionGraph,
    GraphEdge,
    GraphNode,
    NodeStatus,
    NodeType,
)

logger = logging.getLogger(__name__)

# Layout constants
X_STEP = 200.0    # pixels per level
Y_STEP = 80.0     # pixels per parallel lane


class GraphBuilder:
    """
    Incremental DAG builder.

    Design for streaming use:
    - process_event() is called for each new event
    - Graph is updated incrementally (no full rebuild)
    - Thread-safe: called from async context only (no locks needed)
    """

    def __init__(self, investigation_id: str, incident_id: str, trace_id: str) -> None:
        self.graph = ExecutionGraph(
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            last_updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self._root_node_id: Optional[str] = None
        self._classification_node_id: Optional[str] = None
        self._playbook_node_id: Optional[str] = None
        self._last_phase_node_ids: list[str] = []  # last "phase" parent nodes
        self._tool_call_nodes: dict[str, str] = {}  # receipt_id → node_id
        self._hypothesis_node_id: Optional[str] = None
        self._llm_node_id: Optional[str] = None
        self._decision_node_id: Optional[str] = None
        self._rca_node_id: Optional[str] = None
        self._sequence_counter = 0
        self._expected_seq: int = 0
        self._tool_lane_counter: int = 0

    def process_event(self, event: AGUIEvent) -> None:
        """Process a single event and update the graph."""
        # Detect sequence gaps
        if event.sequence_num > self._expected_seq:
            for missing_seq in range(self._expected_seq, event.sequence_num):
                self._add_gap_node(missing_seq)
        self._expected_seq = event.sequence_num + 1
        self.graph.total_events += 1
        self.graph.last_updated_at = event.timestamp

        etype = event.event_type
        payload = event.payload

        if etype == EventType.INVESTIGATION_STARTED:
            self._on_investigation_started(event)

        elif etype == EventType.INCIDENT_CLASSIFIED:
            self._on_incident_classified(event)

        elif etype == EventType.PLAYBOOK_SELECTED:
            self._on_playbook_selected(event)

        elif etype == EventType.TOOL_CALLED:
            self._on_tool_called(event)

        elif etype in (EventType.TOOL_RESPONDED, EventType.TOOL_FAILED, EventType.TOOL_TIMEOUT):
            self._on_tool_responded(event)

        elif etype == EventType.LLM_INVOKED:
            self._on_llm_invoked(event)

        elif etype == EventType.LLM_RESPONDED:
            self._on_llm_responded(event)

        elif etype == EventType.HYPOTHESIS_SCORED:
            self._on_hypothesis_scored(event)

        elif etype == EventType.HYPOTHESIS_SELECTED:
            self._on_hypothesis_selected(event)

        elif etype == EventType.MEMORY_QUERIED:
            self._on_memory_queried(event)

        elif etype == EventType.CONTROL_REQUESTED:
            self._on_control_requested(event)

        elif etype in (EventType.CONTROL_APPROVED, EventType.CONTROL_REJECTED):
            self._on_control_resolved(event)

        elif etype == EventType.RCA_GENERATED:
            self._on_rca_generated(event)

        elif etype in (EventType.INVESTIGATION_COMPLETED, EventType.INVESTIGATION_FAILED):
            self._on_investigation_ended(event)

        elif etype == EventType.BUDGET_WARNING:
            self._on_budget_warning(event)

        elif etype == EventType.CIRCUIT_BREAKER_TRIPPED:
            self._on_circuit_breaker(event)

        # Recompute layout after each event
        self._compute_layout()

    # ── Event handlers ──────────────────────────────────────────────────────

    def _on_investigation_started(self, event: AGUIEvent) -> None:
        node = self._make_node(
            node_id=f"inv_{event.investigation_id[:8]}",
            node_type=NodeType.INVESTIGATION,
            label=f"Investigation\n{event.incident_id}",
            event=event,
            status=NodeStatus.RUNNING,
        )
        self.graph.add_node(node)
        self._root_node_id = node.node_id
        self._last_phase_node_ids = [node.node_id]

    def _on_incident_classified(self, event: AGUIEvent) -> None:
        incident_type = event.payload.get("incident_type", "unknown")
        node = self._make_node(
            node_id=f"classify_{event.sequence_num}",
            node_type=NodeType.CLASSIFICATION,
            label=f"Classified\n{incident_type}",
            event=event,
            status=NodeStatus.SUCCESS,
            metadata={"incident_type": incident_type},
        )
        self.graph.add_node(node)
        self._classification_node_id = node.node_id
        self._add_edges_from_parents(node)
        self._last_phase_node_ids = [node.node_id]

    def _on_playbook_selected(self, event: AGUIEvent) -> None:
        playbook = event.payload.get("playbook", [])
        node = self._make_node(
            node_id=f"playbook_{event.sequence_num}",
            node_type=NodeType.PLAYBOOK,
            label=f"Playbook\n{len(playbook)} workers",
            event=event,
            status=NodeStatus.SUCCESS,
            metadata={"playbook": playbook},
        )
        self.graph.add_node(node)
        self._playbook_node_id = node.node_id
        self._add_edges_from_parents(node)
        self._last_phase_node_ids = [node.node_id]
        self._tool_lane_counter = 0

    def _on_tool_called(self, event: AGUIEvent) -> None:
        worker = event.payload.get("worker", "unknown")
        action = event.payload.get("action", "unknown")
        receipt_id = event.payload.get("receipt_id", "")
        node_id = f"tool_{event.sequence_num}_{worker.lower()[:6]}"
        node = self._make_node(
            node_id=node_id,
            node_type=NodeType.TOOL_CALL,
            label=f"{worker}\n{action}",
            event=event,
            status=NodeStatus.RUNNING,
            worker=worker,
            action=action,
            metadata={
                "receipt_id": receipt_id,
                "tool": event.payload.get("tool", ""),
                "params": event.payload.get("params", {}),
            },
        )
        # Assign lane for parallel layout
        node.metadata["lane"] = self._tool_lane_counter
        self._tool_lane_counter += 1

        if receipt_id:
            node.receipt_id = receipt_id
            self._tool_call_nodes[receipt_id] = node_id

        self.graph.add_node(node)
        self._add_edges_from_parents(node)

    def _on_tool_responded(self, event: AGUIEvent) -> None:
        receipt_id = event.payload.get("receipt_id", "")
        node_id = self._tool_call_nodes.get(receipt_id)
        if not node_id:
            # Tool responded without a matching tool.called event (gap)
            return
        node = self.graph.get_node(node_id)
        if not node:
            return

        etype = event.event_type
        elapsed = event.payload.get("elapsed_ms", 0)
        error = event.payload.get("error")

        node.status = (
            NodeStatus.SUCCESS if etype == EventType.TOOL_RESPONDED
            else NodeStatus.TIMEOUT if etype == EventType.TOOL_TIMEOUT
            else NodeStatus.FAILED
        )
        node.completed_at = event.timestamp
        node.duration_ms = elapsed
        if error:
            node.error_message = str(error)[:200]

        # After all tools complete, set last phase to tool nodes
        tool_nodes = [n for n in self.graph.nodes if n.node_type == NodeType.TOOL_CALL]
        self._last_phase_node_ids = [n.node_id for n in tool_nodes if not n.child_ids]

    def _on_llm_invoked(self, event: AGUIEvent) -> None:
        node = self._make_node(
            node_id=f"llm_{event.sequence_num}",
            node_type=NodeType.LLM_INFERENCE,
            label=f"LLM\nRefinement",
            event=event,
            status=NodeStatus.RUNNING,
            llm_model=event.payload.get("model", ""),
            metadata={"purpose": event.payload.get("purpose", "hypothesis_refinement")},
        )
        self.graph.add_node(node)
        self._llm_node_id = node.node_id
        self._add_edges_from_parents(node)
        self._last_phase_node_ids = [node.node_id]

    def _on_llm_responded(self, event: AGUIEvent) -> None:
        if not self._llm_node_id:
            return
        node = self.graph.get_node(self._llm_node_id)
        if node:
            node.status = NodeStatus.SUCCESS
            node.completed_at = event.timestamp
            node.token_count = event.payload.get("total_tokens", 0)

    def _on_hypothesis_scored(self, event: AGUIEvent) -> None:
        hypotheses = event.payload.get("hypotheses", [])
        winner = event.payload.get("winner", "")
        confidence = event.payload.get("confidence", 0.0)
        node = self._make_node(
            node_id=f"hyp_{event.sequence_num}",
            node_type=NodeType.HYPOTHESIS,
            label=f"Hypotheses\n{len(hypotheses)} scored",
            event=event,
            status=NodeStatus.SUCCESS,
            confidence=confidence,
            metadata={
                "hypotheses": hypotheses,
                "winner": winner,
                "count": len(hypotheses),
            },
        )
        self.graph.add_node(node)
        self._hypothesis_node_id = node.node_id
        self._add_edges_from_parents(node)
        self._last_phase_node_ids = [node.node_id]

    def _on_hypothesis_selected(self, event: AGUIEvent) -> None:
        winner = event.payload.get("winner", "")
        confidence = event.payload.get("confidence", 0.0)
        node = self._make_node(
            node_id=f"decision_{event.sequence_num}",
            node_type=NodeType.DECISION,
            label=f"Decision\n{winner[:20]}",
            event=event,
            status=NodeStatus.SUCCESS,
            confidence=confidence,
            hypothesis_name=winner,
        )
        self.graph.add_node(node)
        self._decision_node_id = node.node_id
        self._add_edges_from_parents(node)
        self._last_phase_node_ids = [node.node_id]

    def _on_memory_queried(self, event: AGUIEvent) -> None:
        node = self._make_node(
            node_id=f"mem_{event.sequence_num}",
            node_type=NodeType.MEMORY_QUERY,
            label=f"Memory\nSearch",
            event=event,
            status=NodeStatus.RUNNING,
            metadata={"query": event.payload.get("query", "")},
        )
        self.graph.add_node(node)
        self._add_edges_from_parents(node)

    def _on_control_requested(self, event: AGUIEvent) -> None:
        action = event.payload.get("action", "approve")
        node = self._make_node(
            node_id=f"ctrl_{event.sequence_num}",
            node_type=NodeType.CONTROL,
            label=f"⏸ Awaiting\n{action}",
            event=event,
            status=NodeStatus.AWAITING_APPROVAL,
            metadata={"action": action, "reason": event.payload.get("reason", "")},
        )
        self.graph.add_node(node)
        self._add_edges_from_parents(node)
        self._last_phase_node_ids = [node.node_id]

    def _on_control_resolved(self, event: AGUIEvent) -> None:
        ctrl_node_id = event.payload.get("node_id")
        if ctrl_node_id:
            node = self.graph.get_node(ctrl_node_id)
            if node:
                node.status = (
                    NodeStatus.APPROVED
                    if event.event_type == EventType.CONTROL_APPROVED
                    else NodeStatus.REJECTED
                )
                node.completed_at = event.timestamp

    def _on_rca_generated(self, event: AGUIEvent) -> None:
        confidence = event.payload.get("confidence", 0.0)
        node = self._make_node(
            node_id=f"rca_{event.sequence_num}",
            node_type=NodeType.RCA,
            label=f"RCA\n{confidence:.0%} confidence",
            event=event,
            status=NodeStatus.SUCCESS,
            confidence=confidence,
            metadata={
                "root_cause": event.payload.get("root_cause", ""),
                "remediation_count": len(event.payload.get("remediation", [])),
            },
        )
        self.graph.add_node(node)
        self._rca_node_id = node.node_id
        self._add_edges_from_parents(node)
        self._last_phase_node_ids = [node.node_id]

    def _on_investigation_ended(self, event: AGUIEvent) -> None:
        if self._root_node_id:
            root = self.graph.get_node(self._root_node_id)
            if root:
                root.status = (
                    NodeStatus.SUCCESS
                    if event.event_type == EventType.INVESTIGATION_COMPLETED
                    else NodeStatus.FAILED
                )
                root.completed_at = event.timestamp
        self.graph.is_complete = True

    def _on_budget_warning(self, event: AGUIEvent) -> None:
        if self._root_node_id:
            root = self.graph.get_node(self._root_node_id)
            if root:
                root.metadata["budget_warning"] = event.payload

    def _on_circuit_breaker(self, event: AGUIEvent) -> None:
        worker = event.payload.get("worker", "unknown")
        # Find the most recent running tool node for this worker and mark it failed
        for node in reversed(self.graph.nodes):
            if (node.node_type == NodeType.TOOL_CALL
                    and node.worker == worker
                    and node.status == NodeStatus.RUNNING):
                node.status = NodeStatus.FAILED
                node.error_message = "Circuit breaker tripped"
                break

    # ── Utilities ────────────────────────────────────────────────────────────

    def _make_node(
        self,
        node_id: str,
        node_type: NodeType,
        label: str,
        event: AGUIEvent,
        status: NodeStatus = NodeStatus.PENDING,
        **kwargs,
    ) -> GraphNode:
        return GraphNode(
            node_id=node_id,
            node_type=node_type,
            label=label,
            investigation_id=event.investigation_id,
            trace_id=event.trace_id,
            span_id=event.span_id,
            status=status,
            started_at=event.timestamp,
            event_id=event.event_id,
            sequence_num=event.sequence_num,
            **kwargs,
        )

    def _add_edges_from_parents(self, node: GraphNode) -> None:
        """Add edges from current phase parents to this node."""
        for parent_id in self._last_phase_node_ids:
            edge = GraphEdge(
                edge_id=f"e_{parent_id}_{node.node_id}",
                source_id=parent_id,
                target_id=node.node_id,
                edge_type="execution",
            )
            self.graph.add_edge(edge)

    def _add_gap_node(self, missing_seq: int) -> None:
        """Add a placeholder node for a missing event."""
        self.graph.event_gaps.append(missing_seq)
        node = GraphNode(
            node_id=f"gap_{missing_seq}",
            node_type=NodeType.TOOL_CALL,
            label=f"? Missing\nevent #{missing_seq}",
            investigation_id=self.graph.investigation_id,
            trace_id=self.graph.trace_id,
            status=NodeStatus.SKIPPED,
            sequence_num=missing_seq,
            metadata={"is_gap": True},
        )
        self.graph.add_node(node)
        self._add_edges_from_parents(node)

    def _compute_layout(self) -> None:
        """
        Assign x/y positions for ReactFlow rendering.

        Strategy: topological sort → assign level → spread parallel nodes vertically.
        """
        if not self.graph.nodes:
            return

        # BFS level assignment
        levels: dict[str, int] = {}
        queue = list(self.graph.root_nodes)
        for n in queue:
            levels[n.node_id] = 0

        visited = set()
        while queue:
            node = queue.pop(0)
            if node.node_id in visited:
                continue
            visited.add(node.node_id)
            for child_id in node.child_ids:
                child = self.graph.get_node(child_id)
                if child:
                    levels[child.node_id] = max(
                        levels.get(child.node_id, 0),
                        levels.get(node.node_id, 0) + 1,
                    )
                    queue.append(child)

        # Group by level for vertical spread
        level_groups: dict[int, list[str]] = {}
        for nid, lvl in levels.items():
            level_groups.setdefault(lvl, []).append(nid)

        for lvl, node_ids in level_groups.items():
            for i, nid in enumerate(node_ids):
                node = self.graph.get_node(nid)
                if node:
                    node.x = float(lvl * X_STEP)
                    total_height = len(node_ids) * Y_STEP
                    node.y = float(i * Y_STEP - total_height / 2)


# Registry: investigation_id → GraphBuilder
_builders: dict[str, GraphBuilder] = {}


def get_or_create_builder(
    investigation_id: str, incident_id: str, trace_id: str
) -> GraphBuilder:
    if investigation_id not in _builders:
        _builders[investigation_id] = GraphBuilder(investigation_id, incident_id, trace_id)
    return _builders[investigation_id]


def get_builder(investigation_id: str) -> Optional[GraphBuilder]:
    return _builders.get(investigation_id)


def rebuild_from_events(
    investigation_id: str,
    incident_id: str,
    trace_id: str,
    events: list[AGUIEvent],
) -> ExecutionGraph:
    """Rebuild DAG from stored events (for replay + late load)."""
    builder = GraphBuilder(investigation_id, incident_id, trace_id)
    sorted_events = sorted(events, key=lambda e: e.sequence_num)
    for event in sorted_events:
        builder.process_event(event)
    return builder.graph
