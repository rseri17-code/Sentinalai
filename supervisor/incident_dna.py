"""Incident DNA: feature-vector encoding for cross-dimensional similarity search.

Each incident is encoded as a 16-dimensional feature vector (its "DNA").
Cosine similarity between DNA profiles surfaces non-obvious matches:
an OOMKill in user-service 6 months ago may share the same DNA as a
Kafka consumer stall today — both driven by an underlying resource-leak
pattern.  No competitor does this kind of cross-incident synthesis.

Feature vector dimensions (all normalised to [0, 1]):
  0  error_rate_ratio         observed_error_rate / baseline_error_rate  (capped 1.0)
  1  latency_ratio            observed_p95 / baseline_p95                (capped 1.0)
  2  cpu_anomaly              1.0/>80 % | 0.5/50-80 % | 0.0 otherwise
  3  memory_anomaly           1.0/>85 % | 0.5/60-85 % | 0.0 otherwise
  4  network_anomaly          1.0 if network errors present, else 0.0
  5  multi_service_impact     num_affected_services / 10                 (capped 1.0)
  6  change_recency           1.0/<30 min | 0.7/<2 h | 0.3/<24 h | 0.0
  7  incident_type_timeout    1.0 if timeout, else 0.0
  8  incident_type_memory     1.0 if oomkill, else 0.0
  9  incident_type_error      1.0 if error_spike, else 0.0
  10 incident_type_network    1.0 if network, else 0.0
  11 incident_type_resource   1.0 if saturation, else 0.0
  12 service_tier             1.0/P1 | 0.67/P2 | 0.33/P3
  13 evidence_source_count    num_evidence_sources / 7                   (capped 1.0)
  14 confidence_score         rca_confidence / 100
  15 resolution_time_bucket   0.25/<15 min | 0.5/15-60 min | 0.75/1-4 h | 1.0/>4 h
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.incident_dna")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FEATURE_NAMES: list[str] = [
    "error_rate_ratio",
    "latency_ratio",
    "cpu_anomaly",
    "memory_anomaly",
    "network_anomaly",
    "multi_service_impact",
    "change_recency",
    "incident_type_timeout",
    "incident_type_memory",
    "incident_type_error",
    "incident_type_network",
    "incident_type_resource",
    "service_tier",
    "evidence_source_count",
    "confidence_score",
    "resolution_time_bucket",
]

_NUM_FEATURES = len(FEATURE_NAMES)  # 16

# Threshold above which a dimension is considered "significantly anomalous"
_MATCHING_THRESHOLD = 0.5

# Human-readable labels keyed by feature name for insight generation
_FEATURE_LABELS: dict[str, str] = {
    "error_rate_ratio":       "elevated error rate",
    "latency_ratio":          "elevated latency",
    "cpu_anomaly":            "CPU pressure",
    "memory_anomaly":         "memory pressure",
    "network_anomaly":        "network errors",
    "multi_service_impact":   "multi-service impact",
    "change_recency":         "recent deployment",
    "incident_type_timeout":  "timeout pattern",
    "incident_type_memory":   "memory-kill pattern",
    "incident_type_error":    "error-spike pattern",
    "incident_type_network":  "network pattern",
    "incident_type_resource": "resource-saturation pattern",
    "service_tier":           "high-tier service",
    "evidence_source_count":  "rich evidence signal",
    "confidence_score":       "high diagnosis confidence",
    "resolution_time_bucket": "long resolution time",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class IncidentDNA:
    """Feature-vector representation of a single incident."""

    incident_id: str
    incident_type: str
    service: str
    features: list[float]        # 16-dim vector, all in [0, 1]
    feature_names: list[str]     # human-readable names for each dimension
    encoded_at: str              # ISO-8601 timestamp

    # ------------------------------------------------------------------
    # Similarity
    # ------------------------------------------------------------------

    def similarity(self, other: "IncidentDNA") -> float:
        """Cosine similarity between two DNA profiles.

        Returns a value in [0, 1].  Returns 0.0 if either vector is all
        zeros (undefined cosine similarity).
        """
        return _cosine_similarity(self.features, other.features)

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id":   self.incident_id,
            "incident_type": self.incident_type,
            "service":       self.service,
            "features":      self.features,
            "feature_names": self.feature_names,
            "encoded_at":    self.encoded_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IncidentDNA":
        return cls(
            incident_id=data["incident_id"],
            incident_type=data["incident_type"],
            service=data["service"],
            features=data["features"],
            feature_names=data["feature_names"],
            encoded_at=data["encoded_at"],
        )


@dataclass
class DNAMatch:
    """A past incident that matches the query by DNA similarity."""

    incident_id: str
    incident_type: str
    service: str
    root_cause: str
    similarity_score: float         # 0.0–1.0
    matching_dimensions: list[str]  # feature names where both vectors > 0.5
    insight: str                    # human-readable explanation of the match


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def encode_incident(
    incident_id: str,
    incident_type: str,
    service: str,
    evidence: dict[str, Any],
    rca_confidence: float,
    service_tier: str = "P2",
    resolution_minutes: int = 60,
) -> IncidentDNA:
    """Encode an incident into its 16-dimensional DNA feature vector.

    All missing evidence keys are handled gracefully (default 0.0).

    Args:
        incident_id:        Unique incident identifier.
        incident_type:      Classified type (timeout, oomkill, error_spike,
                            network, saturation, …).
        service:            Affected service name.
        evidence:           Evidence dict collected during investigation.
                            Expected keys documented inline below.
        rca_confidence:     RCA confidence 0–100.
        service_tier:       "P1", "P2", or "P3".
        resolution_minutes: Time from detection to resolution.

    Returns:
        IncidentDNA with a fully populated 16-dim features list.
    """
    features: list[float] = [0.0] * _NUM_FEATURES

    # 0 — error_rate_ratio
    obs_err  = _safe_float(evidence, "observed_error_rate", 0.0)
    base_err = _safe_float(evidence, "baseline_error_rate", 0.0)
    if base_err > 0:
        features[0] = min(obs_err / base_err, 1.0)

    # 1 — latency_ratio
    obs_lat  = _safe_float(evidence, "observed_p95", 0.0)
    base_lat = _safe_float(evidence, "baseline_p95", 0.0)
    if base_lat > 0:
        features[1] = min(obs_lat / base_lat, 1.0)

    # 2 — cpu_anomaly
    cpu_pct = _safe_float(evidence, "cpu_percent", 0.0)
    if cpu_pct > 80:
        features[2] = 1.0
    elif cpu_pct >= 50:
        features[2] = 0.5

    # 3 — memory_anomaly
    mem_pct = _safe_float(evidence, "memory_percent", 0.0)
    if mem_pct > 85:
        features[3] = 1.0
    elif mem_pct >= 60:
        features[3] = 0.5

    # 4 — network_anomaly
    network_errors = evidence.get("network_errors")
    if network_errors:
        features[4] = 1.0

    # 5 — multi_service_impact
    num_affected = _safe_int(evidence, "num_affected_services", 0)
    features[5] = min(num_affected / 10.0, 1.0)

    # 6 — change_recency
    deploy_minutes_ago = _safe_float(evidence, "last_deploy_minutes_ago", -1.0)
    if deploy_minutes_ago < 0:
        features[6] = 0.0
    elif deploy_minutes_ago <= 30:
        features[6] = 1.0
    elif deploy_minutes_ago <= 120:
        features[6] = 0.7
    elif deploy_minutes_ago <= 1440:
        features[6] = 0.3
    else:
        features[6] = 0.0

    # 7-11 — incident type one-hot weights
    itype = (incident_type or "").lower()
    features[7]  = 1.0 if itype == "timeout"     else 0.0
    features[8]  = 1.0 if itype == "oomkill"     else 0.0
    features[9]  = 1.0 if itype == "error_spike" else 0.0
    features[10] = 1.0 if itype == "network"     else 0.0
    features[11] = 1.0 if itype == "saturation"  else 0.0

    # 12 — service_tier
    tier = (service_tier or "P2").upper()
    if tier == "P1":
        features[12] = 1.0
    elif tier == "P2":
        features[12] = 0.67
    else:
        features[12] = 0.33

    # 13 — evidence_source_count
    num_sources = _safe_int(evidence, "num_evidence_sources", 0)
    if num_sources == 0:
        # Fall back to counting non-empty, non-private keys in evidence
        num_sources = sum(
            1 for k, v in evidence.items()
            if not k.startswith("_") and v not in (None, "", [], {})
        )
    features[13] = min(num_sources / 7.0, 1.0)

    # 14 — confidence_score
    features[14] = min(max(rca_confidence / 100.0, 0.0), 1.0)

    # 15 — resolution_time_bucket
    if resolution_minutes < 15:
        features[15] = 0.25
    elif resolution_minutes < 60:
        features[15] = 0.5
    elif resolution_minutes < 240:
        features[15] = 0.75
    else:
        features[15] = 1.0

    return IncidentDNA(
        incident_id=incident_id,
        incident_type=incident_type,
        service=service,
        features=features,
        feature_names=list(FEATURE_NAMES),
        encoded_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Similarity search
# ---------------------------------------------------------------------------

def find_similar_by_dna(
    query_dna: IncidentDNA,
    candidate_dnas: list[IncidentDNA],
    top_k: int = 5,
    min_similarity: float = 0.6,
) -> list[DNAMatch]:
    """Find the most similar past incidents by DNA cosine similarity.

    Args:
        query_dna:       The current incident's DNA.
        candidate_dnas:  Pool of past DNA profiles to search.
        top_k:           Maximum results to return.
        min_similarity:  Minimum cosine similarity to include.

    Returns:
        List of DNAMatch sorted by similarity descending, len <= top_k.
    """
    results: list[DNAMatch] = []

    for candidate in candidate_dnas:
        # Skip self-match
        if candidate.incident_id == query_dna.incident_id:
            continue

        score = query_dna.similarity(candidate)
        if score < min_similarity:
            continue

        matching_dims = _matching_dimensions(query_dna.features, candidate.features)
        insight = _generate_insight(
            query_dna=query_dna,
            candidate=candidate,
            matching_dims=matching_dims,
            score=score,
        )

        # root_cause is stored as a property on the DNA if available; else empty
        root_cause = getattr(candidate, "root_cause", "")

        results.append(DNAMatch(
            incident_id=candidate.incident_id,
            incident_type=candidate.incident_type,
            service=candidate.service,
            root_cause=root_cause,
            similarity_score=round(score, 4),
            matching_dimensions=matching_dims,
            insight=insight,
        ))

    results.sort(key=lambda m: m.similarity_score, reverse=True)
    return results[:top_k]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_dna_store(store_path: str = "eval/incident_dna_store.json") -> list[IncidentDNA]:
    """Load persisted DNA profiles from disk.

    Returns an empty list if the file is not found or is corrupt.
    """
    try:
        with open(store_path, "r") as f:
            raw = json.load(f)
        if not isinstance(raw, list):
            logger.warning("DNA store format unexpected — returning empty list")
            return []
        return [IncidentDNA.from_dict(item) for item in raw]
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("DNA store corrupt or schema mismatch: %s — returning empty list", exc)
        return []


def save_dna_store(
    dnas: list[IncidentDNA],
    store_path: str = "eval/incident_dna_store.json",
) -> None:
    """Persist DNA profiles to disk atomically."""
    os.makedirs(os.path.dirname(os.path.abspath(store_path)), exist_ok=True)
    tmp = store_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump([d.to_dict() for d in dnas], f, indent=2)
    os.replace(tmp, store_path)
    logger.debug("Saved %d DNA profiles to %s", len(dnas), store_path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors.

    Returns 0.0 if either vector is all-zeros (undefined cosine).
    Result is clamped to [0, 1] to guard against floating-point edge cases.
    """
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    raw = dot / (norm_a * norm_b)
    return max(0.0, min(1.0, raw))


def _matching_dimensions(
    a: list[float],
    b: list[float],
    threshold: float = _MATCHING_THRESHOLD,
) -> list[str]:
    """Return feature names where both vectors exceed the threshold.

    These are the dimensions where both incidents show significant anomaly.
    """
    return [
        FEATURE_NAMES[i]
        for i, (va, vb) in enumerate(zip(a, b))
        if va > threshold and vb > threshold
    ]


def _generate_insight(
    query_dna: IncidentDNA,
    candidate: IncidentDNA,
    matching_dims: list[str],
    score: float,
) -> str:
    """Produce a concise, human-readable explanation for why two DNA profiles match."""
    if not matching_dims:
        return (
            f"Structural similarity (score={score:.2f}) despite no single dominant dimension — "
            "distributed low-level resource pressure pattern."
        )

    # Describe the shared dimensions in plain English
    labels = [_FEATURE_LABELS.get(d, d) for d in matching_dims]

    # Highlight cross-type matches (the novel insight)
    same_type = query_dna.incident_type == candidate.incident_type
    same_service = query_dna.service == candidate.service

    if not same_type:
        type_note = (
            f"Despite being classified as '{query_dna.incident_type}' vs "
            f"'{candidate.incident_type}', both show: "
        )
    elif not same_service:
        type_note = (
            f"Same '{query_dna.incident_type}' pattern across different services "
            f"('{query_dna.service}' and '{candidate.service}'): "
        )
    else:
        type_note = f"Repeated '{query_dna.incident_type}' pattern on '{query_dna.service}': "

    shared_str = ", ".join(labels[:4])  # cap at 4 for readability
    if len(labels) > 4:
        shared_str += f" and {len(labels) - 4} more shared signal(s)"

    return f"{type_note}{shared_str}."


def _safe_float(d: dict[str, Any], key: str, default: float) -> float:
    try:
        val = d.get(key, default)
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _safe_int(d: dict[str, Any], key: str, default: int) -> int:
    try:
        val = d.get(key, default)
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default
