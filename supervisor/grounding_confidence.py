"""Multi-dimensional grounding confidence scoring for SentinalAI.

Computes an operational confidence score from 10 evidence-grounded dimensions
and maps it to an explicit operational threshold classification.

Confidence States:
  VERIFIED_ROOT_CAUSE       >= 0.82  (direct entity + error + temporal + 2 sources, no contradictions)
  HIGH_CONFIDENCE_GROUNDED  >= 0.68
  PARTIALLY_GROUNDED        >= 0.42
  INVESTIGATION_CONTINUES   >= 0.18
  NO_OPERATIONAL_EVIDENCE   <  0.18

The 10 dimensions scored:
  1. entity_match            — Root cause names a specific entity found in evidence
  2. error_signature         — Known error patterns present in evidence
  3. temporal_proximity      — Evidence timestamps cluster near incident start
  4. dependency_alignment    — Topology/dependency path supports root cause
  5. change_correlation      — A recent change correlates with the incident
  6. topology_convergence    — Multi-hop CMDB/graph agrees on blast radius
  7. repeat_offender         — Entity has recurred (boosts only with operational evidence)
  8. multi_source            — DT + Splunk + topology + changes all present
  9. remediation_similarity  — Similar past incidents had matching fixes
  10. contradiction_penalty  — Contradictory evidence reduces the score

UNKNOWN-classified incidents are capped at HIGH_CONFIDENCE_GROUNDED.
Missing timestamps reduce but do not invalidate strong evidence.
All dimensions are optional — degrade gracefully when inputs are absent.

Configuration:
  GROUNDING_MODEL=v1|v2  — v1 uses legacy single-int confidence pass-through,
                            v2 applies full multi-dimensional scoring (default: v1)
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("sentinalai.grounding_confidence")

GROUNDING_MODEL = os.environ.get("GROUNDING_MODEL", "v2").lower()


# ---------------------------------------------------------------------------
# Operational confidence threshold classifications
# ---------------------------------------------------------------------------

class ConfidenceState(str, Enum):
    VERIFIED_ROOT_CAUSE      = "VERIFIED_ROOT_CAUSE"
    HIGH_CONFIDENCE_GROUNDED = "HIGH_CONFIDENCE_GROUNDED"
    PARTIALLY_GROUNDED       = "PARTIALLY_GROUNDED"
    INVESTIGATION_CONTINUES  = "INVESTIGATION_CONTINUES"
    NO_OPERATIONAL_EVIDENCE  = "NO_OPERATIONAL_EVIDENCE"


_THRESHOLDS = [
    (0.82, ConfidenceState.VERIFIED_ROOT_CAUSE),
    (0.68, ConfidenceState.HIGH_CONFIDENCE_GROUNDED),
    (0.42, ConfidenceState.PARTIALLY_GROUNDED),
    (0.18, ConfidenceState.INVESTIGATION_CONTINUES),
]


def classify_confidence(score: float) -> ConfidenceState:
    for threshold, state in _THRESHOLDS:
        if score >= threshold:
            return state
    return ConfidenceState.NO_OPERATIONAL_EVIDENCE


# ---------------------------------------------------------------------------
# VERIFIED_ROOT_CAUSE hard requirements
# ---------------------------------------------------------------------------

def _verified_requirements_met(dims: dict[str, float], source_count: int) -> bool:
    """VERIFIED requires: entity match + error signature + (temporal OR dependency)
    + at least 2 operational sources + no significant contradictions."""
    has_entity    = dims.get("entity_match", 0.0) >= 0.70
    has_error_sig = dims.get("error_signature", 0.0) >= 0.60
    has_temporal_or_dep = (
        dims.get("temporal_proximity", 0.0) >= 0.50
        or dims.get("dependency_alignment", 0.0) >= 0.50
    )
    no_contradictions = dims.get("contradiction_penalty", 1.0) >= 0.80
    enough_sources = source_count >= 2
    return has_entity and has_error_sig and has_temporal_or_dep and no_contradictions and enough_sources


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class GroundingResult:
    """Output of grounding confidence scoring."""
    score: float                                    # 0.0–1.0 composite score
    state: ConfidenceState                          # Operational threshold label
    dimensions: dict[str, float] = field(default_factory=dict)
    source_count: int = 0
    capped: bool = False                            # True when UNKNOWN cap applied
    cap_reason: str = ""
    missing_timestamp_penalty: float = 0.0
    model_version: str = "v1"

    @property
    def as_int(self) -> int:
        """Return score as 0–100 integer for backward compatibility."""
        return int(round(self.score * 100))

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "state": self.state.value,
            "dimensions": {k: round(v, 4) for k, v in self.dimensions.items()},
            "source_count": self.source_count,
            "capped": self.capped,
            "cap_reason": self.cap_reason,
            "missing_timestamp_penalty": round(self.missing_timestamp_penalty, 4),
            "model_version": self.model_version,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score(
    result: dict,
    evidence: dict,
    incident_type: str = "unknown",
    recurrence_info: Optional[dict] = None,
    topology_info: Optional[dict] = None,
    change_info: Optional[dict] = None,
) -> GroundingResult:
    """Compute grounding confidence for a completed investigation.

    Always returns a GroundingResult regardless of missing inputs — degrades
    gracefully when evidence / topology / recurrence are unavailable.

    Parameters
    ----------
    result:          The RCA result dict from _analyze_evidence()
    evidence:        Raw evidence dict from investigation
    incident_type:   Incident classification string (e.g. "oomkill", "timeout")
    recurrence_info: Optional output of recurrence_tracker.check() for the entity
    topology_info:   Optional topology/CMDB context dict
    change_info:     Optional ITSM change correlation dict
    """
    if GROUNDING_MODEL == "v1":
        return _passthrough_v1(result)

    return compute_confidence_v2(
        result=result,
        evidence=evidence,
        incident_type=incident_type,
        recurrence_info=recurrence_info,
        topology_info=topology_info,
        change_info=change_info,
    )


def validate_grounding_legacy(result: dict) -> GroundingResult:
    """v1 passthrough — legacy single-int confidence, no multi-dimensional scoring."""
    return _passthrough_v1(result)


def validate_grounding_v2(
    result: dict,
    evidence: dict,
    incident_type: str = "unknown",
    recurrence_info: Optional[dict] = None,
    topology_info: Optional[dict] = None,
    change_info: Optional[dict] = None,
) -> GroundingResult:
    """v2 full multi-dimensional grounding validation."""
    return compute_confidence_v2(
        result=result,
        evidence=evidence,
        incident_type=incident_type,
        recurrence_info=recurrence_info,
        topology_info=topology_info,
        change_info=change_info,
    )


def compute_confidence_legacy(raw_confidence: int) -> int:
    """v1: identity mapping — returns raw_confidence unchanged."""
    return raw_confidence


def compute_confidence_v2(
    result: dict,
    evidence: dict,
    incident_type: str = "unknown",
    recurrence_info: Optional[dict] = None,
    topology_info: Optional[dict] = None,
    change_info: Optional[dict] = None,
) -> GroundingResult:
    """Full 10-dimension grounding confidence scoring (v2).

    Weights sum to 1.0 (before contradiction penalty application).
    """
    root_cause = result.get("root_cause", "")
    raw_conf   = result.get("confidence", 0)
    timeline   = result.get("evidence_timeline", [])

    # Count operational evidence sources
    source_count = _count_sources(evidence)

    # Score each dimension
    dims: dict[str, float] = {}
    dims["entity_match"]          = _dim_entity_match(root_cause, evidence)
    dims["error_signature"]       = _dim_error_signature(root_cause, evidence, incident_type)
    dims["temporal_proximity"]    = _dim_temporal_proximity(evidence, timeline)
    dims["dependency_alignment"]  = _dim_dependency_alignment(root_cause, topology_info, evidence)
    dims["change_correlation"]    = _dim_change_correlation(change_info, evidence)
    dims["topology_convergence"]  = _dim_topology_convergence(topology_info, evidence)
    dims["repeat_offender"]       = _dim_repeat_offender(recurrence_info, source_count)
    dims["multi_source"]          = _dim_multi_source(source_count, evidence)
    dims["remediation_similarity"]= _dim_remediation_similarity(result, recurrence_info)
    dims["contradiction_penalty"] = _dim_contradiction_penalty(evidence, root_cause)

    # Missing timestamp penalty (reduces but does not invalidate)
    missing_ts_penalty = _missing_timestamp_penalty(evidence, timeline)

    # Weighted composite (contradiction_penalty is a multiplier, not additive)
    weights = {
        "entity_match":           0.18,
        "error_signature":        0.16,
        "temporal_proximity":     0.12,
        "dependency_alignment":   0.12,
        "change_correlation":     0.10,
        "topology_convergence":   0.08,
        "repeat_offender":        0.06,
        "multi_source":           0.10,
        "remediation_similarity": 0.08,
        # contradiction_penalty: applied as multiplier after weighted sum
    }

    raw_score = sum(weights[d] * dims[d] for d in weights)
    raw_score *= dims["contradiction_penalty"]   # [0.5–1.0] multiplier
    raw_score *= (1.0 - missing_ts_penalty)      # up to 10% timestamp penalty
    raw_score = round(min(1.0, max(0.0, raw_score)), 4)

    # Apply UNKNOWN classification cap
    capped = False
    cap_reason = ""
    if incident_type.lower() in ("unknown", "") and raw_score >= 0.68:
        raw_score = 0.679   # cap below HIGH_CONFIDENCE_GROUNDED boundary
        capped = True
        cap_reason = "UNKNOWN classification capped at HIGH_CONFIDENCE_GROUNDED"

    # Map to operational state
    state = classify_confidence(raw_score)

    # Enforce VERIFIED hard requirements — downgrade if not met
    if state == ConfidenceState.VERIFIED_ROOT_CAUSE:
        if not _verified_requirements_met(dims, source_count):
            raw_score = min(raw_score, 0.819)   # just below VERIFIED threshold
            state = ConfidenceState.HIGH_CONFIDENCE_GROUNDED
            logger.debug(
                "VERIFIED downgraded to HIGH_CONFIDENCE_GROUNDED: "
                "entity=%.2f err=%.2f temporal=%.2f dep=%.2f sources=%d contradiction=%.2f",
                dims["entity_match"], dims["error_signature"],
                dims["temporal_proximity"], dims["dependency_alignment"],
                source_count, dims["contradiction_penalty"],
            )

    logger.info(
        "Grounding v2: score=%.3f state=%s sources=%d entity=%.2f err=%.2f "
        "temporal=%.2f dep=%.2f multi=%.2f contradiction=%.2f capped=%s",
        raw_score, state.value, source_count,
        dims["entity_match"], dims["error_signature"],
        dims["temporal_proximity"], dims["dependency_alignment"],
        dims["multi_source"], dims["contradiction_penalty"], capped,
    )

    return GroundingResult(
        score=raw_score,
        state=state,
        dimensions=dims,
        source_count=source_count,
        capped=capped,
        cap_reason=cap_reason,
        missing_timestamp_penalty=missing_ts_penalty,
        model_version="v2",
    )


# ---------------------------------------------------------------------------
# v1 passthrough
# ---------------------------------------------------------------------------

def _passthrough_v1(result: dict) -> GroundingResult:
    conf = result.get("confidence", 0)
    score = round(min(1.0, max(0.0, conf / 100.0)), 4)
    state = classify_confidence(score)
    return GroundingResult(
        score=score,
        state=state,
        dimensions={},
        model_version="v1",
    )


# ---------------------------------------------------------------------------
# Dimension scorers
# ---------------------------------------------------------------------------

_ENTITY_PATTERNS = re.compile(
    r'\b(pod|node|service|deployment|container|host|db|database|namespace|'
    r'replica|shard|cluster|queue|topic|endpoint|cert|certificate|'
    r'connection.pool|thread.pool|api|gateway)\b',
    re.IGNORECASE,
)

_ERROR_PATTERNS = [
    # OOM
    (r'\bOOMKill(ed)?\b|\bout.of.memory\b|\bmemory.leak\b|\bmemory.exhaust', 0.9),
    # Connection pool
    (r'\bconnection.pool.exhaust|\bno.connections.available\b|\bpool.timeout\b', 0.85),
    # Timeout cascades
    (r'\btimeout\b|\bdeadline.exceeded\b|\bcontext.canceled\b', 0.70),
    # Auth / cert
    (r'\bcertificate.expir|\bhandshake.fail|\bssl.error\b|\bauth.fail|\bunauthorized\b', 0.85),
    # Disk / saturation
    (r'\bdisk.full|\bno.space.left\b|\bsaturation\b|\bqueue.depth\b', 0.80),
    # DNS
    (r'\bDNS.resolution.fail|\bname.resolution\b|\bNXDOMAIN\b', 0.80),
    # CPU throttle
    (r'\bcpu.throttl|\bcpu.limit\b|\bthrottle\b', 0.75),
    # Crash / restart
    (r'\bcrash.loop|\bCrashLoopBackOff\b|\brestart.count\b', 0.80),
    # Rate limit
    (r'\brate.limit|\btoo.many.requests\b|\b429\b', 0.70),
    # Deployment
    (r'\bdeploy\b|\brollback\b|\brelease\b', 0.55),
]

_COMPILED_ERROR_PATTERNS = [
    (re.compile(p, re.IGNORECASE), w) for p, w in _ERROR_PATTERNS
]


def _count_sources(evidence: dict) -> int:
    """Count distinct high-level source categories in evidence."""
    _GROUPS = {
        "logs":    ("search_logs", "get_error_logs", "search_error_logs",
                    "search_timeout_logs", "search_oom_logs", "search_latency_logs"),
        "metrics": ("query_metrics", "query_response_time", "query_error_rate",
                    "query_memory_metrics", "query_cpu_metrics"),
        "signals": ("get_golden_signals", "check_golden_signals", "get_apm_signals"),
        "events":  ("get_k8s_events", "get_events", "get_network_events"),
        "changes": ("get_change_data", "get_recent_deployments", "get_config_changes"),
    }
    found: set[str] = set()
    for ev_key in evidence:
        if ev_key.startswith("_"):
            continue
        for category, markers in _GROUPS.items():
            if any(m in ev_key for m in markers):
                found.add(category)
                break
    return len(found)


def _evidence_text(evidence: dict) -> str:
    """Flatten evidence values to a single string for pattern matching."""
    parts = []
    for k, v in evidence.items():
        if k.startswith("_"):
            continue
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, dict):
            parts.append(str(v))
        elif isinstance(v, list):
            parts.append(" ".join(str(x) for x in v[:50]))  # cap list size
    return " ".join(parts)


def _dim_entity_match(root_cause: str, evidence: dict) -> float:
    """Does the root cause name a specific entity found in evidence?"""
    if not root_cause or root_cause.startswith("INSUFFICIENT"):
        return 0.0
    if root_cause == "META_QUERY_NOT_INCIDENT":
        return 1.0

    # Extract entity names from root cause
    rc_entities = set(_ENTITY_PATTERNS.findall(root_cause.lower()))
    if not rc_entities:
        # Check for any proper nouns (capitalized multi-char tokens)
        proper = re.findall(r'\b[A-Z][a-z]{2,}\b', root_cause)
        if proper:
            rc_entities = {p.lower() for p in proper}

    if not rc_entities:
        return 0.20   # root cause exists but no named entities

    ev_text = _evidence_text(evidence).lower()
    if not ev_text:
        return 0.10

    matched = sum(1 for e in rc_entities if e in ev_text)
    ratio = matched / len(rc_entities)
    return min(1.0, 0.30 + ratio * 0.70)


def _dim_error_signature(root_cause: str, evidence: dict, incident_type: str) -> float:
    """Known error patterns present in evidence and/or root cause."""
    if not root_cause:
        return 0.0

    combined = root_cause + " " + _evidence_text(evidence)
    best = 0.0
    for pattern, weight in _COMPILED_ERROR_PATTERNS:
        if pattern.search(combined):
            best = max(best, weight)

    # Incident type bonus: generic type confirmation
    type_bonus = 0.0
    if incident_type.lower() not in ("unknown", ""):
        if incident_type.lower() in combined.lower():
            type_bonus = 0.10

    return min(1.0, best + type_bonus)


def _dim_temporal_proximity(evidence: dict, timeline: list) -> float:
    """Evidence timestamps cluster near incident start.

    Proxy: if timeline has entries with timestamps, they provide temporal grounding.
    Missing timestamps reduce score but don't make it zero.
    """
    if not timeline:
        # No timeline → can't confirm temporal alignment, but don't penalise twice
        # (missing_timestamp_penalty handles the reduction)
        return 0.30

    timestamped = sum(1 for e in timeline if isinstance(e, dict) and e.get("timestamp"))
    if timestamped == 0:
        return 0.20

    ratio = min(1.0, timestamped / max(1, len(timeline)))
    return round(0.30 + 0.70 * ratio, 3)


def _dim_dependency_alignment(
    root_cause: str,
    topology_info: Optional[dict],
    evidence: dict,
) -> float:
    """Topology/dependency path supports root cause."""
    if not topology_info:
        # Check evidence for upstream/downstream signals
        ev_text = _evidence_text(evidence).lower()
        dep_signals = ["upstream", "downstream", "dependency", "depends on", "called by",
                       "calls", "consumer", "producer", "client of"]
        hits = sum(1 for s in dep_signals if s in ev_text)
        return min(0.50, 0.10 + hits * 0.08)

    # topology_info provided — use blast radius / dependency chain
    dep_services = topology_info.get("dependency_chain", []) or topology_info.get("dependencies", [])
    root_cause_lower = root_cause.lower() if root_cause else ""
    if not dep_services:
        return 0.30

    # Check if root cause entity appears in dependency chain
    matched = any(
        (str(s).lower() in root_cause_lower or root_cause_lower in str(s).lower())
        for s in dep_services
    )
    if matched:
        return 0.85

    # Dependency chain present but root cause not in it
    chain_len = len(dep_services)
    return min(0.60, 0.30 + chain_len * 0.05)


def _dim_change_correlation(change_info: Optional[dict], evidence: dict) -> float:
    """A recent change correlates with the incident."""
    if not change_info:
        # Look for change signals in evidence
        ev_text = _evidence_text(evidence).lower()
        change_signals = ["deploy", "config change", "rollout", "release", "migration",
                          "restart", "certificate rotation", "password rotation"]
        hits = sum(1 for s in change_signals if s in ev_text)
        return min(0.50, hits * 0.12)

    corr_score = change_info.get("correlation_score", 0.0)
    if corr_score > 0.70:
        return 0.90
    if corr_score > 0.40:
        return 0.70
    if corr_score > 0.20:
        return 0.45
    return 0.10


def _dim_topology_convergence(topology_info: Optional[dict], evidence: dict) -> float:
    """Multi-hop CMDB/graph agrees on blast radius."""
    if not topology_info:
        return 0.20   # topology absent: neutral

    hop_count   = topology_info.get("hop_count", 0)
    ci_count    = topology_info.get("ci_count", 0) or topology_info.get("affected_ci_count", 0)
    risk_tier   = topology_info.get("risk_tier", "").upper()

    if ci_count == 0:
        return 0.20

    # More CIs in blast radius = more topology convergence evidence
    ci_score = min(0.60, 0.20 + ci_count * 0.05)
    hop_bonus = min(0.25, hop_count * 0.10)
    tier_bonus = {"CRITICAL": 0.15, "HIGH": 0.10, "MEDIUM": 0.05, "LOW": 0.0}.get(risk_tier, 0.0)
    return min(1.0, ci_score + hop_bonus + tier_bonus)


def _dim_repeat_offender(recurrence_info: Optional[dict], source_count: int) -> float:
    """Repeat offender — boost ONLY when operational evidence also exists.

    Per requirements: recurrence boosts confidence only after operational evidence
    is present; it does not substitute for it.
    """
    if not recurrence_info:
        return 0.0   # no recurrence data available

    if source_count < 1:
        return 0.0   # no operational evidence → no boost

    recurrence_count = recurrence_info.get("recurrence_count", 0)
    permanent_fix    = recurrence_info.get("permanent_fix_applied", False)

    if permanent_fix:
        # Permanent fix applied but recurring → strong signal
        return 0.70 if recurrence_count >= 2 else 0.50

    if recurrence_count >= 5:
        return 0.85
    if recurrence_count >= 3:
        return 0.70
    if recurrence_count >= 1:
        return 0.50
    return 0.0


def _dim_multi_source(source_count: int, evidence: dict) -> float:
    """DT + Splunk + topology + changes all present — multi-source convergence.

    Multi-source convergence should materially increase confidence.
    """
    # DT signals: golden_signals / apm
    has_dt      = any("golden_signals" in k or "apm" in k for k in evidence if not k.startswith("_"))
    # Splunk logs
    has_splunk  = any("logs" in k or "search_" in k for k in evidence if not k.startswith("_"))
    # Topology / CMDB
    has_topology= any("k8s" in k or "topology" in k or "cmdb" in k for k in evidence if not k.startswith("_"))
    # Changes
    has_changes = any("deploy" in k or "change" in k or "config" in k for k in evidence if not k.startswith("_"))

    combo_count = sum([has_dt, has_splunk, has_topology, has_changes])

    # Base from raw source count
    base = min(0.60, source_count * 0.15)
    # Bonus for each of the 4 key systems
    combo_bonus = combo_count * 0.10
    return min(1.0, base + combo_bonus)


def _dim_remediation_similarity(result: dict, recurrence_info: Optional[dict]) -> float:
    """Similar past incidents had matching fixes."""
    if not recurrence_info:
        return 0.0

    similar_fixes = recurrence_info.get("similar_remediation_count", 0)
    last_fix_worked = recurrence_info.get("last_fix_successful", None)

    if last_fix_worked is True and similar_fixes >= 2:
        return 0.85
    if similar_fixes >= 1:
        return 0.55
    return 0.0


def _dim_contradiction_penalty(evidence: dict, root_cause: str) -> float:
    """Contradictory evidence reduces confidence — returns a [0.5–1.0] multiplier.

    Looks for explicit contradiction signals: evidence marked as
    contradicting the stated root cause, or mutually inconsistent
    error signatures.
    """
    # Check for explicit contradiction markers in evidence
    contradiction_signals = ["_contradiction", "_conflicting", "_inconsistent"]
    contradictions = sum(
        1 for k in evidence
        if any(s in k.lower() for s in contradiction_signals)
    )

    # Check for mutually exclusive error signals in combined text
    ev_text = _evidence_text(evidence).lower()
    rc_lower = root_cause.lower() if root_cause else ""

    # Memory OOM root cause but no memory signals
    oom_claimed  = "oom" in rc_lower or "memory" in rc_lower
    oom_evidence = "oom" in ev_text or "memory" in ev_text
    timeout_claimed  = "timeout" in rc_lower
    timeout_evidence = "timeout" in ev_text

    silent_contradictions = 0
    if oom_claimed and not oom_evidence and ev_text:
        silent_contradictions += 1
    if timeout_claimed and not timeout_evidence and ev_text:
        silent_contradictions += 1

    total_contradictions = contradictions + silent_contradictions
    if total_contradictions == 0:
        return 1.0
    if total_contradictions == 1:
        return 0.80
    if total_contradictions == 2:
        return 0.65
    return 0.50


def _missing_timestamp_penalty(evidence: dict, timeline: list) -> float:
    """Fraction of penalty to apply for missing temporal grounding.

    Missing timestamps reduce confidence but do NOT invalidate strong evidence.
    Max penalty: 10%.
    """
    if timeline:
        return 0.0   # timeline present — no penalty

    # Check if evidence has any timestamp-bearing keys
    ts_keys = sum(
        1 for k, v in evidence.items()
        if not k.startswith("_") and isinstance(v, dict)
        and ("timestamp" in v or "time" in v or "ts" in v)
    )
    if ts_keys > 0:
        return 0.0

    # No timestamps at all
    total_keys = sum(1 for k in evidence if not k.startswith("_"))
    if total_keys == 0:
        return 0.05   # empty evidence — mild penalty
    return 0.07       # evidence present but no timestamps → 7% penalty
