"""sentinel_core.models — all shared data models for SentinalAI.

Import directly from sub-modules for clarity:
    from sentinel_core.models.incident import Incident
    from sentinel_core.models.events import AGUIEvent, EventType
    from sentinel_core.models.dev_task import DevTask, DevTaskStatus

Or from this package for convenience:
    from sentinel_core.models import Incident, AGUIEvent, EventType
"""
from sentinel_core.models.incident import (
    Incident,
    _normalize_severity,
    _normalize_snow_state,
    _extract_pd_assignee,
)
from sentinel_core.models.events import (
    AGUIEvent,
    EventType,
    EventSchema,
    CURRENT_SCHEMA_VERSION,
)
from sentinel_core.models.receipts import (
    UIReceipt,
    ReceiptSchema,
    CURRENT_RECEIPT_SCHEMA_VERSION,
)
from sentinel_core.models.incidents import (
    IncidentState,
    InvestigationStatus,
    IncidentSeverity,
    ControlAction,
    ControlActionType,
    HypothesisSummary,
    MemoryMatch,
    CURRENT_INCIDENT_SCHEMA_VERSION,
)
from sentinel_core.models.dev_task import (
    DevTask,
    DevTaskStatus,
    DevTaskSource,
    DevTaskPriority,
    DevTaskType,
    ValidationResult,
    CIRun,
    ReviewComment,
    CURRENT_DEV_TASK_SCHEMA_VERSION,
)
from sentinel_core.models.graph import (
    GraphNode,
    GraphEdge,
    ExecutionGraph,
    NodeType,
    NodeStatus,
    CURRENT_GRAPH_SCHEMA_VERSION,
)
from sentinel_core.models.inference import (
    InferenceError,
    InferenceUsage,
    InferenceRequest,
    InferenceResponse,
    StructuredResult,
    InferencePort,
)
from sentinel_core.models.workflow import (
    WorkflowPhase,
    WorkflowStatus,
    PhaseStatus,
    ExecutionMetadata,
    PhaseResult,
    WorkflowCheckpoint,
    WorkflowState,
    WorkflowPort,
)

__all__ = [
    # incident
    "Incident",
    # events
    "AGUIEvent", "EventType", "EventSchema", "CURRENT_SCHEMA_VERSION",
    # receipts
    "UIReceipt", "ReceiptSchema", "CURRENT_RECEIPT_SCHEMA_VERSION",
    # incidents
    "IncidentState", "InvestigationStatus", "IncidentSeverity",
    "ControlAction", "ControlActionType", "HypothesisSummary", "MemoryMatch",
    "CURRENT_INCIDENT_SCHEMA_VERSION",
    # dev_task
    "DevTask", "DevTaskStatus", "DevTaskSource", "DevTaskPriority",
    "DevTaskType", "ValidationResult", "CIRun", "ReviewComment",
    "CURRENT_DEV_TASK_SCHEMA_VERSION",
    # graph
    "GraphNode", "GraphEdge", "ExecutionGraph", "NodeType", "NodeStatus",
    "CURRENT_GRAPH_SCHEMA_VERSION",
    # inference
    "InferenceError", "InferenceUsage", "InferenceRequest",
    "InferenceResponse", "StructuredResult", "InferencePort",
    # workflow
    "WorkflowPhase", "WorkflowStatus", "PhaseStatus", "ExecutionMetadata",
    "PhaseResult", "WorkflowCheckpoint", "WorkflowState", "WorkflowPort",
]
