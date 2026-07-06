"""Causal node — 12 typed entity kinds."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from sentinel_core.models._immutable import freeze_dict


class CausalNodeType(str, Enum):
    INCIDENT          = "incident"
    SERVICE           = "service"
    SYMPTOM           = "symptom"
    SIGNAL            = "signal"
    HYPOTHESIS        = "hypothesis"
    EVIDENCE          = "evidence"
    ROOT_CAUSE        = "root_cause"
    REMEDIATION       = "remediation"
    DEPLOYMENT_CHANGE = "deployment_change"
    DEPENDENCY        = "dependency"
    FAILURE_MODE      = "failure_mode"
    INCIDENT_PATTERN  = "incident_pattern"


def make_node_id(node_type: CausalNodeType | str, label: str) -> str:
    t = node_type.value if isinstance(node_type, CausalNodeType) else str(node_type)
    raw = f"{t}:{label}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class CausalNode:
    node_id:    str
    node_type:  str
    label:      str
    properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # RC-D: prevent mutation of properties dict via attribute access.
        object.__setattr__(self, "properties", freeze_dict(self.properties))

    @classmethod
    def make(
        cls, node_type: CausalNodeType | str, label: str,
        properties: dict[str, Any] | None = None,
    ) -> "CausalNode":
        return cls(
            node_id=make_node_id(node_type, label),
            node_type=(node_type.value if isinstance(node_type, CausalNodeType)
                        else str(node_type)),
            label=str(label),
            properties=dict(properties or {}),
        )


__all__ = ["CausalNode", "CausalNodeType", "make_node_id"]
