"""Admission controller — classifies artifacts for the learning corpus.

Wave 1 mode: **classify only**. The controller returns a decision; the
runtime records it as an audit event but performs NO state transition
(candidate-only persistence). Fail-closed: an artifact is admitted only
when every hard gate passes and no soft gate fires — any doubt lands in
quarantine, never in admitted.

Pure: no I/O, no env, no clock. Retroactive signals (operator rejection,
replay regression, benchmark disagreement) arrive as an explicit
``signals`` mapping from offline pipelines.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sentinel_core.investigation_artifact.schemas import InvestigationArtifact

# Hard-gate thresholds.
MIN_EVIDENCE_KEYS = 2
# Soft-gate thresholds.
MIN_CONFIDENCE = 40


@dataclass(frozen=True)
class AdmissionDecision:
    """Outcome of classifying one artifact."""
    state:   str                      # admitted | quarantined | rejected
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "reasons": list(self.reasons)}


class AdmissionController:
    """Stateless classifier over InvestigationArtifacts."""

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def classify(
        self,
        artifact: InvestigationArtifact,
        signals: Mapping[str, Any] | None = None,
    ) -> AdmissionDecision:
        """Classify an artifact. Hard rejects first, then quarantine,
        then admitted. First hard hit wins; soft reasons accumulate."""
        sig = dict(signals or {})

        hard = self._hard_reject_reasons(artifact, sig)
        if hard:
            return AdmissionDecision(state="rejected", reasons=tuple(hard))

        soft = self._quarantine_reasons(artifact, sig)
        if soft:
            return AdmissionDecision(state="quarantined", reasons=tuple(soft))

        return AdmissionDecision(state="admitted", reasons=())

    # ------------------------------------------------------------------
    # Hard gates (R1-R7) — any hit rejects
    # ------------------------------------------------------------------

    def _hard_reject_reasons(
        self, a: InvestigationArtifact, sig: Mapping[str, Any],
    ) -> list[str]:
        reasons: list[str] = []
        if a.status == "meta_query":
            reasons.append("R1:meta_query")
        if a.status == "early_return":
            reasons.append("R2:early_return")
        if bool(sig.get("circuit_broken")) or \
                bool(a.final_result_summary.get("circuit_broken")):
            reasons.append("R3:circuit_broken")
        if a.status == "blocked":
            reasons.append("R4:evidence_gate_block")
        if not a.root_cause:
            reasons.append("R5:missing_root_cause")
        if int(a.evidence_key_summary.get("count", 0)) < MIN_EVIDENCE_KEYS:
            reasons.append("R6:insufficient_evidence")
        if a.status == "failed":
            reasons.append("R7:failed_phase")
        return reasons

    # ------------------------------------------------------------------
    # Soft gates (quarantine) — any hit quarantines
    # ------------------------------------------------------------------

    def _quarantine_reasons(
        self, a: InvestigationArtifact, sig: Mapping[str, Any],
    ) -> list[str]:
        reasons: list[str] = []
        if a.confidence < MIN_CONFIDENCE:
            reasons.append("Q1:low_confidence")
        if not a.decision_summary:
            reasons.append("Q2:missing_decision_summary")
        if not a.replay_pointer:
            reasons.append("Q3:missing_replay_pointer")
        if not a.benchmark_pointer:
            reasons.append("Q4:missing_benchmark_pointer")
        if bool(sig.get("operator_rejected")):
            reasons.append("Q5:operator_rejected")
        if bool(sig.get("replay_regression")):
            reasons.append("Q6:replay_regression")
        if bool(sig.get("benchmark_disagreement")):
            reasons.append("Q7:benchmark_disagreement")
        return reasons


__all__ = ["AdmissionController", "AdmissionDecision",
           "MIN_CONFIDENCE", "MIN_EVIDENCE_KEYS"]
