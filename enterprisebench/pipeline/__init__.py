"""EnterpriseBench EB-2 — the deterministic Investigation Evaluation Pipeline.

Transforms EFIC reasoning cases into production-shaped MCP interactions, drives
the UNMODIFIED SentinelAI investigation engine end-to-end through its real MCP
boundary, captures the complete investigation trace, and evaluates the engine's
investigation *process* against the hidden Enterprise Investigation Specification.

Prime directive (identical to the EnterpriseBench architecture): this subsystem
modifies nothing in the engine, planner, runtime, replay, confidence, or
recommendation logic. It treats SentinelAI as a black box, feeding it telemetry
through the existing ``McpGateway`` boundary and reading only observable outputs.

The Enterprise Investigation Specification (``efic.investigation_spec``) and the
hidden answer key (``task.ground_truth`` / ``task.traps``) are EVALUATION-ONLY.
They are NEVER passed to the engine — the ``BenchMCPSource`` is constructed from
the public ``task.incident`` + ``task.telemetry`` alone. See ``bench_source`` and
the isolation proof in ``tests/enterprisebench/test_eb2.py``.
"""
from __future__ import annotations

EB2_VERSION = "0.2.0"

__all__ = ["EB2_VERSION"]
