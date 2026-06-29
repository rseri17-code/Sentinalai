"""Shadow-mode runtime for the typed EvidenceLedger.

Phase 9 wiring. When ``EVIDENCE_LEDGER_SHADOW_ENABLED`` is OFF (the default),
``ShadowMirror.create()`` returns ``None`` — callers should short-circuit on
``None`` so the hot path remains a single ``is not None`` check per write.

When ON, a real ShadowMirror is constructed, holds an ``EvidenceLedger``
alongside the legacy dict, mirrors writes via ``set()``, and produces a
``ParityReport`` when ``parity(evidence_dict)`` is called at the end of a
phase. The mirror NEVER influences decisions, output, persistence, replay,
or worker calls — it only observes.

Hot-path contract (used inside the supervisor agent):

    _shadow = ShadowMirror.create()           # None when flag off
    evidence[label] = result
    if _shadow is not None:
        _shadow.set(label, result)
    ...
    if _shadow is not None:
        _shadow.parity_log(evidence, context="_execute_playbook")

Zero observable behavior change when the flag is off (verified by tests).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from sentinel_core.evidence.shadow import is_shadow_enabled

logger = logging.getLogger("sentinalai.evidence_shadow")


@dataclass
class ParityReport:
    """Result of comparing a legacy evidence dict against a shadow ledger."""
    ok: bool
    evidence_keys: int
    ledger_keys: int
    missing_in_ledger: list[str] = field(default_factory=list)
    extra_in_ledger: list[str] = field(default_factory=list)
    value_mismatches: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return f"parity OK ({self.evidence_keys} keys)"
        parts = []
        if self.missing_in_ledger:
            parts.append(f"missing_in_ledger={self.missing_in_ledger!r}")
        if self.extra_in_ledger:
            parts.append(f"extra_in_ledger={self.extra_in_ledger!r}")
        if self.value_mismatches:
            parts.append(f"value_mismatches={self.value_mismatches!r}")
        return (
            f"parity MISMATCH (evidence={self.evidence_keys} keys, "
            f"ledger={self.ledger_keys} keys; "
            + "; ".join(parts) + ")"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok":                self.ok,
            "evidence_keys":     self.evidence_keys,
            "ledger_keys":       self.ledger_keys,
            "missing_in_ledger": list(self.missing_in_ledger),
            "extra_in_ledger":   list(self.extra_in_ledger),
            "value_mismatches":  list(self.value_mismatches),
        }


class ShadowMirror:
    """Observational ledger that mirrors writes to the legacy evidence dict.

    Construct ONLY via ``ShadowMirror.create()`` — that factory returns
    ``None`` when the shadow flag is off so callers can short-circuit cheaply.
    """

    __slots__ = ("_ledger",)

    def __init__(self) -> None:
        # Deferred import: this module loads even when the ledger module is
        # never used, and tests that disable the flag should not pay the
        # import cost. Construction only happens when create() succeeds.
        from sentinel_core.evidence import EvidenceLedger
        self._ledger: EvidenceLedger = EvidenceLedger()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def create(cls) -> Optional["ShadowMirror"]:
        """Return a new ShadowMirror if the flag is enabled, else None."""
        if not is_shadow_enabled():
            return None
        return cls()

    # ------------------------------------------------------------------
    # Mirror API (called from the hot path)
    # ------------------------------------------------------------------

    def set(self, key: str, value: Any) -> None:
        """Mirror a single ``evidence[key] = value`` write."""
        # Import here too keeps the symbol scope tight and avoids any
        # top-level cyclic-import surprise in the agent module.
        from sentinel_core.evidence import EvidenceSource
        from sentinel_core.evidence.adapter import (
            infer_kind_for_key,
            infer_source_for_key,
        )
        self._ledger.add(
            key, value,
            source=infer_source_for_key(key),
            kind=infer_kind_for_key(key),
        )

    # ------------------------------------------------------------------
    # Parity check
    # ------------------------------------------------------------------

    def parity(self, evidence: dict[str, Any]) -> ParityReport:
        """Compare the legacy evidence dict against the shadow ledger.

        Pure function — does not mutate either side. Returns a ParityReport
        the caller can inspect or log.
        """
        ledger_dict = self._ledger.to_dict()
        ev_keys = set(evidence.keys())
        lg_keys = set(ledger_dict.keys())

        missing_in_ledger = sorted(ev_keys - lg_keys)
        extra_in_ledger   = sorted(lg_keys - ev_keys)
        value_mismatches  = sorted(
            k for k in (ev_keys & lg_keys) if evidence[k] != ledger_dict[k]
        )

        ok = not (missing_in_ledger or extra_in_ledger or value_mismatches)
        return ParityReport(
            ok=ok,
            evidence_keys=len(ev_keys),
            ledger_keys=len(lg_keys),
            missing_in_ledger=missing_in_ledger,
            extra_in_ledger=extra_in_ledger,
            value_mismatches=value_mismatches,
        )

    def parity_log(self, evidence: dict[str, Any], context: str = "") -> ParityReport:
        """Run parity() and log the result. Never raises.

        ``context`` is a short tag — e.g. the calling function name — used to
        annotate the log line so multiple call sites are distinguishable.
        """
        report = self.parity(evidence)
        prefix = f"[shadow_parity:{context}] " if context else "[shadow_parity] "
        if report.ok:
            logger.debug(prefix + report.summary())
        else:
            logger.warning(prefix + report.summary())
        return report

    # ------------------------------------------------------------------
    # Introspection (test surface)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return the shadow ledger's current state as a plain dict."""
        return self._ledger.to_dict()

    def keys(self) -> list[str]:
        return self._ledger.keys()

    def __len__(self) -> int:
        return len(self._ledger)


__all__ = ["ShadowMirror", "ParityReport"]
