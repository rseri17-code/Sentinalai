"""Compatibility adapter between the legacy evidence dict and EvidenceLedger.

The supervisor still uses a plain ``dict[str, Any]`` for evidence (50+
mutation sites in ``supervisor.agent``). These helpers let new code adopt
the ledger at a boundary without forcing a rewrite:

- ``dict_to_ledger(d)`` — wrap an existing evidence dict.
- ``ledger_to_dict(l)`` — flatten back to the legacy dict shape.
- ``round_trip(d)`` — convenience: ``ledger_to_dict(dict_to_ledger(d))``.
  Lossless by contract — used in tests and parity checks.

The default ``kind`` for any key is guessed by ``infer_kind_for_key()``
which uses the family taxonomy from the Phase 8A reality scan. The guess
is best-effort and never raises.
"""
from __future__ import annotations

from typing import Any

from sentinel_core.evidence.ledger import (
    EvidenceKind,
    EvidenceLedger,
    EvidenceSource,
)


# ---------------------------------------------------------------------------
# Key → kind inference
# ---------------------------------------------------------------------------

# Map well-known evidence keys to their EvidenceKind family. Keys not in this
# map fall through to ``EvidenceKind.WORKER_RESULT`` (worker labels are
# dynamic), unless they start with ``_`` in which case they become PROVENANCE.
_KEY_TO_KIND: dict[str, EvidenceKind] = {
    # Logs family
    "logs":               EvidenceKind.LOGS,
    "log_data":           EvidenceKind.LOGS,
    "search_logs":        EvidenceKind.LOGS,
    # Metrics family
    "metrics":            EvidenceKind.METRICS,
    "query_metrics":      EvidenceKind.METRICS,
    "get_resource_metrics": EvidenceKind.METRICS,
    # Golden signals
    "get_golden_signals": EvidenceKind.GOLDEN_SIGNALS,
    "golden_signals":     EvidenceKind.GOLDEN_SIGNALS,
    # Events / changes
    "events":             EvidenceKind.EVENTS,
    "get_events":         EvidenceKind.EVENTS,
    "changes":            EvidenceKind.CHANGES,
    "get_change_data":    EvidenceKind.CHANGES,
    "change_records":     EvidenceKind.CHANGES,
    # APM / traces
    "apm":                EvidenceKind.APM,
    "apm_data":           EvidenceKind.APM,
    "trace_correlation":  EvidenceKind.APM,
    # ITSM / devops / confluence
    "itsm_context":       EvidenceKind.ITSM,
    "devops_context":     EvidenceKind.DEVOPS,
    "diff_analysis":      EvidenceKind.DEVOPS,
    "git_blame":          EvidenceKind.DEVOPS,
    "git_context":        EvidenceKind.DEVOPS,
    "cmdb_blast_radius":  EvidenceKind.DEVOPS,
    "confluence_context": EvidenceKind.CONFLUENCE,
    # Historical / priming
    "historical_context": EvidenceKind.HISTORICAL,
    # Network
    "network_evidence":    EvidenceKind.NETWORK,
    "network_correlation": EvidenceKind.NETWORK,
    "network_summary":     EvidenceKind.NETWORK,
    # Correlation
    "visual_evidence":     EvidenceKind.CORRELATION,
}


def infer_kind_for_key(key: str) -> EvidenceKind:
    """Best-effort family lookup for an evidence key."""
    if key.startswith("_"):
        return EvidenceKind.PROVENANCE
    return _KEY_TO_KIND.get(key, EvidenceKind.WORKER_RESULT)


def infer_source_for_key(key: str) -> EvidenceSource:
    """Best-effort source lookup for an evidence key.

    Underscore-prefixed keys are POST_PROCESSING (supervisor-injected); all
    other keys default to WORKER (the playbook produced them).
    """
    if key.startswith("_"):
        return EvidenceSource.POST_PROCESSING
    return EvidenceSource.WORKER


# ---------------------------------------------------------------------------
# Adapter functions
# ---------------------------------------------------------------------------

def dict_to_ledger(
    data: dict[str, Any],
    *,
    infer_provenance: bool = True,
) -> EvidenceLedger:
    """Wrap a legacy evidence dict in an EvidenceLedger.

    When ``infer_provenance=True`` (default), each key is annotated with a
    best-effort ``EvidenceKind`` / ``EvidenceSource`` based on its name.
    Pass ``infer_provenance=False`` to stamp every entry with the default
    UNKNOWN / OTHER provenance instead.
    """
    ledger = EvidenceLedger()
    if infer_provenance:
        for k, v in data.items():
            ledger.add(
                k, v,
                source=infer_source_for_key(k),
                kind=infer_kind_for_key(k),
            )
    else:
        ledger.merge_dict(data)
    return ledger


def ledger_to_dict(ledger: EvidenceLedger) -> dict[str, Any]:
    """Flatten a ledger back to the legacy ``dict[str, Any]`` shape.

    Lossy with respect to provenance (kind / source / confidence / metadata
    / timestamp are dropped). Use ``ledger.to_full_dict()`` when provenance
    must survive — but note that the supervisor's evidence dict has no
    provenance to round-trip with.
    """
    return ledger.to_dict()


def round_trip(data: dict[str, Any]) -> dict[str, Any]:
    """``ledger_to_dict(dict_to_ledger(d))``. Must be lossless on keys + values."""
    return ledger_to_dict(dict_to_ledger(data))


__all__ = [
    "dict_to_ledger",
    "ledger_to_dict",
    "round_trip",
    "infer_kind_for_key",
    "infer_source_for_key",
]
