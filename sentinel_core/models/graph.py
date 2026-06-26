"""AG UI Execution Graph Schema v1.0

The execution graph is a Directed Acyclic Graph (DAG) that represents
the causal structure of an investigation. Every node maps to:
- an event in the event stream
- a receipt in the receipt store
- a position in the investigation timeline

Graph is reconstructed deterministically from the ordered event stream.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


CURRENT_GRAPH_SCHEMA_VERSION = "1.0"


class NodeType(str, Enum):
    INVESTIGATION = "investigation"    # Root node
    CLASSIFICATION = "classification"  # Incident type classification
    PLAYBOOK = "playbook"              # Playbook selection
    TOOL_CALL = "tool_call"            # Worker tool invocation
    LLM_INFERENCE = "llm_inference"    # LLM call
    HYPOTHESIS = "hypothesis"          # Hypothesis generation/scoring
    MEMORY_QUERY = "memory_query"      # AgentCore memory search
    DECISION = "decision"              # Hypothesis selection / RCA finalization
    CONTROL = "control"                # Human-in-the-loop gate
    RCA = "rca"                        # Final RCA output


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class GraphNode(BaseModel):
    """Single node in the execution DAG."""
    model_config = {"extra": "allow"}

    # Identity
    node_id: str                    # Unique within graph
    schema_version: str = Field(default=CURRENT_GRAPH_SCHEMA_VERSION)
    node_type: NodeType
    label: str                       # Human-readable label

    # Context
    investigation_id: str
    trace_id: str
    span_id: str = Field(default="")

    # Graph structure
    parent_ids: list[str] = Field(default_factory=list)
    child_ids: list[str] = Field(default_factory=list)

    # State
    status: NodeStatus = NodeStatus.PENDING
    started_at: Optional[str] = None   # ISO 8601
    completed_at: Optional[str] = None  # ISO 8601
    duration_ms: Optional[float] = None

    # Evidence linkage
    receipt_id: Optional[str] = None
    event_id: Optional[str] = None
    sequence_num: int = 0

    # Semantic data
    worker: Optional[str] = None       # For TOOL_CALL nodes
    action: Optional[str] = None       # For TOOL_CALL nodes
    tool: Optional[str] = None         # MCP tool name
    confidence: Optional[float] = None  # For HYPOTHESIS/DECISION nodes
    hypothesis_name: Optional[str] = None  # For HYPOTHESIS nodes
    llm_model: Optional[str] = None    # For LLM_INFERENCE nodes
    token_count: Optional[int] = None  # For LLM_INFERENCE nodes

    # Display
    metadata: dict[str, Any] = Field(default_factory=dict)
    error_message: Optional[str] = None

    # ReactFlow positioning (set by layout algorithm)
    x: Optional[float] = None
    y: Optional[float] = None


class GraphEdge(BaseModel):
    """Directed edge between two graph nodes."""
    edge_id: str
    source_id: str                  # parent node
    target_id: str                  # child node
    edge_type: str = "execution"    # execution | data | hypothesis | control
    label: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionGraph(BaseModel):
    """Complete execution DAG for an investigation."""
    model_config = {"extra": "allow"}

    investigation_id: str
    incident_id: str
    trace_id: str
    schema_version: str = Field(default=CURRENT_GRAPH_SCHEMA_VERSION)

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)

    # Metadata
    created_at: str = Field(default="")
    last_updated_at: str = Field(default="")
    is_complete: bool = False
    total_events: int = 0
    event_gaps: list[int] = Field(default_factory=list)  # Missing sequence numbers

    def get_node(self, node_id: str) -> Optional[GraphNode]:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        return None

    def add_node(self, node: GraphNode) -> None:
        # Prevent duplicates
        existing = {n.node_id for n in self.nodes}
        if node.node_id not in existing:
            self.nodes.append(node)

    def add_edge(self, edge: GraphEdge) -> None:
        existing = {e.edge_id for e in self.edges}
        if edge.edge_id not in existing:
            self.edges.append(edge)
            # Update child_ids on source, parent_ids on target
            src = self.get_node(edge.source_id)
            tgt = self.get_node(edge.target_id)
            if src and edge.target_id not in src.child_ids:
                src.child_ids.append(edge.target_id)
            if tgt and edge.source_id not in tgt.parent_ids:
                tgt.parent_ids.append(edge.source_id)

    @property
    def root_nodes(self) -> list[GraphNode]:
        return [n for n in self.nodes if not n.parent_ids]

    @property
    def leaf_nodes(self) -> list[GraphNode]:
        return [n for n in self.nodes if not n.child_ids]

    @property
    def tool_call_nodes(self) -> list[GraphNode]:
        return [n for n in self.nodes if n.node_type == NodeType.TOOL_CALL]

    @property
    def has_gaps(self) -> bool:
        return len(self.event_gaps) > 0
