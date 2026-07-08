"""Investigation Artifact writer — Wave 1 runtime hook.

Called by ``investigate()`` immediately after ``attach_receipts`` on
every return path. Produce-only: builds the canonical artifact and
persists it as a CANDIDATE. Nothing at runtime reads it back.

Feature flags (both default OFF — flag off ⇒ byte-identical runtime):

  INVESTIGATION_ARTIFACT_ENABLED  gates the candidate write entirely.
  ADMISSION_CONTROL_ENABLED       additionally records the admission
                                  classification as an audit event.
                                  Candidate-only mode: the decision is
                                  recorded, no state transition happens.

Never raises — a learning-write failure must never fail an
investigation (the ``_store_to_memory`` contract). Failures emit one
structured warning with a named event for observability.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Mapping

logger = logging.getLogger("sentinalai.artifact_writer")

_DEFAULT_STORE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "eval", "investigation_artifacts",
)


def _flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def _store_path() -> str:
    return os.environ.get("ARTIFACT_STORE_PATH", _DEFAULT_STORE_PATH)


_DEFAULT_MEMORY_RECORD_PATH = os.path.join(
    os.path.dirname(__file__), "..", "eval", "memory_records",
)


def _memory_record_root() -> str:
    return os.environ.get("MEMORY_RECORD_STORE_PATH",
                          _DEFAULT_MEMORY_RECORD_PATH)


def _provenance() -> dict[str, Any]:
    """Snapshot of the runtime configuration that shaped this run."""
    return {
        "producer": "wave1",
        "planner_mode": (
            "agentic" if _flag("AGENTIC_PLANNER") else "playbook"
        ),
        "loop_controller": _flag("LOOP_CONTROLLER_ENABLED"),
        "llm_model_id": os.environ.get("LLM_MODEL_ID", ""),
    }


def maybe_write_investigation_artifact(
    result: Mapping[str, Any] | None,
    incident_id: str,
    investigation_id: str = "",
) -> None:
    """Build + persist a candidate artifact when the flag is on.

    Reads ``result`` only — never mutates it. No-op (and no imports of
    the artifact package) when INVESTIGATION_ARTIFACT_ENABLED is off.
    """
    if not _flag("INVESTIGATION_ARTIFACT_ENABLED"):
        return
    if not isinstance(result, dict):
        return
    try:
        from sentinel_core.investigation_artifact import (
            AdmissionController,
            ArtifactStore,
            build_artifact,
        )

        created_at = datetime.now(timezone.utc).isoformat()
        artifact = build_artifact(
            result,
            incident_id=incident_id,
            investigation_id=investigation_id,
            created_at=created_at,
            provenance=_provenance(),
        )
        store = ArtifactStore(_store_path())
        store.save_candidate(artifact)
        logger.info(
            "artifact.candidate.written artifact_id=%s incident_id=%s "
            "status=%s",
            artifact.artifact_id, incident_id, artifact.status,
        )

        decision = None
        if _flag("ADMISSION_CONTROL_ENABLED"):
            decision = AdmissionController().classify(artifact)
            store.record_decision(
                artifact.artifact_id, decision.state,
                reasons=decision.reasons, at=created_at,
            )
            logger.info(
                "artifact.admission.classified artifact_id=%s decision=%s "
                "reasons=%s",
                artifact.artifact_id, decision.state,
                ",".join(decision.reasons),
            )

        # Wave 2 — admission-controlled MemoryRecord projection.
        # Produce-only: nothing at runtime reads these records back.
        if _flag("MEMORY_RECORD_FROM_ARTIFACT_ENABLED"):
            from sentinel_core.intel_memory import MemoryRecord, MemoryStore

            record = MemoryRecord.from_artifact(artifact)
            memory_root = _memory_record_root()
            if _flag("MEMORY_ADMISSION_ENABLED"):
                # Only admitted (or offline-validated) artifacts may enter
                # ACTIVE memory. Fail-closed: quarantined/rejected are
                # skipped entirely — the artifact store retains them.
                if decision is None:
                    decision = AdmissionController().classify(artifact)
                if decision.state == "admitted":
                    mstore = MemoryStore(memory_root)
                    if not mstore.has(record.memory_id):   # append-only
                        mstore.save(record)
                    logger.info(
                        "memory_record.admitted.written memory_id=%s",
                        record.memory_id,
                    )
                else:
                    logger.info(
                        "memory_record.skipped memory_id=%s decision=%s "
                        "reasons=%s",
                        record.memory_id, decision.state,
                        ",".join(decision.reasons),
                    )
            else:
                # Candidate-only mode (explicitly flagged): records land
                # in an INACTIVE side area never scanned as active memory.
                mstore = MemoryStore(os.path.join(memory_root, ".candidate"))
                if not mstore.has(record.memory_id):        # append-only
                    mstore.save(record)
                logger.info(
                    "memory_record.candidate.written memory_id=%s",
                    record.memory_id,
                )
    except Exception as exc:
        # Named event — the review's "no silent swallow" rule (M6).
        logger.warning(
            "artifact.write.failed incident_id=%s error_type=%s error=%s",
            incident_id, type(exc).__name__, exc,
        )


__all__ = ["maybe_write_investigation_artifact"]
