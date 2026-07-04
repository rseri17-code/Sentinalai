"""DecisionContext — deterministic operational recommendations derived
from an IntelligenceContext.

This is Sentinel's first Decision Intelligence layer: a pure, testable
transform from persisted intelligence into structured operational
decisions. No LLM. No natural-language generation. No new store.

Design principles
-----------------
- **Deterministic**: same IntelligenceContext → byte-identical
  DecisionContext. Every mapping is a closed-form expression over the
  input fields; no randomness, no timestamps injected.
- **Bounded**: every field has a fixed shape and a fixed upper bound.
  Nothing here grows with corpus size beyond what IntelligenceContext
  already caps.
- **Frozen**: safe to pass across module boundaries, safe to serialize.
- **Missing-tolerant**: an empty IntelligenceContext produces a valid
  DecisionContext with sensible defaults (low priority, low confidence,
  empty recommendations). Callers never crash on an empty corpus.

Field semantics
---------------
- ``confidence`` (0-100): a rule-based aggregation of how well the
  accumulated intelligence supports acting on this incident.
- ``likely_failure_type``: the classification's incident_type, with a
  preference for the top recurring pattern's incident_type when one
  exists.
- ``likely_blast_radius`` ({severity, total_affected, top_service}): a
  compact projection of the CausalGraph read.
- ``recurring_incident``: True iff any pattern has occurrence_count >= 2.
- ``historical_success_rate``: max success_rate across pattern matches
  (0.0 when no patterns).
- ``recommended_investigation_order``: a tuple of stable string tokens
  ordered by which intelligence signals are populated.
- ``recommended_next_service``: the top-strength downstream service if
  any dependency exists, else "".
- ``recommended_queries``: a tuple of stable query-name tokens that
  future guided-investigation consumers can dispatch on.
- ``confidence_adjustments``: an ordered list of {reason, delta} pairs
  whose deltas sum (with a +50 base) to ``confidence``. Explainable.
- ``evidence_gaps``: tuple of intelligence-module names that produced
  ZERO signals during POST_CLASSIFY (i.e., silent gaps in the corpus).
- ``investigation_priority``: "critical" | "high" | "medium" | "low"
  derived from blast_radius severity + affected count + recurring signal.

Every field is derived by the ``from_intelligence_context`` factory
below. See the tests in ``tests/test_decision_context.py`` for the
exact mapping table.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# The 6 read modules Decision Intelligence expects to see. If any is
# absent from IntelligenceContext.module_names_seen we treat it as an
# evidence gap.
# ---------------------------------------------------------------------------

_EXPECTED_MODULES: tuple[str, ...] = (
    "historical_lookup",
    "pattern_recognition",
    "incident_graph_lookup",
    "dependency_graph_lookup",
    "episodic_memory_lookup",
    "causal_graph_lookup",
)


# ---------------------------------------------------------------------------
# Compact projections used by DecisionContext (avoids re-serializing the
# full IntelligenceContext tuples).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BlastRadiusProjection:
    severity:       str = "low"
    total_affected: int = 0
    top_service:    str = ""    # highest-probability affected service, "" if none


@dataclass(frozen=True)
class ConfidenceAdjustment:
    reason: str
    delta:  int


# ---------------------------------------------------------------------------
# DecisionContext — canonical operational-decision object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DecisionContext:
    # Identity — mirrors IntelligenceContext for correlation
    service:       str = ""
    incident_type: str = ""

    # Core signals
    confidence:              int = 50
    likely_failure_type:     str = ""
    likely_blast_radius:     BlastRadiusProjection = field(default_factory=BlastRadiusProjection)
    recurring_incident:      bool = False
    historical_success_rate: float = 0.0

    # Recommendations
    recommended_investigation_order: tuple[str, ...] = ()
    recommended_next_service:        str = ""
    recommended_queries:             tuple[str, ...] = ()

    # Explainability
    confidence_adjustments: tuple[ConfidenceAdjustment, ...] = ()
    evidence_gaps:          tuple[str, ...] = ()

    # Overall priority
    investigation_priority: str = "low"

    # Provenance
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict rendering. Tuples become lists so the payload is
        symmetric with what json.dumps + json.loads would round-trip.

        ``dataclasses.asdict`` preserves the tuple type on tuple fields —
        we walk it once here to normalize to lists so the payload is
        stable across Python versions and the runtime's ModuleResult
        can be compared to a JSON round-trip byte-for-byte.
        """
        return _tuples_to_lists(asdict(self))

    def is_empty(self) -> bool:
        """True iff no meaningful signals informed this decision.

        The terminal ``collect_evidence`` step in
        ``recommended_investigation_order`` is always present so its
        appearance alone does not defeat emptiness — only additional
        entries do.
        """
        return (
            not self.service
            and not self.incident_type
            and self.confidence == 50
            and not self.recurring_incident
            and self.historical_success_rate == 0.0
            and self.recommended_investigation_order == ("collect_evidence",)
            and not self.recommended_next_service
            and not self.recommended_queries
        )

    # ------------------------------------------------------------------
    # Factory — deterministic transform
    # ------------------------------------------------------------------

    @classmethod
    def from_intelligence_context(cls, ic) -> "DecisionContext":
        """Deterministic transform. Same input → identical output.

        ``ic`` is expected to be a
        ``sentinel_core.models.intel_context.IntelligenceContext``.
        Non-dataclass duck-typing is tolerated for robustness (any object
        with matching attribute names works).
        """
        service = str(_getattr_safe(ic, "service", "") or "")
        incident_type = str(_getattr_safe(ic, "incident_type", "") or "")

        rm_matches      = _as_tuple(_getattr_safe(ic, "resolution_memory_matches", ()))
        inv_matches     = _as_tuple(_getattr_safe(ic, "investigation_matches", ()))
        pattern_matches = _as_tuple(_getattr_safe(ic, "pattern_matches", ()))
        related_ids     = _as_tuple(_getattr_safe(ic, "related_incident_ids", ()))
        upstream        = _as_tuple(_getattr_safe(ic, "upstream_dependencies", ()))
        downstream      = _as_tuple(_getattr_safe(ic, "downstream_dependents", ()))
        affected        = _as_tuple(_getattr_safe(ic, "affected_services", ()))
        episodes        = _as_tuple(_getattr_safe(ic, "episode_matches", ()))
        blast_severity  = str(_getattr_safe(ic, "blast_radius_severity", "low") or "low")
        blast_total     = _as_int(_getattr_safe(ic, "blast_radius_total_affected", 0))
        blast_affected  = _as_tuple(_getattr_safe(ic, "blast_radius_affected", ()))
        module_names    = _as_tuple(_getattr_safe(ic, "module_names_seen", ()))

        # --- Recurring + top pattern ---------------------------------
        top_pattern = None
        for p in pattern_matches:
            occ = int(_getattr_safe(p, "occurrence_count", 0) or 0)
            if occ >= 2:
                if top_pattern is None or occ > int(_getattr_safe(top_pattern,
                                                                    "occurrence_count", 0) or 0):
                    top_pattern = p
        recurring_incident = top_pattern is not None

        # --- historical_success_rate = max pattern success_rate ------
        historical_success_rate = 0.0
        for p in pattern_matches:
            r = float(_getattr_safe(p, "success_rate", 0.0) or 0.0)
            if r > historical_success_rate:
                historical_success_rate = r
        historical_success_rate = round(historical_success_rate, 3)

        # --- likely_failure_type -------------------------------------
        # Prefer the top recurring pattern's incident_type; else use
        # classification's incident_type; else "".
        if top_pattern is not None:
            likely_failure_type = str(_getattr_safe(top_pattern, "incident_type", "") or "") or incident_type
        else:
            likely_failure_type = incident_type

        # --- likely_blast_radius -------------------------------------
        top_service = ""
        if blast_affected:
            first = blast_affected[0]
            top_service = str(_getattr_safe(first, "service_id", "") or "")
        blast_radius = BlastRadiusProjection(
            severity=blast_severity,
            total_affected=blast_total,
            top_service=top_service,
        )

        # --- recommended_next_service --------------------------------
        # Highest-strength downstream edge's source_service (the caller
        # depending on us) — the operator's most useful "check this next".
        recommended_next_service = ""
        if downstream:
            best = max(downstream,
                        key=lambda e: float(_getattr_safe(e, "strength", 0.0) or 0.0))
            recommended_next_service = str(_getattr_safe(best, "source_service", "") or "")
        elif affected:
            recommended_next_service = str(affected[0] or "")

        # --- recommended_investigation_order -------------------------
        # Ordered, deterministic sequence of investigative "steps"
        # keyed to which signals fired. Fixed tokens so downstream
        # consumers can pattern-match.
        order: list[str] = []
        if upstream:
            order.append("check_upstream_dependencies")
        if recurring_incident:
            order.append("compare_recurring_pattern")
        if rm_matches:
            order.append("review_prior_resolutions")
        if episodes:
            order.append("recall_prior_episodes")
        if related_ids:
            order.append("inspect_related_incidents")
        if blast_severity in ("high", "critical") or blast_total >= 2:
            order.append("assess_blast_radius")
        order.append("collect_evidence")  # always the terminal step
        recommended_investigation_order = tuple(order)

        # --- recommended_queries -------------------------------------
        queries: list[str] = []
        if service:
            queries.append("logs_for_service")
        if upstream:
            queries.append("upstream_service_health")
        if downstream:
            queries.append("downstream_service_health")
        if rm_matches:
            queries.append("prior_resolution_actions")
        if recurring_incident:
            queries.append("pattern_playbook")
        if blast_severity in ("high", "critical"):
            queries.append("blast_radius_dashboard")
        recommended_queries = tuple(queries)

        # --- confidence + adjustments --------------------------------
        adjustments: list[ConfidenceAdjustment] = []
        base = 50
        if rm_matches:
            adjustments.append(ConfidenceAdjustment(
                reason="have_prior_resolution_memory", delta=15,
            ))
        if inv_matches:
            adjustments.append(ConfidenceAdjustment(
                reason="have_prior_investigations", delta=5,
            ))
        if recurring_incident:
            adjustments.append(ConfidenceAdjustment(
                reason="recurring_pattern_seen", delta=10,
            ))
        if historical_success_rate >= 0.5:
            adjustments.append(ConfidenceAdjustment(
                reason="high_historical_success_rate", delta=10,
            ))
        if episodes:
            adjustments.append(ConfidenceAdjustment(
                reason="have_prior_episodes", delta=5,
            ))
        if related_ids:
            adjustments.append(ConfidenceAdjustment(
                reason="have_related_incidents", delta=3,
            ))
        if blast_severity == "critical":
            adjustments.append(ConfidenceAdjustment(
                reason="critical_blast_radius_hint", delta=2,
            ))
        confidence = base + sum(a.delta for a in adjustments)
        confidence = max(0, min(100, confidence))

        # --- evidence_gaps -------------------------------------------
        seen = set(module_names)
        evidence_gaps = tuple(m for m in _EXPECTED_MODULES if m not in seen)

        # --- investigation_priority ----------------------------------
        if blast_severity == "critical" or blast_total >= 5:
            priority = "critical"
        elif blast_severity == "high" or (recurring_incident
                                            and historical_success_rate < 0.5):
            priority = "high"
        elif (rm_matches or inv_matches or pattern_matches
                or related_ids or episodes or blast_total > 0):
            priority = "medium"
        else:
            priority = "low"

        return cls(
            service=service,
            incident_type=incident_type,
            confidence=confidence,
            likely_failure_type=likely_failure_type,
            likely_blast_radius=blast_radius,
            recurring_incident=recurring_incident,
            historical_success_rate=historical_success_rate,
            recommended_investigation_order=recommended_investigation_order,
            recommended_next_service=recommended_next_service,
            recommended_queries=recommended_queries,
            confidence_adjustments=tuple(adjustments),
            evidence_gaps=evidence_gaps,
            investigation_priority=priority,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _getattr_safe(obj: Any, name: str, default: Any = None) -> Any:
    """getattr that tolerates dict-like objects too."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_tuple(value: Any) -> tuple:
    """Coerce any iterable to a tuple. Non-iterable / None → ()."""
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, (list, set)):
        return tuple(value)
    # Strings are iterable but we don't want per-character explosion.
    if isinstance(value, (str, bytes)):
        return ()
    try:
        return tuple(value)
    except Exception:
        return ()


def _tuples_to_lists(obj: Any) -> Any:
    """Recursively convert every tuple to a list. Leaves other types alone."""
    if isinstance(obj, tuple):
        return [_tuples_to_lists(v) for v in obj]
    if isinstance(obj, list):
        return [_tuples_to_lists(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _tuples_to_lists(v) for k, v in obj.items()}
    return obj


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce any value to int. Uncoercible → default. Never raises."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "DecisionContext",
    "BlastRadiusProjection",
    "ConfidenceAdjustment",
]
