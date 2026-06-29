"""sentinel_core.evidence — typed evidence ledger and adapter.

Public API:

    from sentinel_core.evidence import (
        EvidenceLedger, EvidenceItem, EvidenceSnapshot,
        EvidenceSource, EvidenceKind,
        dict_to_ledger, ledger_to_dict, round_trip,
        is_shadow_enabled,
    )

Phase 8 ships the model + adapter only. No agent.py code is wired to use
the ledger yet — that integration is gated behind ``is_shadow_enabled()``
and is the work of a future phase.
"""
from sentinel_core.evidence.ledger import (
    EvidenceLedger,
    EvidenceItem,
    EvidenceSnapshot,
    EvidenceSource,
    EvidenceKind,
)
from sentinel_core.evidence.adapter import (
    dict_to_ledger,
    ledger_to_dict,
    round_trip,
    infer_kind_for_key,
    infer_source_for_key,
)
from sentinel_core.evidence.shadow import (
    is_shadow_enabled,
    SHADOW_ENV_VAR,
)

__all__ = [
    "EvidenceLedger",
    "EvidenceItem",
    "EvidenceSnapshot",
    "EvidenceSource",
    "EvidenceKind",
    "dict_to_ledger",
    "ledger_to_dict",
    "round_trip",
    "infer_kind_for_key",
    "infer_source_for_key",
    "is_shadow_enabled",
    "SHADOW_ENV_VAR",
]
