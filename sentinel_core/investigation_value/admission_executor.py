"""R1 — Offline Admission Executor.

The missing loop edge the Closed-Loop Validation identified: decisions
were classified and recorded but never EXECUTED, so the admitted corpus
was pinned at zero. This module closes the edge:

    candidate ─classify─▶ admitted / quarantined / rejected   (transition)
    admitted  ─Q5-Q7 signal─▶ quarantined                      (demotion)
    admitted  ─validation signal─▶ validated                   (event-only)
    admitted  ─▶ MemoryRecord ─▶ ACTIVE MemoryStore            (projection)

Guarantees: append-only (transitions are file moves + audit events;
memory removal archives via ``MemoryStore.delete`` — RC-E, never
destroys), deterministic (sorted processing order, caller-supplied
timestamp, pure classifier), replayable (audit JSONL is the full
history), auditable (every action is an event).

Offline only. Never touches runtime. Never raises past its report.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from sentinel_core.intel_memory import MemoryRecord, MemoryStore
from sentinel_core.investigation_artifact import (
    AdmissionController,
    ArtifactStore,
)

EXECUTOR_SCHEMA_VERSION = 1

# Retroactive signals that demote an admitted artifact (audit Q5-Q7).
_DEMOTION_SIGNALS = (
    "operator_rejected", "replay_regression", "benchmark_disagreement",
)


def run_admission_review(
    artifact_root: Path | str,
    memory_root: Path | str,
    at: str,
    signals_by_artifact: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """One deterministic admission pass. Returns an auditable report.

    ``signals_by_artifact`` carries per-artifact offline signals:
    benchmark_pointer / benchmark_disagreement (R2 matcher output),
    operator_rejected, replay_regression, validated (bool).
    """
    astore = ArtifactStore(artifact_root)
    mstore = MemoryStore(memory_root)
    controller = AdmissionController()
    signals = {str(k): dict(v) for k, v in (signals_by_artifact or {}).items()}

    report: dict[str, Any] = {
        "schema_version": EXECUTOR_SCHEMA_VERSION,
        "at": str(at),
        "admitted": [], "quarantined": [], "rejected": [],
        "demoted": [], "validated": [], "errors": [],
    }

    # ── Phase A: classify + execute every candidate (sorted = determinism)
    for aid in astore.list_ids("candidate"):
        try:
            artifact = astore.load(aid)
            decision = controller.classify(artifact, signals.get(aid))
            astore.transition(aid, decision.state,
                               reasons=decision.reasons, at=at)
            report[decision.state].append(aid)
            if decision.state == "admitted":
                record = MemoryRecord.from_artifact(artifact)
                if not mstore.has(record.memory_id):     # append-only
                    mstore.save(record)
        except Exception as exc:
            report["errors"].append(
                {"artifact_id": aid, "stage": "classify",
                 "error": f"{type(exc).__name__}: {exc}"})

    # ── Phase B: retroactive demotion of previously admitted artifacts
    for aid in astore.list_ids("admitted"):
        sig = signals.get(aid)
        if not sig:
            continue
        fired = tuple(sorted(
            f"retro:{name}" for name in _DEMOTION_SIGNALS if sig.get(name)
        ))
        if not fired:
            continue
        try:
            astore.transition(aid, "quarantined", reasons=fired, at=at)
            # Active memory must not retain a demoted record. RC-E:
            # delete() archives to .deleted/ — history preserved.
            if mstore.has(aid):
                mstore.delete(aid)
            report["demoted"].append(aid)
        except Exception as exc:
            report["errors"].append(
                {"artifact_id": aid, "stage": "demote",
                 "error": f"{type(exc).__name__}: {exc}"})

    # ── Phase C: validation promotion (event-only; bytes untouched)
    for aid in astore.list_ids("admitted"):
        sig = signals.get(aid)
        if not sig or not sig.get("validated"):
            continue
        if astore.state_of(aid) != "admitted":     # demoted above
            continue
        try:
            astore.transition(aid, "validated",
                               reasons=("retro:validated",), at=at)
            report["validated"].append(aid)
        except Exception as exc:
            report["errors"].append(
                {"artifact_id": aid, "stage": "validate",
                 "error": f"{type(exc).__name__}: {exc}"})

    for key in ("admitted", "quarantined", "rejected",
                 "demoted", "validated"):
        report[key] = sorted(report[key])
    return report


__all__ = ["EXECUTOR_SCHEMA_VERSION", "run_admission_review"]
