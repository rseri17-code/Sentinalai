"""Replay system for SentinalAI investigations.

Persists investigation receipts + outputs as replay artifacts.
Replay mode rehydrates from stored receipts without making external calls.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_REPLAY_DIR = os.getenv("SENTINALAI_REPLAY_DIR", "/tmp/sentinalai_replays")


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
        return str(path)

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
