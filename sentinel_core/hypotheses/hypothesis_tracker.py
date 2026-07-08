"""HypothesisTracker — build up a HypothesisGraph deterministically.

The tracker holds mutable working state during construction; once
:meth:`build_graph` is called the immutable :class:`HypothesisGraph` is
returned. The tracker never touches production runtime.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from sentinel_core.hypotheses.hypothesis_graph import HypothesisGraph
from sentinel_core.hypotheses.schemas import (
    Hypothesis,
    HypothesisEvidence,
    HypothesisStatus,
    HypothesisTransition,
)


class HypothesisTracker:
    """Builder for a :class:`HypothesisGraph`.

    Not thread-safe by design — one investigation → one tracker.
    """

    def __init__(self, investigation_id: str = "", started_at: str = "") -> None:
        self._investigation_id = str(investigation_id)
        self._started_at = str(started_at)
        self._completed_at = ""
        self._by_id: dict[str, Hypothesis] = {}

    # ------------------------------------------------------------------
    # Hypothesis lifecycle
    # ------------------------------------------------------------------

    def propose(self, name: str, description: str = "",
                 initial_confidence: int = 50) -> Hypothesis:
        """Create or refine a proposed hypothesis. Deterministic upsert
        per name.

        RC-J: previously first-write-wins — a second call with the same
        name silently returned the original Hypothesis and dropped any
        refined description or confidence supplied by the caller. The
        new merge policy is monotone-informative:

        - **description**: if a non-empty new description is supplied
          and the stored description is empty (or strictly shorter),
          replace it. Otherwise keep the stored value.
        - **confidence**: keep ``max(stored, new)`` — a re-propose can
          only strengthen conviction, never weaken it.

        Both rules are pure functions of the two Hypotheses, so
        determinism holds regardless of caller order.
        """
        h = Hypothesis.make(
            name=name,
            description=description,
            status=HypothesisStatus.PROPOSED.value,
            confidence=_clamp(initial_confidence),
        )
        stored = self._by_id.get(h.hypothesis_id)
        if stored is None:
            self._by_id[h.hypothesis_id] = h
            return h
        # Refinement merge — never silently discards new information.
        new_desc = h.description
        keep_desc = stored.description
        if new_desc and (not keep_desc or len(new_desc) > len(keep_desc)):
            keep_desc = new_desc
        keep_conf = max(int(stored.confidence), int(h.confidence))
        merged = replace(stored, description=keep_desc, confidence=keep_conf)
        self._by_id[h.hypothesis_id] = merged
        return merged

    def add_supporting_evidence(
        self, hypothesis_id: str, key: str,
        weight: float = 1.0, reason: str = "", added_at: str = "",
    ) -> Hypothesis:
        return self._add_evidence(
            hypothesis_id, key, supports=True,
            weight=weight, reason=reason, added_at=added_at,
        )

    def add_refuting_evidence(
        self, hypothesis_id: str, key: str,
        weight: float = 1.0, reason: str = "", added_at: str = "",
    ) -> Hypothesis:
        return self._add_evidence(
            hypothesis_id, key, supports=False,
            weight=weight, reason=reason, added_at=added_at,
        )

    def transition(
        self, hypothesis_id: str, to_status: str | HypothesisStatus,
        at: str = "", reason: str = "",
        new_confidence: int | None = None,
    ) -> Hypothesis:
        h = self._must(hypothesis_id)
        to_str = to_status.value if isinstance(to_status, HypothesisStatus) else str(to_status)
        confidence_before = h.confidence
        confidence_after = (
            _clamp(new_confidence) if new_confidence is not None else confidence_before
        )
        trans = HypothesisTransition(
            at=str(at),
            from_status=h.status,
            to_status=to_str,
            confidence_before=confidence_before,
            confidence_after=confidence_after,
            reason=str(reason),
        )
        updated = replace(
            h,
            status=to_str,
            confidence=confidence_after,
            transitions=h.transitions + (trans,),
        )
        self._by_id[hypothesis_id] = updated
        return updated

    def rule_out(
        self, hypothesis_id: str, reason: str, at: str = "",
    ) -> Hypothesis:
        updated = self.transition(
            hypothesis_id, HypothesisStatus.RULED_OUT,
            at=at, reason=reason, new_confidence=0,
        )
        updated = replace(updated, ruled_out_reason=str(reason))
        self._by_id[hypothesis_id] = updated
        return updated

    def confirm(
        self, hypothesis_id: str, root_cause: str,
        reason: str = "", at: str = "",
        confidence: int = 100, mtti_contribution_ms: int = 0,
    ) -> Hypothesis:
        updated = self.transition(
            hypothesis_id, HypothesisStatus.CONFIRMED,
            at=at, reason=reason, new_confidence=confidence,
        )
        updated = replace(
            updated,
            root_cause=str(root_cause),
            confirmed_reason=str(reason),
            mtti_contribution_ms=int(mtti_contribution_ms),
        )
        self._by_id[hypothesis_id] = updated
        return updated

    # ------------------------------------------------------------------
    # Finalise
    # ------------------------------------------------------------------

    def finalise(self, completed_at: str = "") -> None:
        self._completed_at = str(completed_at)

    def build_graph(self) -> HypothesisGraph:
        return HypothesisGraph(
            investigation_id=self._investigation_id,
            hypotheses=tuple(sorted(
                self._by_id.values(), key=lambda h: h.hypothesis_id,
            )),
            started_at=self._started_at,
            completed_at=self._completed_at,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _must(self, hypothesis_id: str) -> Hypothesis:
        if hypothesis_id not in self._by_id:
            raise KeyError(f"unknown hypothesis_id: {hypothesis_id!r}")
        return self._by_id[hypothesis_id]

    def _add_evidence(
        self, hypothesis_id: str, key: str, *,
        supports: bool, weight: float, reason: str, added_at: str,
    ) -> Hypothesis:
        h = self._must(hypothesis_id)
        ev = HypothesisEvidence(
            key=str(key), supports=bool(supports),
            weight=float(max(0.0, min(1.0, weight))),
            reason=str(reason), added_at=str(added_at),
        )
        if supports:
            updated = replace(h, supporting_evidence=h.supporting_evidence + (ev,))
        else:
            updated = replace(h, refuting_evidence=h.refuting_evidence + (ev,))
        self._by_id[hypothesis_id] = updated
        return updated


def _clamp(v: Any) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, n))


__all__ = ["HypothesisTracker"]
