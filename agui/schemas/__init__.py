"""AG UI data contract schemas — single source of truth.

All schemas are versioned. Breaking changes require a major version bump.
Consumers must handle unknown fields gracefully.
"""
from agui.schemas.events import AGUIEvent, EventType, EventSchema
from agui.schemas.receipts import UIReceipt, ReceiptSchema
from agui.schemas.graph import GraphNode, GraphEdge, ExecutionGraph, NodeType, NodeStatus
from agui.schemas.incidents import IncidentState, ControlAction, ControlActionType, InvestigationStatus

__all__ = [
    "AGUIEvent", "EventType", "EventSchema",
    "UIReceipt", "ReceiptSchema",
    "GraphNode", "GraphEdge", "ExecutionGraph", "NodeType", "NodeStatus",
    "IncidentState", "ControlAction", "ControlActionType", "InvestigationStatus",
]
