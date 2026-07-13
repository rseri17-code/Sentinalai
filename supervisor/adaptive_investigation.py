"""Tranche 2 — Adaptive evidence acquisition advisor (shadow mode).

Answers, after every investigation and without any runtime authority:

  Which uncertainty matters most?          → per-hypothesis unknown map
  Which acquisition reduces it best?       → next-best-evidence ranking
  Is the investigation already decided?    → deterministic stop conditions
  When could collection have stopped?      → stop-point simulation over
                                             the evolved playbook order
  Do sources contradict each other?        → CrossSourceConflict + tie-break
  Is the causal ordering possible?         → temporal precedence check
  Was the incident misclassified?          → bounded reclassification advice

Reuses (Phase 0 reality scan — nothing new invented):
  planner_rules._capability_catalog   evidence yield / confidence gain / cost
  sentinel_core.strategy_optimizer.CostModel   expected-value scoring
  tool_selector.get_evolved_playbook  the order collection actually ran
  Tranche 1 belief-revision deltas    identical replay arithmetic

SHADOW CONTRACT: additive ``_adaptive_investigation`` metadata only.
Never touches root_cause/confidence/evidence routing. Flag OFF ⇒ result
untouched. Deterministic: sorted iteration, fixed thresholds, no clock.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Mapping

from supervisor.hypothesis_engine import (
    REFUTE_DELTA,
    SUPPORT_DELTA,
    _clamp,
    _tokens,
    select_disconfirmation_probe,
)

logger = logging.getLogger("sentinalai.adaptive_investigation")

# Deterministic stop thresholds (documented; tuned later from shadow data).
STOP_CONFIDENCE = 85
STOP_MARGIN = 25
MIN_REMAINING_EV = 0.02

# Change-shaped hypothesis detection (temporal causality applies).
_CHANGE_TOKENS = frozenset(
    {"deploy", "deployment", "rollback", "release", "commit", "change",
      "rollout", "merged"})

# Cross-source contradiction rules: (positive_key, negative_key, meaning,
# tie_break worker/action). A conflict fires when the positive source
# carries signal while the negative source claims health, or one of a
# pair errored while its sibling succeeded.
_CONTRADICTION_PAIRS: tuple[tuple[str, str, str, str], ...] = (
    ("search_error_logs", "check_golden_signals",
      "error logs show failures while golden signals report healthy",
      "metrics_worker.query_metrics"),
    ("search_oom_logs", "check_memory_metrics",
      "OOM log entries present while memory metrics look nominal",
      "metrics_worker.get_events"),
    ("search_network_logs", "get_network_alerts",
      "network errors in logs while no network alerts fired",
      "apm_worker.get_apm_traces"),
)

# Evidence-key signatures per incident type (from playbook labels) used
# by the bounded reclassification check.
_TYPE_SIGNATURES: dict[str, frozenset[str]] = {
    "oomkill": frozenset({"search_oom_logs", "check_memory_metrics",
                            "search_memory_logs"}),
    "timeout": frozenset({"search_timeout_logs", "check_latency_metrics"}),
    "latency": frozenset({"search_latency_logs", "check_latency_metrics",
                            "get_network_evidence"}),
    "network": frozenset({"search_network_logs", "get_network_alerts",
                            "get_network_evidence"}),
    "saturation": frozenset({"check_cpu_metrics", "search_cpu_logs"}),
    "error_spike": frozenset({"search_error_logs"}),
}

_ISO_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Phase 2 — Uncertainty model
# ---------------------------------------------------------------------------

def build_uncertainty_map(
    hypotheses_meta: list[dict[str, Any]],
    evidence_keys: set[str],
) -> dict[str, list[str]]:
    """Per-hypothesis unanswered questions: cited evidence not in hand,
    plus catalog evidence relevant to the hypothesis and still missing."""
    try:
        from supervisor.deterministic_planner.planner_rules import (
            _capability_catalog,
        )
        catalog = _capability_catalog()
    except Exception:
        catalog = {}

    out: dict[str, list[str]] = {}
    for h in sorted(hypotheses_meta or [],
                     key=lambda x: str(x.get("name", ""))):
        name = str(h.get("name", ""))
        if not name:
            continue
        refs = {str(r) for r in (h.get("evidence_refs") or ())}
        unknown = {r for r in refs if r not in evidence_keys}
        h_tokens = _tokens(name) | _tokens(h.get("root_cause", ""))
        for key in sorted(catalog):
            cap = catalog[key]
            # a capability is relevant when its yield overlaps the
            # hypothesis's cited refs or its description shares tokens
            relevant = (
                set(cap.typical_evidence_yield) & refs
                or (_tokens(cap.description) & h_tokens)
            )
            if relevant:
                unknown |= {e for e in cap.typical_evidence_yield
                             if e not in evidence_keys}
        out[name] = sorted(unknown)
    return out


# ---------------------------------------------------------------------------
# Phase 3 — Next Best Evidence (catalog + cost model + discrimination)
# ---------------------------------------------------------------------------

def select_next_evidence(
    incident_type: str,
    evidence_keys: set[str],
    uncertainty_map: Mapping[str, list[str]],
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """Ranked acquisitions: base expected value (existing probe selector's
    arithmetic) plus a discrimination bonus for evidence that appears in
    the unknowns of MORE THAN ONE competing hypothesis — the acquisition
    most likely to separate the differential."""
    try:
        from supervisor.deterministic_planner.planner_rules import (
            _capability_catalog,
        )
        from sentinel_core.strategy_optimizer import CostModel
    except Exception:
        return []

    # how many hypotheses each missing evidence key would inform
    demand: dict[str, int] = {}
    for unknowns in uncertainty_map.values():
        for key in unknowns:
            demand[key] = demand.get(key, 0) + 1

    model = CostModel()
    catalog = _capability_catalog()
    rows: list[tuple[tuple, dict[str, Any]]] = []
    for key in sorted(catalog):
        cap = catalog[key]
        missing = [e for e in cap.typical_evidence_yield
                   if e not in evidence_keys]
        if not missing:
            continue
        info_gain = len(missing) / max(1, len(cap.typical_evidence_yield))
        ev = model.overall_value(
            expected_information_gain=info_gain,
            expected_confidence_gain=int(cap.typical_confidence_gain),
            historical_success_rate=1.0,
            execution_cost=int(cap.typical_runtime_ms),
        )
        discrimination = sum(demand.get(e, 0) for e in missing)
        score = float(ev) + 0.05 * discrimination
        rows.append((
            (-score, str(cap.capability_id)),
            {"capability_id": str(cap.capability_id),
              "expected_value": round(float(ev), 4),
              "discrimination": discrimination,
              "score": round(score, 4),
              "missing_evidence": sorted(missing),
              "reduces_uncertainty_for": sorted(
                  h for h, unknowns in uncertainty_map.items()
                  if set(unknowns) & set(missing))},
        ))
    rows.sort(key=lambda r: r[0])
    return [r[1] for r in rows[:top_n]]


# ---------------------------------------------------------------------------
# Phase 5 — Deterministic stop conditions
# ---------------------------------------------------------------------------

def evaluate_stop(
    confidences: Mapping[str, int],
    survived_disconfirmation: bool | None,
    best_remaining_ev: float,
    budget_remaining: int | None,
) -> dict[str, Any]:
    """Stop when the investigation is decided. Pure and deterministic."""
    ranked = sorted(confidences.items(), key=lambda kv: (-kv[1], kv[0]))
    leader_conf = ranked[0][1] if ranked else 0
    margin = (leader_conf - ranked[1][1]) if len(ranked) > 1 else leader_conf

    reasons: list[str] = []
    if survived_disconfirmation:
        reasons.append("winner_survived_disconfirmation")
    if leader_conf >= STOP_CONFIDENCE:
        reasons.append(f"confidence>={STOP_CONFIDENCE}")
    if margin >= STOP_MARGIN:
        reasons.append(f"margin>={STOP_MARGIN}")
    if best_remaining_ev < MIN_REMAINING_EV:
        reasons.append("remaining_information_gain_below_cost")
    if budget_remaining is not None and budget_remaining <= 0:
        reasons.append("budget_exhausted")

    return {"should_stop": bool(reasons), "reasons": sorted(reasons),
            "leader_confidence": leader_conf, "margin": margin}


# ---------------------------------------------------------------------------
# Phase 9 — Stop-point simulation over the actual collection order
# ---------------------------------------------------------------------------

def simulate_stop_point(
    hypotheses_meta: list[dict[str, Any]],
    evidence: Mapping[str, Any],
    incident_type: str,
    steps: list[str] | None = None,
) -> dict[str, Any]:
    """Walk the evolved playbook order, replaying Tranche 1 belief
    arithmetic on the evidence each step ACTUALLY produced, and find the
    first step at which the stop conditions fire. Shadow measurement of
    'unnecessary worker calls eliminated'."""
    if steps is None:
        try:
            from supervisor.tool_selector import get_evolved_playbook
            steps = [str(s.get("label") or s.get("action", ""))
                     for s in get_evolved_playbook(incident_type)]
        except Exception:
            steps = []
    steps = [s for s in steps if s]
    if not steps or not hypotheses_meta:
        return {"steps_total": len(steps), "stop_at_step": None,
                 "unnecessary_calls": 0, "stop_reasons": []}

    cands = sorted(hypotheses_meta,
                    key=lambda x: (-float(x.get("score", 0)),
                                    str(x.get("name", ""))))
    conf = {str(c.get("name", "")): _clamp(int(float(c.get("score", 50))))
            for c in cands}
    refs = {str(c.get("name", "")): {str(r) for r in
             (c.get("evidence_refs") or ())} for c in cands}

    collected: set[str] = set()
    stop_at: int | None = None
    stop_reasons: list[str] = []
    for i, label in enumerate(steps, start=1):
        if label in evidence:
            collected.add(label)
            for name in sorted(conf):
                if label in refs[name]:
                    conf[name] = _clamp(conf[name] + SUPPORT_DELTA)
                elif any(label in refs[o] for o in refs if o != name):
                    conf[name] = _clamp(conf[name] - REFUTE_DELTA)
        remaining = [s for s in steps[i:] if s not in collected]
        # remaining EV proxy: any hypothesis-cited evidence still ahead?
        cited_ahead = any(
            s in r for s in remaining for r in refs.values()
        )
        verdict = evaluate_stop(
            conf, survived_disconfirmation=None,
            best_remaining_ev=(1.0 if cited_ahead else 0.0),
            budget_remaining=None,
        )
        if verdict["should_stop"]:
            stop_at = i
            stop_reasons = verdict["reasons"]
            break

    unnecessary = (len(steps) - stop_at) if stop_at else 0
    return {
        "steps_total": len(steps),
        "stop_at_step": stop_at,
        "stop_reasons": stop_reasons,
        "unnecessary_calls": unnecessary,
        "estimated_mtti_saving_pct": round(
            100.0 * unnecessary / len(steps), 1) if steps else 0.0,
    }


# ---------------------------------------------------------------------------
# Phase 6 — Contradiction detection (never silently merge)
# ---------------------------------------------------------------------------

def _has_signal(v: Any) -> bool:
    if not v:
        return False
    if isinstance(v, dict):
        if v.get("error"):
            return False
        return any(v.get(k) for k in v)
    return True


def detect_contradictions(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for pos, neg, meaning, tie_break in _CONTRADICTION_PAIRS:
        if pos in evidence and neg in evidence:
            pos_signal = _has_signal(evidence[pos])
            neg_v = evidence[neg]
            neg_healthy = isinstance(neg_v, dict) and not neg_v.get("error") \
                and not any(
                    str(k).lower().find(t) >= 0
                    for k in neg_v for t in ("breach", "alert", "violation"))
            if pos_signal and neg_healthy:
                conflicts.append({
                    "kind": "CrossSourceConflict",
                    "sources": [neg, pos],
                    "meaning": meaning,
                    "confidence_adjustment": -10,
                    "tie_break_recommendation": tie_break,
                })
    # generic: one source errored while its pair succeeded
    errored = sorted(k for k, v in evidence.items()
                      if isinstance(v, dict) and v.get("error")
                      and not k.startswith("_"))
    if errored:
        conflicts.append({
            "kind": "SourceReliabilityDifferential",
            "sources": errored,
            "meaning": "sources errored while siblings succeeded — their "
                        "absence must not be read as negative evidence",
            "confidence_adjustment": 0,
            "tie_break_recommendation": "",
        })
    return conflicts


# ---------------------------------------------------------------------------
# Phase 7 — Temporal causality
# ---------------------------------------------------------------------------

def _extract_timestamps(obj: Any, depth: int = 0) -> list[str]:
    if depth > 3:
        return []
    out: list[str] = []
    if isinstance(obj, str):
        out.extend(m.group(0) for m in _ISO_TS.finditer(obj))
    elif isinstance(obj, dict):
        for k in sorted(obj):
            out.extend(_extract_timestamps(obj[k], depth + 1))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_extract_timestamps(v, depth + 1))
    return out


def check_temporal_causality(
    hypotheses_meta: list[dict[str, Any]],
    evidence: Mapping[str, Any],
    symptom_time: str,
) -> list[dict[str, Any]]:
    """A change cannot explain a symptom that predates it. For every
    change-shaped hypothesis, compare the EARLIEST change timestamp in
    change evidence against the symptom time (lexicographic ISO)."""
    if not symptom_time:
        return []
    change_evidence = {
        k: v for k, v in evidence.items()
        if any(t in str(k).lower()
                for t in ("deploy", "change", "diff", "commit"))
    }
    change_times = sorted(
        ts for v in change_evidence.values()
        for ts in _extract_timestamps(v)
    )
    if not change_times:
        return []
    earliest_change = change_times[0]

    demotions = []
    for h in sorted(hypotheses_meta or [],
                     key=lambda x: str(x.get("name", ""))):
        name = str(h.get("name", ""))
        if not (_tokens(name) | _tokens(h.get("root_cause", ""))) \
                & _CHANGE_TOKENS:
            continue
        if earliest_change > str(symptom_time)[:16]:
            demotions.append({
                "hypothesis": name,
                "recommendation": "demote",
                "reason": "rejected because causal ordering impossible: "
                           f"earliest change {earliest_change} is after "
                           f"symptom onset {str(symptom_time)[:16]}",
            })
    return demotions


# ---------------------------------------------------------------------------
# Phase 8 — Bounded reclassification advice
# ---------------------------------------------------------------------------

def recommend_reclassification(
    incident_type: str, evidence_keys: set[str],
) -> dict[str, Any] | None:
    """One bounded recommendation when the evidence profile contradicts
    the classified type: zero own-signature hits AND >=2 hits for
    exactly one other type."""
    own = _TYPE_SIGNATURES.get(incident_type, frozenset())
    if own & evidence_keys:
        return None
    candidates = sorted(
        (t, len(sig & evidence_keys))
        for t, sig in _TYPE_SIGNATURES.items()
        if t != incident_type and len(sig & evidence_keys) >= 2
    )
    if not candidates:
        return None
    best_type, hits = max(candidates, key=lambda c: (c[1], c[0]))
    return {
        "from_type": incident_type, "to_type": best_type,
        "signature_hits": hits, "bounded": "single reclassification only",
        "reason": (f"no {incident_type!r} signature evidence collected; "
                    f"{hits} {best_type!r} signature keys present"),
    }


# ---------------------------------------------------------------------------
# Advisor entry point (shadow)
# ---------------------------------------------------------------------------

def run_adaptive_advisor(
    result: dict[str, Any],
    evidence: dict[str, Any],
    incident_type: str,
    hypotheses_meta: list[dict[str, Any]] | None = None,
    symptom_time: str = "",
    budget: Any = None,
) -> None:
    """Attach ``_adaptive_investigation`` shadow recommendations.

    No-op unless ADAPTIVE_INVESTIGATION_ENABLED. Never raises. Never
    changes root_cause / confidence / evidence routing.
    """
    if not _flag("ADAPTIVE_INVESTIGATION_ENABLED"):
        return
    try:
        meta = hypotheses_meta or []
        evidence_keys = {k for k in evidence if not str(k).startswith("_")}

        uncertainty = build_uncertainty_map(meta, evidence_keys)
        next_best = select_next_evidence(incident_type, evidence_keys,
                                           uncertainty)
        best_ev = next_best[0]["expected_value"] if next_best else 0.0

        narrative = result.get("_elimination_narrative") or {}
        graph = result.get("_hypothesis_graph") or {}
        confidences = {
            str(h.get("name", "")): int(h.get("confidence", 0))
            for h in graph.get("hypotheses", [])
        } or {str(h.get("name", "")): _clamp(int(float(h.get("score", 50))))
               for h in meta}

        budget_remaining = None
        try:
            if budget is not None:
                budget_remaining = int(budget.remaining())
        except Exception:
            budget_remaining = None

        result["_adaptive_investigation"] = {
            "uncertainty_map": uncertainty,
            "next_best_evidence": next_best,
            "stop": evaluate_stop(
                confidences,
                narrative.get("survived_disconfirmation"),
                best_ev, budget_remaining,
            ),
            "stop_point_simulation": simulate_stop_point(
                meta, evidence, incident_type),
            "contradictions": detect_contradictions(evidence),
            "temporal_causality": check_temporal_causality(
                meta, evidence, symptom_time),
            "reclassification": recommend_reclassification(
                incident_type, evidence_keys),
        }
    except Exception as exc:
        logger.warning(
            "adaptive_investigation.failed error_type=%s error=%s",
            type(exc).__name__, exc,
        )


__all__ = [
    "build_uncertainty_map", "check_temporal_causality",
    "detect_contradictions", "evaluate_stop",
    "recommend_reclassification", "run_adaptive_advisor",
    "select_next_evidence", "simulate_stop_point",
]
