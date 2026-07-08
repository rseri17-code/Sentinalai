"""Deterministic incident fingerprinting.

Given an incident's canonical identity fields, produce a stable
sha256[:16] fingerprint. Same input → same fingerprint. No embeddings.
No vector store.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from sentinel_core.intel_memory.schemas import (
    BlastRadiusSnapshot,
    MemoryRecord,
    TopologySnapshot,
)


FINGERPRINT_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Component hashes
# ---------------------------------------------------------------------------

def _sha16(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def compute_topology_hash(topo: TopologySnapshot | dict[str, Any] | None) -> str:
    """Deterministic hash of the topology snapshot."""
    if topo is None:
        return _sha16("topo:empty")
    if isinstance(topo, dict):
        services   = tuple(str(x) for x in (topo.get("services", ()) or ()))
        namespaces = tuple(str(x) for x in (topo.get("namespaces", ()) or ()))
        clusters   = tuple(str(x) for x in (topo.get("clusters", ()) or ()))
        regions    = tuple(str(x) for x in (topo.get("regions", ()) or ()))
        cloud      = str(topo.get("cloud", "") or "")
        gateway    = str(topo.get("gateway", "") or "")
        idp        = str(topo.get("idp", "") or "")
        dns        = str(topo.get("dns", "") or "")
        databases  = tuple(str(x) for x in (topo.get("databases", ()) or ()))
        deps_raw   = topo.get("dependencies", ()) or ()
        deps       = tuple((str(a), str(b)) for a, b in deps_raw)
    else:
        services   = topo.services
        namespaces = topo.namespaces
        clusters   = topo.clusters
        regions    = topo.regions
        cloud      = topo.cloud
        gateway    = topo.gateway
        idp        = topo.idp
        dns        = topo.dns
        databases  = topo.databases
        deps       = topo.dependencies

    parts = [
        f"svcs={_join_sorted(services)}",
        f"nss={_join_sorted(namespaces)}",
        f"clusters={_join_sorted(clusters)}",
        f"regions={_join_sorted(regions)}",
        f"cloud={cloud}",
        f"gw={gateway}",
        f"idp={idp}",
        f"dns={dns}",
        f"dbs={_join_sorted(databases)}",
        f"deps={_join_sorted([f'{a}>{b}' for a, b in deps])}",
    ]
    return _sha16("|".join(parts))


def compute_transaction_path_hash(path: Iterable[str] | None) -> str:
    """Deterministic sha16 hash of a transaction hop sequence.

    RC-G: previously joined hops with ``">"`` — a hop containing a
    literal ``>`` collided with a longer path split at that character.
    Framed JSON serialisation escapes such characters inside each
    element, closing the collision.
    """
    tokens = list(str(x) for x in (path or ()))
    return _sha16("txp:" + json.dumps(tokens, sort_keys=True))


def compute_planner_path_hash(steps: Iterable[str] | None) -> str:
    """Deterministic sha16 hash of a planner-step sequence.

    RC-G: same fix as :func:`compute_transaction_path_hash` — replaces
    the ``","`` delimiter with framed JSON to prevent collisions from
    steps that contain commas.
    """
    tokens = list(str(x) for x in (steps or ()))
    return _sha16("planner:" + json.dumps(tokens, sort_keys=True))


def compute_evidence_pattern_hash(evidence: Iterable[str] | None) -> str:
    """Sorted evidence keys → sha16."""
    return _sha16("evd:" + _join_sorted(evidence or ()))


def _blast_hash(blast: BlastRadiusSnapshot | dict[str, Any] | None) -> str:
    if blast is None:
        return _sha16("blast:none")
    if isinstance(blast, dict):
        sev = str(blast.get("severity", "low") or "low")
        tot = int(blast.get("total_affected", 0) or 0)
        aff = tuple(str(x) for x in (blast.get("affected", ()) or ()))
    else:
        sev = blast.severity
        tot = blast.total_affected
        aff = blast.affected
    return _sha16(f"blast:{sev}|{tot}|{_join_sorted(aff)}")


def _join_sorted(items: Iterable[str]) -> str:
    return ",".join(sorted({str(x) for x in items if str(x)}))


# ---------------------------------------------------------------------------
# FingerprintInput — flexible caller-friendly input
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FingerprintInput:
    """Every field is optional; missing fields degrade to empty strings.

    Use :func:`compute_fingerprint` to derive the sha16 fingerprint from
    a fully-populated :class:`FingerprintInput`.
    """
    service:            str  = ""
    environment:        str  = ""
    application:        str  = ""
    incident_type:      str  = ""
    topology:           Any  = None                # TopologySnapshot | dict | None
    deployment:         str  = ""
    namespace:          str  = ""
    pod:                str  = ""
    node:               str  = ""
    region:             str  = ""
    cloud:              str  = ""
    gateway:            str  = ""
    idp:                str  = ""
    dns:                str  = ""
    database:           str  = ""
    error_type:         str  = ""
    transaction_path:   tuple[str, ...] = ()
    infrastructure_deps: tuple[str, ...] = ()
    blast_radius:       Any  = None                # BlastRadiusSnapshot | dict | None
    planner_path:       tuple[str, ...] = ()
    evidence_pattern:   tuple[str, ...] = ()


def compute_fingerprint(fi: FingerprintInput) -> str:
    """Deterministic sha256[:16] fingerprint. Same input → same output."""
    parts = [
        f"svc={fi.service}",
        f"env={fi.environment}",
        f"app={fi.application}",
        f"it={fi.incident_type}",
        f"topo={compute_topology_hash(fi.topology)}",
        f"dep={fi.deployment}",
        f"ns={fi.namespace}",
        f"pod={fi.pod}",
        f"node={fi.node}",
        f"region={fi.region}",
        f"cloud={fi.cloud}",
        f"gw={fi.gateway}",
        f"idp={fi.idp}",
        f"dns={fi.dns}",
        f"db={fi.database}",
        f"err={fi.error_type}",
        f"txp={compute_transaction_path_hash(fi.transaction_path)}",
        f"idep={_join_sorted(fi.infrastructure_deps)}",
        f"blast={_blast_hash(fi.blast_radius)}",
        f"planner={compute_planner_path_hash(fi.planner_path)}",
        f"evd={compute_evidence_pattern_hash(fi.evidence_pattern)}",
    ]
    return _sha16("|".join(parts))


def fingerprint_from_record(rec: MemoryRecord) -> str:
    """Derive a fingerprint from a fully-populated MemoryRecord."""
    fi = FingerprintInput(
        service=rec.service,
        environment=rec.environment,
        application=rec.application,
        incident_type=rec.incident_type,
        topology=rec.topology,
        transaction_path=rec.transaction_path,
        blast_radius=rec.blast_radius,
        planner_path=rec.planner_decisions,
        evidence_pattern=rec.evidence_collected,
        # Coarse component fields — pulled from the topology when
        # available.
        namespace=rec.topology.namespaces[0] if rec.topology.namespaces else "",
        region=rec.topology.regions[0] if rec.topology.regions else "",
        cloud=rec.topology.cloud,
        gateway=rec.topology.gateway,
        idp=rec.topology.idp,
        dns=rec.topology.dns,
    )
    return compute_fingerprint(fi)


__all__ = [
    "FINGERPRINT_SCHEMA_VERSION",
    "FingerprintInput",
    "compute_fingerprint",
    "compute_topology_hash",
    "compute_transaction_path_hash",
    "compute_planner_path_hash",
    "compute_evidence_pattern_hash",
    "fingerprint_from_record",
]
