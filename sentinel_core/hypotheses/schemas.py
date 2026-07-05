"""Hypothesis Intelligence canonical schemas."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


HYPOTHESIS_SCHEMA_VERSION = 1


class HypothesisStatus(str, Enum):
    PROPOSED    = "proposed"
    SUPPORTED   = "supported"
    REFUTED     = "refuted"
    RULED_OUT   = "ruled_out"
    CONFIRMED   = "confirmed"


def make_hypothesis_id(name: str) -> str:
    return "hyp:" + hashlib.sha256(f"{name}".encode()).hexdigest()[:12]


@dataclass(frozen=True)
class HypothesisEvidence:
    """A piece of evidence supporting or refuting a hypothesis."""
    key:        str
    supports:   bool = True             # False → refutes
    weight:     float = 1.0             # 0-1
    reason:     str = ""                # short explanation
    added_at:   str = ""                # caller-supplied ISO 8601


@dataclass(frozen=True)
class HypothesisTransition:
    """Transition record for a hypothesis lifecycle event."""
    at:                 str
    from_status:        str
    to_status:          str
    confidence_before:  int = 0
    confidence_after:   int = 0
    reason:             str = ""


@dataclass(frozen=True)
class Hypothesis:
    """One considered hypothesis + its evidence + status trail."""
    hypothesis_id:      str
    name:               str
    description:        str = ""
    status:             str = HypothesisStatus.PROPOSED.value
    confidence:         int = 50
    supporting_evidence: tuple[HypothesisEvidence, ...] = ()
    refuting_evidence:   tuple[HypothesisEvidence, ...] = ()
    transitions:        tuple[HypothesisTransition, ...] = ()
    ruled_out_reason:   str = ""        # populated when status=RULED_OUT
    confirmed_reason:   str = ""        # populated when status=CONFIRMED
    root_cause:         str = ""        # populated when status=CONFIRMED
    mtti_contribution_ms: int = 0
    schema_version:     int = HYPOTHESIS_SCHEMA_VERSION

    @classmethod
    def make(cls, name: str, description: str = "", **kwargs: Any) -> "Hypothesis":
        return cls(
            hypothesis_id=make_hypothesis_id(name),
            name=name,
            description=description,
            **kwargs,
        )

    def to_dict(self) -> dict[str, Any]:
        return _tuples_to_lists(asdict(self))

    def is_terminal(self) -> bool:
        return self.status in (HypothesisStatus.CONFIRMED.value,
                                HypothesisStatus.RULED_OUT.value)


def _tuples_to_lists(obj: Any) -> Any:
    if isinstance(obj, tuple):
        return [_tuples_to_lists(v) for v in obj]
    if isinstance(obj, list):
        return [_tuples_to_lists(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _tuples_to_lists(v) for k, v in obj.items()}
    return obj


__all__ = [
    "HYPOTHESIS_SCHEMA_VERSION",
    "HypothesisStatus",
    "HypothesisEvidence",
    "HypothesisTransition",
    "Hypothesis",
    "make_hypothesis_id",
]
