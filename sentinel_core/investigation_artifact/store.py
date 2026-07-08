"""ArtifactStore — append-only persistence for InvestigationArtifacts.

Layout (state directories, RC-E discipline throughout):

    {root}/candidate/{artifact_id}.json
    {root}/admitted/{artifact_id}.json
    {root}/quarantined/{artifact_id}.json
    {root}/rejected/{artifact_id}.json
    {root}/events/admission_audit.jsonl     (append-only)

Rules:
- Artifact files are never modified after write. No overwrite, no delete.
- ``save_candidate`` is idempotent: the artifact is content-addressed, so
  a second save of the same artifact_id is a no-op (same bytes).
- State transitions are file MOVES between state directories plus one
  appended audit event. "validated" is an audit event on an admitted
  artifact — no move, bytes untouched.
- Writes are atomic: unique tmp file (pid+uuid) + ``Path.replace`` —
  the concurrent-writer pattern from ReplayStore (RC-E).
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterator

from sentinel_core.investigation_artifact.schemas import (
    ADMISSION_STATES,
    InvestigationArtifact,
)
from sentinel_core.investigation_artifact.serialization import (
    artifact_from_dict,
    artifact_to_dict,
)

# Directory-backed states. "validated" is event-only (artifact stays in
# admitted/), so it has no directory of its own.
_DIR_STATES: tuple[str, ...] = (
    "candidate", "admitted", "quarantined", "rejected",
)
_EVENTS_DIR = "events"
_AUDIT_FILE = "admission_audit.jsonl"


class ArtifactStoreError(RuntimeError):
    """Raised on invalid store operations (bad ids, bad states)."""


class ArtifactStore:
    """Append-only, state-directory artifact store."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def save_candidate(self, artifact: InvestigationArtifact) -> Path:
        """Persist a new artifact into ``candidate/``.

        Idempotent on artifact_id: artifacts are content-addressed, so an
        existing file with the same id is the same content — return its
        path without touching it (append-only: never overwrite).
        """
        aid = self._check_id(artifact.artifact_id)
        existing = self._find(aid)
        if existing is not None:
            return existing[1]
        path = self._dir("candidate") / f"{aid}.json"
        payload = json.dumps(
            artifact_to_dict(artifact), sort_keys=True, indent=2,
        )
        self._atomic_write(path, payload)
        return path

    def transition(
        self, artifact_id: str, to_state: str,
        reasons: tuple[str, ...] = (), at: str = "",
    ) -> None:
        """Move an artifact between admission states + append audit event.

        ``validated`` is special: audit-event only — the artifact must
        already be in ``admitted/`` and its file is not moved.
        """
        aid = self._check_id(artifact_id)
        if to_state not in ADMISSION_STATES:
            raise ArtifactStoreError(f"unknown admission state: {to_state!r}")
        found = self._find(aid)
        if found is None:
            raise ArtifactStoreError(f"artifact not found: {aid!r}")
        from_state, path = found

        if to_state == "validated":
            if from_state != "admitted":
                raise ArtifactStoreError(
                    "validated requires the artifact to be admitted "
                    f"(currently {from_state!r})"
                )
        elif to_state != from_state:
            target = self._dir(to_state) / path.name
            if target.exists():
                raise ArtifactStoreError(
                    f"target already exists (append-only): {target}"
                )
            path.replace(target)

        self._append_audit({
            "artifact_id": aid,
            "from": from_state,
            "to": to_state,
            "reasons": list(reasons),
            "at": str(at),
        })

    def record_decision(
        self, artifact_id: str, decided_state: str,
        reasons: tuple[str, ...] = (), at: str = "",
    ) -> None:
        """Append a classification decision WITHOUT moving the artifact.

        Wave 1 candidate-only mode: the admission controller classifies
        and the decision is recorded for audit, but no state transition
        is driven by the runtime.
        """
        aid = self._check_id(artifact_id)
        self._append_audit({
            "artifact_id": aid,
            "from": self.state_of(aid) or "candidate",
            "to": f"decision:{decided_state}",
            "reasons": list(reasons),
            "at": str(at),
        })

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def load(self, artifact_id: str) -> InvestigationArtifact:
        aid = self._check_id(artifact_id)
        found = self._find(aid)
        if found is None:
            raise ArtifactStoreError(f"artifact not found: {aid!r}")
        data = json.loads(found[1].read_text())
        return artifact_from_dict(data)

    def state_of(self, artifact_id: str) -> str | None:
        """Directory-derived state, upgraded to 'validated' when an
        admitted artifact carries a validated audit event."""
        aid = self._check_id(artifact_id)
        found = self._find(aid)
        if found is None:
            return None
        state = found[0]
        if state == "admitted":
            for event in self.audit_events():
                if event.get("artifact_id") == aid \
                        and event.get("to") == "validated":
                    return "validated"
        return state

    def list_ids(self, state: str = "candidate") -> list[str]:
        if state not in _DIR_STATES:
            raise ArtifactStoreError(f"not a directory state: {state!r}")
        d = self._root / state
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.json"))

    def audit_events(self) -> Iterator[dict[str, Any]]:
        audit = self._root / _EVENTS_DIR / _AUDIT_FILE
        if not audit.exists():
            return
        for line in audit.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _dir(self, state: str) -> Path:
        d = self._root / state
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _find(self, artifact_id: str) -> tuple[str, Path] | None:
        for state in _DIR_STATES:
            p = self._root / state / f"{artifact_id}.json"
            if p.exists():
                return state, p
        return None

    def _append_audit(self, event: dict[str, Any]) -> None:
        d = self._root / _EVENTS_DIR
        d.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, sort_keys=True)
        with open(d / _AUDIT_FILE, "a") as f:
            f.write(line + "\n")

    def _atomic_write(self, path: Path, payload: str) -> None:
        tmp = path.with_suffix(f".{os.getpid()}-{uuid.uuid4().hex}.tmp")
        tmp.write_text(payload)
        tmp.replace(path)

    @staticmethod
    def _check_id(artifact_id: str) -> str:
        aid = str(artifact_id)
        if not aid or "/" in aid or "\\" in aid or "." in aid:
            raise ArtifactStoreError(f"invalid artifact_id: {artifact_id!r}")
        return aid


__all__ = ["ArtifactStore", "ArtifactStoreError"]
