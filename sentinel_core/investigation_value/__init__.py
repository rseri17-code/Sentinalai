"""Investigation Value — Wave 3 Readiness Program.

Offline, deterministic measurement of whether memory objectively
improves investigations, plus automatic evaluation of the Wave 3
readiness gates (G1-G11).

Produce-only: consumes Investigation Artifacts and MemoryRecords;
never touches runtime. Wave 3 remains disabled regardless of score.
"""
from sentinel_core.investigation_value.metrics import (
    METRICS_SCHEMA_VERSION,
    confidence_gain,
    evidence_acceleration_score,
    false_lead_elimination_score,
    investigation_improvement_potential,
    planner_guidance_score,
    root_cause_acceleration_score,
    worker_reduction_score,
)
from sentinel_core.investigation_value.readiness import (
    GATES,
    READINESS_SCHEMA_VERSION,
    GateInputs,
    evaluate_gates,
)
from sentinel_core.investigation_value.shadow_pipeline import (
    run_readiness_evaluation,
)

__all__ = [
    "GATES",
    "GateInputs",
    "METRICS_SCHEMA_VERSION",
    "READINESS_SCHEMA_VERSION",
    "confidence_gain",
    "evaluate_gates",
    "evidence_acceleration_score",
    "false_lead_elimination_score",
    "investigation_improvement_potential",
    "planner_guidance_score",
    "root_cause_acceleration_score",
    "run_readiness_evaluation",
    "worker_reduction_score",
]
