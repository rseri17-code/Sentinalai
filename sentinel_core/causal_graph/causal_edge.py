"""Causal edge — 12 typed relationships."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CausalEdgeType(str, Enum):
    OBSERVED_IN            = "observed_in"
    CAUSED_BY              = "caused_by"
    SUPPORTS               = "supports"
    DISPROVES              = "disproves"
    PRECEDES               = "precedes"
    CORRELATES_WITH        = "correlates_with"
    RESOLVED_BY            = "resolved_by"
    AFFECTS                = "affects"
    DEPENDS_ON             = "depends_on"
    RECURS_WITH            = "recurs_with"
    REDUCES_MTTI           = "reduces_mtti"
    INCREASES_CONFIDENCE   = "increases_confidence"


def make_edge_id(source_id: str, target_id: str, edge_type: CausalEdgeType | str) -> str:
    t = edge_type.value if isinstance(edge_type, CausalEdgeType) else str(edge_type)
    return hashlib.sha256(f"{source_id}:{target_id}:{t}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class CausalEdge:
    edge_id:    str
    source_id:  str
    target_id:  str
    edge_type:  str
    weight:     float = 1.0
    properties: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def make(
        cls, source_id: str, target_id: str,
        edge_type: CausalEdgeType | str,
        weight: float = 1.0,
        properties: dict[str, Any] | None = None,
    ) -> "CausalEdge":
        return cls(
            edge_id=make_edge_id(source_id, target_id, edge_type),
            source_id=str(source_id),
            target_id=str(target_id),
            edge_type=(edge_type.value if isinstance(edge_type, CausalEdgeType)
                        else str(edge_type)),
            weight=float(weight),
            properties=dict(properties or {}),
        )


__all__ = ["CausalEdge", "CausalEdgeType", "make_edge_id"]
