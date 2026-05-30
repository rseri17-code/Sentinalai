"""Replay system for SentinalAI investigations.

Persists investigation receipts + outputs as replay artifacts.
Replay mode rehydrates from stored receipts without making external calls.

Stepwise replay (replay_stepwise) lets callers walk through a past
investigation one tool call at a time, observing how evidence accumulated
and how the hypothesis would have evolved at each step.

Retention policy (applied on every save):
  REPLAY_MAX_AGE_HOURS  — delete artifacts older than this (default: 24)
  REPLAY_MAX_FILES      — keep at most this many artifacts total (default: 500)
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Generator

logger = logging.getLogger(__name__)

DEFAULT_REPLAY_DIR = os.getenv("SENTINALAI_REPLAY_DIR", "/tmp/sentinalai_replays")
_MAX_AGE_HOURS = float(os.getenv("REPLAY_MAX_AGE_HOURS", "24"))
_MAX_FILES = int(os.getenv("REPLAY_MAX_FILES", "500"))


@dataclass
class ReplayStep:
    """A single step in a stepwise replay.

    Attributes:
        step_num:          1-based position in the tool-call sequence.
        total_steps:       Total number of steps in the replay.
        receipt:           The tool-call receipt executed at this step
                           (worker, action, params, result, elapsed_ms, status).
        evidence_snapshot: Evidence dict as it existed *after* this step.
        partial_result:    RCA analysis result computed from evidence_snapshot,
                           or None if no analysis function was provided.
    """
    step_num: int
    total_steps: int
    receipt: dict
    evidence_snapshot: dict = field(default_factory=dict)
    partial_result: dict | None = None


class ReplayStore:
    """Persists and loads investigation replay artifacts."""

    def __init__(self, replay_dir: str = DEFAULT_REPLAY_DIR):
        self.replay_dir = Path(replay_dir)

    def save(
        self,
        case_id: str,
        receipts: list[dict],
        result: dict,
        evidence: dict | None = None,
    ) -> str:
        """Save an investigation artifact. Returns the artifact path."""
        self.replay_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{case_id}_{ts}.json"
        path = self.replay_dir / filename

        artifact = {
            "case_id": case_id,
            "timestamp": ts,
            "receipts": receipts,
            "result": result,
            "evidence": evidence or {},
        }

        path.write_text(json.dumps(artifact, indent=2, default=str))
        logger.info("Saved replay artifact: %s", path)
        self._purge_old_artifacts()
        return str(path)

    def _purge_old_artifacts(
        self,
        max_age_hours: float = _MAX_AGE_HOURS,
        max_files: int = _MAX_FILES,
    ) -> int:
        """Delete artifacts that are too old or exceed the file count limit.

        Returns the number of files deleted.
        """
        if not self.replay_dir.exists():
            return 0

        all_files = sorted(self.replay_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
        cutoff = time.time() - max_age_hours * 3600
        deleted = 0

        # Pass 1: delete by age
        remaining = []
        for f in all_files:
            if f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                    deleted += 1
                    logger.debug("Replay purge (age): deleted %s", f.name)
                except OSError as exc:
                    logger.warning("Replay purge: could not delete %s: %s", f.name, exc)
            else:
                remaining.append(f)

        # Pass 2: delete oldest if count still exceeds limit (newest-first keeps)
        if len(remaining) > max_files:
            to_delete = remaining[: len(remaining) - max_files]
            for f in to_delete:
                try:
                    f.unlink()
                    deleted += 1
                    logger.debug("Replay purge (count): deleted %s", f.name)
                except OSError as exc:
                    logger.warning("Replay purge: could not delete %s: %s", f.name, exc)

        if deleted:
            logger.info("Replay purge: deleted %d artifact(s)", deleted)
        return deleted

    def load(self, case_id: str) -> dict | None:
        """Load the most recent replay artifact for a case."""
        if not self.replay_dir.exists():
            return None

        # Find all matching files
        matches = sorted(
            self.replay_dir.glob(f"{case_id}_*.json"),
            reverse=True,
        )
        if not matches:
            return None

        try:
            return json.loads(matches[0].read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load replay %s: %s", matches[0], exc)
            return None

    def list_cases(self) -> list[str]:
        """List all case IDs with replay artifacts."""
        if not self.replay_dir.exists():
            return []

        cases = set()
        for f in self.replay_dir.glob("*.json"):
            # Extract case_id from filename: INC12345_20240212T103015Z.json
            parts = f.stem.rsplit("_", 1)
            if parts:
                cases.add(parts[0])
        return sorted(cases)

    def list_all(self) -> list[dict]:
        """List all artifacts with metadata (case_id, timestamp, receipt_count).

        Returns list sorted newest-first.
        """
        if not self.replay_dir.exists():
            return []

        entries = []
        for f in sorted(self.replay_dir.glob("*.json"), reverse=True):
            try:
                artifact = json.loads(f.read_text())
                entries.append({
                    "case_id": artifact.get("case_id", ""),
                    "timestamp": artifact.get("timestamp", ""),
                    "receipt_count": len(artifact.get("receipts", [])),
                    "incident_type": artifact.get("incident_type", ""),
                    "confidence": artifact.get("result", {}).get("confidence"),
                    "path": str(f),
                })
            except (json.JSONDecodeError, OSError):
                continue
        return entries


def replay_investigation(case_id: str, replay_dir: str = DEFAULT_REPLAY_DIR) -> dict | None:
    """Replay a previously-stored investigation.

    Returns the stored result if the artifact exists, else None.
    """
    store = ReplayStore(replay_dir)
    artifact = store.load(case_id)
    if artifact is None:
        logger.info("No replay artifact found for %s", case_id)
        return None

    logger.info(
        "Replaying %s from artifact (timestamp=%s, receipts=%d)",
        case_id,
        artifact.get("timestamp", "unknown"),
        len(artifact.get("receipts", [])),
    )
    return artifact.get("result")


def replay_stepwise(
    case_id: str,
    replay_dir: str = DEFAULT_REPLAY_DIR,
    analyze_fn: Callable[[dict], dict] | None = None,
) -> Generator[ReplayStep, None, None]:
    """Walk through a past investigation one tool-call at a time.

    Yields a ReplayStep after each receipt is applied.  Evidence accumulates
    across steps exactly as it did during the original investigation.

    Args:
        case_id:    The investigation to replay.
        replay_dir: Directory containing replay artifacts.
        analyze_fn: Optional callable ``(evidence_snapshot) -> result_dict``.
                    When provided, each step includes a ``partial_result``
                    showing what the RCA would have concluded with only the
                    evidence collected so far.  Typically pass
                    ``supervisor._analyze_evidence_from_snapshot``.

    Yields:
        ReplayStep for each tool call in the stored receipt sequence.

    Raises:
        ValueError: If no artifact exists for the case_id.

    Example::

        for step in replay_stepwise("INC12345"):
            print(f"Step {step.step_num}/{step.total_steps}: "
                  f"{step.receipt['worker']}.{step.receipt['action']}")
            print(f"  Evidence keys so far: {list(step.evidence_snapshot)}")
            if step.partial_result:
                print(f"  Hypothesis at step: {step.partial_result.get('root_cause')}")
    """
    store = ReplayStore(replay_dir)
    artifact = store.load(case_id)
    if artifact is None:
        raise ValueError(f"No replay artifact found for {case_id!r}")

    receipts: list[dict] = artifact.get("receipts", [])
    stored_evidence: dict = artifact.get("evidence", {})
    total = len(receipts)

    logger.info(
        "Stepwise replay: %s — %d steps (timestamp=%s)",
        case_id, total, artifact.get("timestamp", "unknown"),
    )

    # Reconstruct evidence incrementally.
    # Each receipt stores the *action* name; its result is in stored_evidence
    # under the same key (this matches how _execute_playbook stores results).
    evidence_so_far: dict[str, Any] = {}

    for i, receipt in enumerate(receipts, start=1):
        action = receipt.get("action", "")
        worker = receipt.get("worker", receipt.get("tool", ""))

        # Rehydrate this step's result from the stored evidence snapshot.
        # Prefer keyed by action; fall back to worker.action for namespaced keys.
        if action in stored_evidence:
            evidence_so_far[action] = stored_evidence[action]
        elif f"{worker}.{action}" in stored_evidence:
            evidence_so_far[f"{worker}.{action}"] = stored_evidence[f"{worker}.{action}"]
        # If the evidence key can't be found, the snapshot still advances
        # (the receipt is included; evidence_so_far just won't have that key).

        partial: dict | None = None
        if analyze_fn is not None:
            try:
                partial = analyze_fn(dict(evidence_so_far))
            except Exception as exc:
                logger.debug("analyze_fn failed at step %d for %s: %s", i, case_id, exc)

        yield ReplayStep(
            step_num=i,
            total_steps=total,
            receipt=dict(receipt),
            evidence_snapshot=dict(evidence_so_far),
            partial_result=partial,
        )
