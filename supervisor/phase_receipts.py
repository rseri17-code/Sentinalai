"""PhaseReceiptCollector — lightweight timing + status recorder for phases.

Behavior-neutral by design:

- The collector is instantiated inside ``investigate()`` per call.
- Each phase call is wrapped in ``with collector.record(phase_name) as r:``.
- On normal exit, the collector appends a ``PhaseExecutionReceipt`` derived
  from the monotonic clock plus whatever the caller set on ``r``.
- On exception, the collector captures ``error_type`` and RE-RAISES —
  never masks the original phase failure.
- Any error INSIDE the receipt-recording path (unlikely — everything is
  pure stdlib) is swallowed so an investigation never fails because of
  receipt bookkeeping.

Status derivation is purely mechanical, driven by ``status_from_result`` —
no phase output is semantically interpreted. Callers pass the phase's
``PhaseResult`` (or its ``.status`` enum) and the mapping table returns
one of the canonical status strings.

Attach point contract: callers append ``collector.to_list()`` to a result
dict under the internal key ``_phase_receipts``. See ``attach_receipts``.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from sentinel_core.models.phase_receipt import (
    PhaseExecutionReceipt,
    STATUS_DEGRADED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    normalize_status,
)
from sentinel_core.models.workflow import PhaseStatus

logger = logging.getLogger("sentinalai.phase_receipts")


# Internal dict key under which receipts attach to the result. Kept in the
# private-attributes namespace (matches _evidence_snapshot, _llm_metrics, ...).
RECEIPTS_RESULT_KEY = "_phase_receipts"


# ---------------------------------------------------------------------------
# Status mapping (mechanical only — no semantic interpretation)
# ---------------------------------------------------------------------------

_STATUS_MAP: dict[PhaseStatus, str] = {
    PhaseStatus.PENDING:   STATUS_SKIPPED,
    PhaseStatus.RUNNING:   STATUS_SUCCESS,
    PhaseStatus.COMPLETED: STATUS_SUCCESS,
    PhaseStatus.FAILED:    STATUS_FAILED,
    PhaseStatus.SKIPPED:   STATUS_SKIPPED,
}


def map_phase_status(status: PhaseStatus) -> str:
    """Map a workflow PhaseStatus enum to the receipt vocabulary."""
    return _STATUS_MAP.get(status, STATUS_SUCCESS)


def status_from_result(phase_result: Any) -> str:
    """Extract a receipt-status string from an arbitrary phase-return object.

    Tolerant of shape: works on ``PhaseResult`` (has .status), on plain
    ``PhaseStatus`` enum values, and returns SUCCESS if the input is None
    or has neither. Never raises.
    """
    if phase_result is None:
        return STATUS_SUCCESS
    try:
        # PhaseResult instance
        status = getattr(phase_result, "status", None)
        if status is None:
            return STATUS_SUCCESS
        if isinstance(status, PhaseStatus):
            return map_phase_status(status)
        # PhaseStatus given directly
        if isinstance(phase_result, PhaseStatus):
            return map_phase_status(phase_result)
        # String / raw value
        return normalize_status(str(status))
    except Exception:
        return STATUS_SUCCESS


# ---------------------------------------------------------------------------
# Mutable per-phase recording (surfaced via the context manager)
# ---------------------------------------------------------------------------

class _PhaseRecording:
    """Mutable handle yielded by ``PhaseReceiptCollector.record()``.

    Callers set ``.status``, ``.evidence_after``, ``.warnings``,
    ``.degraded_reason``, and ``.metadata`` between context-manager
    ``__enter__`` and ``__exit__``. Only the last-set values are recorded.
    """
    __slots__ = (
        "phase_name", "started_at", "evidence_before", "evidence_after",
        "status", "warnings", "degraded_reason", "error_type", "metadata",
    )

    def __init__(self, phase_name: str, started_at: float, evidence_before: int) -> None:
        self.phase_name        = phase_name
        self.started_at        = started_at
        self.evidence_before   = evidence_before
        self.evidence_after    = evidence_before
        self.status            = STATUS_SUCCESS
        self.warnings: list[str] = []
        self.degraded_reason   = ""
        self.error_type        = ""
        self.metadata: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class PhaseReceiptCollector:
    """Per-investigation receipt collector. Not thread-safe by design (one
    investigation runs on one logical caller; parallel phase execution is
    a future concern).
    """

    def __init__(self) -> None:
        self._receipts: list[PhaseExecutionReceipt] = []

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    @contextmanager
    def record(
        self,
        phase_name: str,
        evidence_before: int = 0,
    ) -> Iterator[_PhaseRecording]:
        """Context manager that times a phase call and records a receipt.

        On normal exit, the caller's mutations to the yielded recording
        are preserved. On exception, the receipt records
        ``status=failed`` + ``error_type=<ExceptionClass>`` and the
        exception is re-raised.
        """
        started_at = _now()
        rec = _PhaseRecording(phase_name, started_at, evidence_before)
        try:
            yield rec
        except Exception as exc:
            rec.status = STATUS_FAILED
            rec.error_type = type(exc).__name__
            self._finalize(rec)
            raise
        # Normal exit path
        self._finalize(rec)

    def _finalize(self, rec: _PhaseRecording) -> None:
        """Append the finished receipt. Swallows internal errors."""
        try:
            completed_at = _now()
            elapsed_ms = max(0.0, (completed_at - rec.started_at) * 1000.0)
            self._receipts.append(PhaseExecutionReceipt(
                phase_name            = rec.phase_name,
                status                = normalize_status(rec.status),
                started_at            = rec.started_at,
                completed_at          = completed_at,
                elapsed_ms            = elapsed_ms,
                evidence_count_before = int(rec.evidence_before or 0),
                evidence_count_after  = int(rec.evidence_after  or 0),
                warnings              = tuple(str(w) for w in (rec.warnings or ())),
                degraded_reason       = str(rec.degraded_reason or ""),
                error_type            = str(rec.error_type or ""),
                metadata              = dict(rec.metadata or {}),
            ))
        except Exception as inner:
            # Never fail an investigation because of a receipt-record error.
            logger.debug("phase-receipt finalize failed: %s", inner)

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------

    def receipts(self) -> list[PhaseExecutionReceipt]:
        return list(self._receipts)

    def to_list(self) -> list[dict[str, Any]]:
        """Return receipts as JSON-serializable dicts, in execution order."""
        out: list[dict[str, Any]] = []
        for r in self._receipts:
            try:
                out.append(r.to_dict())
            except Exception:
                # A single malformed receipt must not poison the whole list.
                pass
        return out

    def __len__(self) -> int:
        return len(self._receipts)


# ---------------------------------------------------------------------------
# Attach helper
# ---------------------------------------------------------------------------

def attach_receipts(
    result: Optional[dict[str, Any]],
    collector: Optional[PhaseReceiptCollector],
) -> Optional[dict[str, Any]]:
    """Attach the collector's receipts to ``result[_phase_receipts]``.

    Returns the same ``result`` reference for convenient chained returns.
    Silently no-ops if ``result`` is not a mutable dict or the collector
    is None. Never raises during normal use.
    """
    if not isinstance(result, dict) or collector is None:
        return result
    try:
        result[RECEIPTS_RESULT_KEY] = collector.to_list()
    except Exception as exc:
        logger.debug("attach_receipts failed (non-critical): %s", exc)
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> float:
    """Wallclock via time.monotonic. Isolated for test-monkeypatching."""
    return time.monotonic()


__all__ = [
    "PhaseReceiptCollector",
    "attach_receipts",
    "map_phase_status",
    "status_from_result",
    "RECEIPTS_RESULT_KEY",
    "STATUS_SUCCESS", "STATUS_DEGRADED", "STATUS_SKIPPED", "STATUS_FAILED",
]
