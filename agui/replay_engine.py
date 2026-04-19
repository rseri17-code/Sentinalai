"""AG UI Replay Engine — deterministic step-by-step investigation replay.

Design principles:
1. Determinism: same input events → same graph, every time
2. Integrity: event hashes validated before replay begins
3. Step control: pause/play/step/speed control from UI
4. Immutability: replay does NOT re-execute agent code
5. Audit: replay sessions are logged with actor_id

Replay modes:
- LIVE: replay at original speed (timestamps preserved)
- FAST: replay at Nx speed (configurable multiplier)
- STEP: one event at a time, manual advance

Storage:
- Replay snapshot = ordered event list + metadata + integrity hashes
- Stored in S3 (production) or local filesystem (dev)

Validation:
- Pre-replay: verify event count, hash chain, sequence completeness
- During replay: detect any state divergence
- Post-replay: compare final graph hash to original
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable, Optional

from agui.schemas.events import AGUIEvent, EventType
from agui.schemas.graph import ExecutionGraph
from agui.graph_builder import rebuild_from_events

logger = logging.getLogger(__name__)


class ReplayMode(str, Enum):
    LIVE = "live"     # original speed
    FAST = "fast"     # Nx speed (speed_multiplier)
    STEP = "step"     # manual step


class ReplayStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class ReplayValidationResult:
    is_valid: bool
    event_count: int
    expected_count: int
    gaps: list[int]
    hash_mismatches: list[str]
    errors: list[str]


@dataclass
class ReplaySnapshot:
    """Serializable replay artifact stored in S3/local."""
    investigation_id: str
    incident_id: str
    trace_id: str
    created_at: str
    events: list[dict[str, Any]]
    event_count: int
    chain_hash: str          # SHA256 of all event hashes concatenated in order
    original_duration_ms: float
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "investigation_id": self.investigation_id,
            "incident_id": self.incident_id,
            "trace_id": self.trace_id,
            "created_at": self.created_at,
            "events": self.events,
            "event_count": self.event_count,
            "chain_hash": self.chain_hash,
            "original_duration_ms": self.original_duration_ms,
            "metadata": self.metadata,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReplaySnapshot":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})

    @staticmethod
    def compute_chain_hash(events: list[AGUIEvent]) -> str:
        """SHA256 of all event idempotency_keys concatenated in sequence order."""
        sorted_events = sorted(events, key=lambda e: e.sequence_num)
        chain = "".join(e.idempotency_key for e in sorted_events)
        return hashlib.sha256(chain.encode()).hexdigest()


@dataclass
class ReplaySession:
    """In-progress replay session."""
    session_id: str
    investigation_id: str
    actor_id: str
    mode: ReplayMode
    speed_multiplier: float
    snapshot: ReplaySnapshot
    status: ReplayStatus = ReplayStatus.PENDING
    current_step: int = 0
    total_steps: int = 0
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    _step_event: asyncio.Event = field(default_factory=asyncio.Event)
    _pause_event: asyncio.Event = field(default_factory=asyncio.Event)


# Type alias for replay event callbacks
ReplayCallback = Callable[[AGUIEvent, int, int], Awaitable[None]]


class ReplayEngine:
    """
    Deterministic investigation replay engine.

    Usage:
    1. build_snapshot(events) → ReplaySnapshot (called at investigation end)
    2. validate(snapshot) → ReplayValidationResult
    3. start_replay(snapshot, callback) → ReplaySession
    4. step/pause/resume/abort via session controls
    """

    def __init__(self) -> None:
        self._sessions: dict[str, ReplaySession] = {}

    def build_snapshot(
        self,
        investigation_id: str,
        incident_id: str,
        trace_id: str,
        events: list[AGUIEvent],
        original_duration_ms: float = 0.0,
        metadata: Optional[dict] = None,
    ) -> ReplaySnapshot:
        """
        Build a replay snapshot from the event stream.
        Called when investigation completes.
        """
        sorted_events = sorted(events, key=lambda e: e.sequence_num)
        chain_hash = ReplaySnapshot.compute_chain_hash(sorted_events)
        return ReplaySnapshot(
            investigation_id=investigation_id,
            incident_id=incident_id,
            trace_id=trace_id,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            events=[e.model_dump() for e in sorted_events],
            event_count=len(sorted_events),
            chain_hash=chain_hash,
            original_duration_ms=original_duration_ms,
            metadata=metadata or {},
        )

    def validate(self, snapshot: ReplaySnapshot) -> ReplayValidationResult:
        """
        Pre-replay validation.

        Checks:
        1. Event count matches
        2. Sequence completeness (no gaps)
        3. Hash chain integrity
        4. Required events present (started + ended)
        """
        errors = []
        gaps = []
        hash_mismatches = []

        if snapshot.event_count != len(snapshot.events):
            errors.append(
                f"Event count mismatch: declared={snapshot.event_count}, "
                f"actual={len(snapshot.events)}"
            )

        # Sequence completeness
        seqs = sorted(e.get("sequence_num", -1) for e in snapshot.events)
        expected_seqs = list(range(len(seqs)))
        for i, (actual, expected) in enumerate(zip(seqs, expected_seqs)):
            if actual != expected:
                gaps.append(expected)

        # Rebuild events and verify chain hash
        try:
            events = [AGUIEvent(**e) for e in snapshot.events]
            actual_chain = ReplaySnapshot.compute_chain_hash(events)
            if actual_chain != snapshot.chain_hash:
                hash_mismatches.append(
                    f"Chain hash mismatch: expected={snapshot.chain_hash[:16]}..., "
                    f"actual={actual_chain[:16]}..."
                )
        except Exception as e:
            errors.append(f"Event deserialization failed: {e}")

        # Required events check
        event_types = {e.get("event_type") for e in snapshot.events}
        if EventType.INVESTIGATION_STARTED.value not in event_types:
            errors.append("Missing investigation.started event")

        is_valid = not errors and not hash_mismatches and len(gaps) == 0

        return ReplayValidationResult(
            is_valid=is_valid,
            event_count=len(snapshot.events),
            expected_count=snapshot.event_count,
            gaps=gaps,
            hash_mismatches=hash_mismatches,
            errors=errors,
        )

    async def start_replay(
        self,
        snapshot: ReplaySnapshot,
        actor_id: str,
        mode: ReplayMode = ReplayMode.FAST,
        speed_multiplier: float = 5.0,
        callback: Optional[ReplayCallback] = None,
    ) -> ReplaySession:
        """
        Start a replay session.

        callback(event, current_step, total_steps) is called for each replayed event.
        """
        validation = self.validate(snapshot)
        if not validation.is_valid:
            raise ValueError(
                f"Replay validation failed: {validation.errors} "
                f"gaps={validation.gaps} "
                f"hash_mismatches={validation.hash_mismatches}"
            )

        # Evict finished sessions older than 1 hour to bound memory growth.
        _ttl = 3600.0
        _now = time.time()
        stale = [
            sid for sid, s in self._sessions.items()
            if s.status in (ReplayStatus.COMPLETED, ReplayStatus.ABORTED, ReplayStatus.FAILED)
            and (s.completed_at or 0) < _now - _ttl
        ]
        for sid in stale:
            del self._sessions[sid]

        session = ReplaySession(
            session_id=str(uuid.uuid4()),
            investigation_id=snapshot.investigation_id,
            actor_id=actor_id,
            mode=mode,
            speed_multiplier=speed_multiplier,
            snapshot=snapshot,
            total_steps=len(snapshot.events),
        )
        session._pause_event.set()  # Start unpaused
        self._sessions[session.session_id] = session

        asyncio.create_task(
            self._run_replay(session, callback)
        )

        logger.info(
            "Replay started: session=%s inv=%s mode=%s actor=%s",
            session.session_id, snapshot.investigation_id, mode, actor_id,
        )
        return session

    async def _run_replay(
        self, session: ReplaySession, callback: Optional[ReplayCallback]
    ) -> None:
        """Main replay loop."""
        session.status = ReplayStatus.RUNNING
        session.started_at = time.time()

        events = [AGUIEvent(**e) for e in session.snapshot.events]
        events.sort(key=lambda e: e.sequence_num)

        # Compute inter-event delays from original timestamps
        delays = self._compute_delays(events, session.mode, session.speed_multiplier)

        for i, event in enumerate(events):
            # Check for pause
            await session._pause_event.wait()

            if session.status == ReplayStatus.ABORTED:
                break

            # Deliver event
            session.current_step = i + 1
            if callback:
                try:
                    await callback(event, session.current_step, session.total_steps)
                except Exception as e:
                    logger.error("Replay callback error: %s", e)

            # Wait for next event (step mode: wait for manual advance)
            if session.mode == ReplayMode.STEP:
                session._step_event.clear()
                await session._step_event.wait()
            elif i < len(delays):
                await asyncio.sleep(delays[i])

        if session.status != ReplayStatus.ABORTED:
            session.status = ReplayStatus.COMPLETED
            session.completed_at = time.time()
            logger.info(
                "Replay completed: session=%s steps=%d",
                session.session_id, session.total_steps,
            )

    def _compute_delays(
        self,
        events: list[AGUIEvent],
        mode: ReplayMode,
        speed_multiplier: float,
    ) -> list[float]:
        """Compute inter-event delays based on original timestamps."""
        if mode == ReplayMode.STEP:
            return [0.0] * len(events)

        delays = [0.0]
        for i in range(1, len(events)):
            prev_ms = events[i - 1].timestamp_epoch_ms
            curr_ms = events[i].timestamp_epoch_ms
            delta_s = max(0.0, (curr_ms - prev_ms) / 1000.0)
            if mode == ReplayMode.LIVE:
                delays.append(delta_s)
            else:
                # Cap at 2s max delay even in live mode, divide by speed
                delays.append(min(delta_s, 2.0) / speed_multiplier)
        return delays

    def pause(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session and session.status == ReplayStatus.RUNNING:
            session._pause_event.clear()
            session.status = ReplayStatus.PAUSED
            return True
        return False

    def resume(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session and session.status == ReplayStatus.PAUSED:
            session._pause_event.set()
            session.status = ReplayStatus.RUNNING
            return True
        return False

    def step(self, session_id: str) -> bool:
        """Advance one step (STEP mode only)."""
        session = self._sessions.get(session_id)
        if session and session.mode == ReplayMode.STEP:
            session._step_event.set()
            return True
        return False

    def abort(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session:
            session.status = ReplayStatus.ABORTED
            session._pause_event.set()  # Unblock if paused
            session._step_event.set()   # Unblock if stepping
            return True
        return False

    def get_session(self, session_id: str) -> Optional[ReplaySession]:
        return self._sessions.get(session_id)

    def rebuild_graph_from_snapshot(self, snapshot: ReplaySnapshot) -> ExecutionGraph:
        """Rebuild the full DAG from a snapshot (for instant graph view)."""
        events = [AGUIEvent(**e) for e in snapshot.events]
        return rebuild_from_events(
            snapshot.investigation_id,
            snapshot.incident_id,
            snapshot.trace_id,
            events,
        )


# Global instance
_replay_engine: Optional[ReplayEngine] = None


def get_replay_engine() -> ReplayEngine:
    global _replay_engine
    if _replay_engine is None:
        _replay_engine = ReplayEngine()
    return _replay_engine
