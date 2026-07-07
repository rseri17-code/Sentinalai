"""IntelligenceContext — canonical unified view of all POST_CLASSIFY
intelligence lookups for one investigation.

The Intelligence Runtime records six independent read-path modules at
POST_CLASSIFY (historical_lookup, pattern_recognition,
incident_graph_lookup, dependency_graph_lookup, episodic_memory_lookup,
causal_graph_lookup). Each emits its own compact receipt payload.

This module gives that data a **single canonical shape** and a
factory that builds it from a list of phase receipts. It is a pure
library — it never reads from disk, never touches the runtime, never
mutates receipts. Consumers (tests, downstream tooling, future
analyzer-prompt augmentation) construct an IntelligenceContext from
the receipts they already receive.

Design principles
-----------------
- Frozen dataclass — safe to pass across boundaries.
- Additive: unknown modules are ignored; missing modules yield empty
  sections. Callers never crash if a runner was disabled.
- Bounded: only the compact per-source summaries the read modules
  already produce are captured; no expansion into raw store rows.
- Deterministic: order preserved, no timestamps injected.

Read shape returned by each module is defined in
``supervisor/intelligence_modules/*.py``. Where a field is absent from
a source we surface the default and continue. The canonical shape
here is designed to hold every source's payload without loss.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from sentinel_core.models._coerce import (
    coerce_float,
    coerce_int,
    coerce_str,
)


# ---------------------------------------------------------------------------
# Per-source summaries
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolutionMemoryMatch:
    memory_id:      str
    root_cause_head: str
    confidence:     int
    recorded_at:    str
    service:        str = ""
    incident_type:  str = ""


@dataclass(frozen=True)
class InvestigationMatch:
    investigation_id: str
    created_at:       str
    incident_type:    str = ""
    service:          str = ""


@dataclass(frozen=True)
class PatternMatch:
    pattern_id:         str
    incident_type:      str
    services:           list[str] = field(default_factory=list)
    canonical_symptoms: list[str] = field(default_factory=list)
    occurrence_count:   int = 0
    success_count:      int = 0
    success_rate:       float = 0.0
    last_seen:          str = ""


@dataclass(frozen=True)
class DependencyEdge:
    source_service: str
    target_service: str
    dep_type:       str
    strength:       float
    observed_count: int = 0
    last_seen:      str = ""


@dataclass(frozen=True)
class EpisodeMatch:
    episode_id:             str
    incident_id:            str
    service:                str
    incident_type:          str
    root_cause_head:        str
    resolution_action_head: str
    outcome:                str
    confidence:             float
    recorded_at:            str


@dataclass(frozen=True)
class AffectedService:
    service_id:      str
    probability:     float
    propagation_ms:  int
    path:            list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Canonical container
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IntelligenceContext:
    service:       str = ""
    incident_type: str = ""

    # Historical (RM + IS)
    resolution_memory_matches:  tuple[ResolutionMemoryMatch, ...] = ()
    investigation_matches:      tuple[InvestigationMatch, ...] = ()

    # Pattern
    pattern_matches:            tuple[PatternMatch, ...] = ()

    # IncidentGraph
    related_incident_ids:       tuple[str, ...] = ()

    # DependencyGraph
    upstream_dependencies:      tuple[DependencyEdge, ...] = ()
    downstream_dependents:      tuple[DependencyEdge, ...] = ()
    affected_services:          tuple[str, ...] = ()

    # EpisodicMemory
    episode_matches:            tuple[EpisodeMatch, ...] = ()

    # CausalGraph
    blast_radius_severity:      str = "low"
    blast_radius_total_affected: int = 0
    blast_radius_affected:      tuple[AffectedService, ...] = ()

    # Provenance
    module_names_seen:          tuple[str, ...] = ()

    # ------------------------------------------------------------------
    # Predicates
    # ------------------------------------------------------------------

    def is_empty(self) -> bool:
        """True iff every source returned zero matches AND we have no service."""
        return (
            not self.service
            and not self.incident_type
            and not self.resolution_memory_matches
            and not self.investigation_matches
            and not self.pattern_matches
            and not self.related_incident_ids
            and not self.upstream_dependencies
            and not self.downstream_dependents
            and not self.episode_matches
            and self.blast_radius_total_affected == 0
        )

    def total_signal_count(self) -> int:
        """Bounded signal count across every source (excluding blast-radius severity)."""
        return (
            len(self.resolution_memory_matches)
            + len(self.investigation_matches)
            + len(self.pattern_matches)
            + len(self.related_incident_ids)
            + len(self.upstream_dependencies)
            + len(self.downstream_dependents)
            + len(self.affected_services)
            + len(self.episode_matches)
            + self.blast_radius_total_affected
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict rendering — every tuple becomes a list.

        RC-I: the previous implementation relied on ``dataclasses.asdict``
        to perform the tuple→list normalization, but ``asdict`` in fact
        preserves the tuple type on tuple fields. Callers doing strict
        Python-level equality against a JSON round-trip therefore saw a
        mismatch. The walker below now enforces the documented contract
        by recursively converting every tuple to a list at every depth.

        The final dict shape and JSON serialization are byte-identical
        to what ``json.dumps(...)`` would emit — ``json.dumps`` treats
        tuples and lists identically as arrays, so any caller already
        going through ``json.dumps`` sees no change.
        """
        return _tuples_to_lists(asdict(self))

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_receipts(cls, receipts: Iterable[dict[str, Any]]) -> "IntelligenceContext":
        """Construct an IntelligenceContext from an iterable of phase receipts.

        Accepts the receipt format produced by ``PhaseReceiptCollector.to_list()``:
        each receipt is a dict; if the receipt has a ``metadata.intelligence``
        key (as populated by the runtime hook in supervisor.agent) that list
        is scanned. Any receipt without intelligence entries is silently
        ignored.

        Unknown module names are ignored — so future modules do not break
        old callers.
        """
        # RC-F: flatten receipts into (name, payload) tuples first, then
        # sort canonically before merging. Same set of receipts arrives
        # at the merge step in the same order regardless of the caller's
        # input list order.
        _entries: list[tuple[str, dict[str, Any], str]] = []
        for r in receipts or []:
            if not isinstance(r, dict):
                continue
            meta = r.get("metadata") or {}
            entries = meta.get("intelligence") if isinstance(meta, dict) else None
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                # Use the "metadata" payload written by the runtime hook.
                # ModuleResult.to_dict() puts the runner's returned dict under
                # entry["metadata"].
                payload = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
                if name:
                    # Serialize payload deterministically for tie-break.
                    try:
                        payload_key = json.dumps(payload, sort_keys=True,
                                                  default=str)
                    except (TypeError, ValueError):
                        payload_key = repr(payload)
                    _entries.append((str(name), payload, payload_key))
        # Canonical order: name ascending, then payload_key ascending.
        _entries.sort(key=lambda t: (t[0], t[2]))
        # RC-J: previously the loop was last-write-wins, silently
        # dropping every earlier payload for the same module. Now
        # duplicates for the same module are merged so no information
        # is lost. Merge is deterministic (union of canonical inputs).
        by_name: dict[str, dict[str, Any]] = {}
        for name, payload, _ in _entries:
            if name in by_name:
                by_name[name] = _merge_module_payloads(by_name[name], payload)
            else:
                by_name[name] = payload

        service = ""
        incident_type = ""

        # historical_lookup — RC-H: coerce_* prevents "None" contamination
        # from JSON nulls and prevents ValueError on adversarial strings.
        hl = by_name.get("historical_lookup") or {}
        service = service or coerce_str(hl.get("service"))
        incident_type = incident_type or coerce_str(hl.get("incident_type"))
        rm_matches = tuple(
            ResolutionMemoryMatch(
                memory_id=coerce_str(m.get("memory_id")),
                root_cause_head=coerce_str(m.get("root_cause_head")),
                confidence=coerce_int(m.get("confidence")),
                recorded_at=coerce_str(m.get("recorded_at")),
                service=coerce_str(m.get("service")),
                incident_type=coerce_str(m.get("incident_type")),
            )
            for m in hl.get("resolution_memory_matches", []) or []
            if isinstance(m, dict)
        )
        inv_matches = tuple(
            InvestigationMatch(
                investigation_id=coerce_str(m.get("investigation_id")),
                created_at=coerce_str(m.get("created_at")),
                incident_type=coerce_str(m.get("incident_type")),
                service=coerce_str(m.get("service")),
            )
            for m in hl.get("investigation_matches", []) or []
            if isinstance(m, dict)
        )

        # pattern_recognition
        pr = by_name.get("pattern_recognition") or {}
        service = service or coerce_str(pr.get("service"))
        incident_type = incident_type or coerce_str(pr.get("incident_type"))
        patterns = tuple(
            PatternMatch(
                pattern_id=coerce_str(p.get("pattern_id")),
                incident_type=coerce_str(p.get("incident_type")),
                services=[coerce_str(x) for x in (p.get("services") or [])],
                canonical_symptoms=[coerce_str(x) for x in
                                     (p.get("canonical_symptoms") or [])],
                occurrence_count=coerce_int(p.get("occurrence_count")),
                success_count=coerce_int(p.get("success_count")),
                success_rate=coerce_float(p.get("success_rate")),
                last_seen=coerce_str(p.get("last_seen")),
            )
            for p in pr.get("pattern_matches", []) or []
            if isinstance(p, dict)
        )

        # incident_graph_lookup
        ig = by_name.get("incident_graph_lookup") or {}
        service = service or coerce_str(ig.get("service"))
        related = tuple(coerce_str(x)
                          for x in (ig.get("related_incident_ids") or []))

        # dependency_graph_lookup
        dg = by_name.get("dependency_graph_lookup") or {}
        service = service or coerce_str(dg.get("service"))
        upstream = tuple(
            DependencyEdge(
                source_service=coerce_str(e.get("source_service")),
                target_service=coerce_str(e.get("target_service")),
                dep_type=coerce_str(e.get("dep_type")),
                strength=coerce_float(e.get("strength")),
                observed_count=coerce_int(e.get("observed_count")),
                last_seen=coerce_str(e.get("last_seen")),
            )
            for e in dg.get("upstream", []) or []
            if isinstance(e, dict)
        )
        downstream = tuple(
            DependencyEdge(
                source_service=coerce_str(e.get("source_service")),
                target_service=coerce_str(e.get("target_service")),
                dep_type=coerce_str(e.get("dep_type")),
                strength=coerce_float(e.get("strength")),
                observed_count=coerce_int(e.get("observed_count")),
                last_seen=coerce_str(e.get("last_seen")),
            )
            for e in dg.get("downstream", []) or []
            if isinstance(e, dict)
        )
        affected_services = tuple(coerce_str(x)
                                   for x in (dg.get("affected_services") or []))

        # episodic_memory_lookup
        em = by_name.get("episodic_memory_lookup") or {}
        service = service or coerce_str(em.get("service"))
        incident_type = incident_type or coerce_str(em.get("incident_type"))
        episodes = tuple(
            EpisodeMatch(
                episode_id=coerce_str(e.get("episode_id")),
                incident_id=coerce_str(e.get("incident_id")),
                service=coerce_str(e.get("service")),
                incident_type=coerce_str(e.get("incident_type")),
                root_cause_head=coerce_str(e.get("root_cause_head")),
                resolution_action_head=coerce_str(e.get("resolution_action_head")),
                outcome=coerce_str(e.get("outcome")),
                confidence=coerce_float(e.get("confidence")),
                recorded_at=coerce_str(e.get("recorded_at")),
            )
            for e in em.get("episodes", []) or []
            if isinstance(e, dict)
        )

        # causal_graph_lookup
        cg = by_name.get("causal_graph_lookup") or {}
        service = service or coerce_str(cg.get("service"))
        blast_severity = coerce_str(cg.get("severity"), default="low") or "low"
        blast_total = coerce_int(cg.get("total_affected"))
        blast_affected = tuple(
            AffectedService(
                service_id=coerce_str(a.get("service_id")),
                probability=coerce_float(a.get("probability")),
                propagation_ms=coerce_int(a.get("propagation_ms")),
                path=[coerce_str(x) for x in (a.get("path") or [])],
            )
            for a in cg.get("affected", []) or []
            if isinstance(a, dict)
        )

        return cls(
            service=service,
            incident_type=incident_type,
            resolution_memory_matches=rm_matches,
            investigation_matches=inv_matches,
            pattern_matches=patterns,
            related_incident_ids=related,
            upstream_dependencies=upstream,
            downstream_dependents=downstream,
            affected_services=affected_services,
            episode_matches=episodes,
            blast_radius_severity=blast_severity,
            blast_radius_total_affected=blast_total,
            blast_radius_affected=blast_affected,
            module_names_seen=tuple(sorted(by_name.keys())),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tuples_to_lists(obj: Any) -> Any:
    """Recursively convert every tuple in ``obj`` to a list.

    Mirrors the walker in ``sentinel_core/intel_memory/schemas.py``.
    RC-I: the JSON-safe rendering contract now truly holds — every
    tuple, at every depth, becomes a list.
    """
    if isinstance(obj, tuple):
        return [_tuples_to_lists(v) for v in obj]
    if isinstance(obj, list):
        return [_tuples_to_lists(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _tuples_to_lists(v) for k, v in obj.items()}
    return obj


def _merge_module_payloads(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """RC-J: deterministically merge two payloads for the same module.

    Policy (never silently drops data):

    - Keys present in only one side are kept unchanged.
    - Both scalar: keep ``a`` if non-empty, else ``b``. Ties (both
      equal) keep ``a``.
    - Both list: concatenate ``a + b``, then dedupe preserving the
      canonical JSON form of each element (first-occurrence wins on
      the deduped output — order is stable because ``a`` was
      canonically pre-sorted upstream).
    - Both dict: recurse.
    - Mixed types: keep ``a`` (the earlier canonical entry).

    Pure and deterministic — same ``(a, b)`` always returns the same
    dict.
    """
    if not isinstance(a, dict) or not isinstance(b, dict):
        return a if isinstance(a, dict) else (b if isinstance(b, dict) else {})
    out: dict[str, Any] = {}
    keys = sorted(set(a.keys()) | set(b.keys()))
    for k in keys:
        if k not in a:
            out[k] = b[k]
        elif k not in b:
            out[k] = a[k]
        else:
            va, vb = a[k], b[k]
            if isinstance(va, dict) and isinstance(vb, dict):
                out[k] = _merge_module_payloads(va, vb)
            elif isinstance(va, list) and isinstance(vb, list):
                out[k] = _dedupe_by_json(va + vb)
            elif type(va) is type(vb):
                # Scalar of the same type — keep the earlier (a) unless
                # it is falsy and b is non-falsy.
                out[k] = va if va else vb
            else:
                # Mixed types — deterministic pick of the earlier side.
                out[k] = va
    return out


def _dedupe_by_json(items: list[Any]) -> list[Any]:
    """Return ``items`` with duplicates (by canonical JSON) removed,
    preserving first-occurrence order."""
    seen: set[str] = set()
    out: list[Any] = []
    for it in items:
        try:
            key = json.dumps(it, sort_keys=True, default=str)
        except (TypeError, ValueError):
            key = repr(it)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


__all__ = [
    "IntelligenceContext",
    "ResolutionMemoryMatch",
    "InvestigationMatch",
    "PatternMatch",
    "DependencyEdge",
    "EpisodeMatch",
    "AffectedService",
]
