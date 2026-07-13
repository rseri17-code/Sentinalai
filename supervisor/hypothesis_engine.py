"""Tranche 1 — Hypothesis-centric investigation engine (shadow mode).

Converts AnalyzePhase's discarded ranked differential into a full
investigative record using ONLY existing reasoning components:

  sentinel_core.hypotheses.HypothesisTracker   lifecycle + transitions
  sentinel_core.hypotheses.scoring             net support/refute
  deterministic_planner._capability_catalog    refutation-probe targets
  sentinel_core.strategy_optimizer.CostModel   expected-value ranking

Reasoning flow (mission Phase 3-5):
  propose competing hypotheses (LLM differential + prior suggestions)
  → attach supporting evidence, revising confidence per event
  → offline disconfirmation: rival-cited evidence and known false-lead
    overlap refute the leader (differential-diagnosis semantics)
  → select the highest-EV refutation probe from the capability catalog
    (optionally execute ONE bounded fetch — separate flag, budget-gated)
  → confirm the survivor, rule out the rest with explicit reasons
  → attach graph + elimination narrative + counterfactual to the result.

SHADOW AUTHORITY CONTRACT: this engine NEVER changes ``root_cause`` or
``confidence``. It adds underscore-metadata keys only. Flag OFF ⇒ the
result dict is untouched, byte-identical to today.

Flags (default OFF):
  HYPOTHESIS_ENGINE_ENABLED          run the engine, enrich the result
  HYPOTHESIS_DISCONFIRMATION_FETCH   additionally execute one bounded
                                     worker probe (requires sup+budget)

Deterministic: sorted iteration everywhere, fixed confidence deltas,
no timestamps, no randomness. Never raises past its own boundary.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Mapping

logger = logging.getLogger("sentinalai.hypothesis_engine")

# Fixed, documented belief-revision deltas (deterministic + explainable).
SUPPORT_DELTA = 7
REFUTE_DELTA = 9
MAX_COMPETING_REFUTATIONS = 3
MAX_PRIOR_CANDIDATES = 3
PRIOR_INITIAL_CONFIDENCE = 30
# Minimum worker-budget remaining before a live probe is allowed.
PROBE_MIN_BUDGET = 2

# Deterministic capability → (worker, action) map for the live probe.
# Only well-known, read-only actions; anything unmapped is recommend-only.
_PROBE_EXECUTION: dict[str, tuple[str, str]] = {
    "cap:collect_pod_lifecycle":    ("event_worker", "get_k8s_events"),
    "cap:collect_deploy_history":   ("change_worker", "get_recent_deployments"),
    "cap:collect_error_logs":       ("log_worker", "search_error_logs"),
    "cap:collect_golden_signals":   ("signal_worker", "get_golden_signals"),
    "cap:collect_resource_metrics": ("metrics_worker", "query_metrics"),
}


def _flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def _tokens(s: str) -> set[str]:
    out = set()
    for tok in str(s or "").lower().split():
        t = tok.strip(".,;:()[]!?\"'`<>-")
        if len(t) >= 3:
            out.add(t)
    return out


def _clamp(v: int) -> int:
    return max(0, min(100, int(v)))


# ---------------------------------------------------------------------------
# Candidate assembly
# ---------------------------------------------------------------------------

def _candidates(
    hypotheses_meta: list[dict[str, Any]],
    evidence: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """LLM differential (primary) + prior-suggested causes (secondary),
    deduplicated by token overlap, deterministically ordered."""
    cands: list[dict[str, Any]] = []
    seen_tokens: list[set[str]] = []

    for h in sorted(hypotheses_meta or [],
                     key=lambda x: (-float(x.get("score", 0)),
                                     str(x.get("name", "")))):
        name = str(h.get("name", "") or h.get("root_cause", ""))
        if not name:
            continue
        cands.append({
            "name": name,
            "description": str(h.get("root_cause", "")),
            "confidence": _clamp(int(float(h.get("score", 50)))),
            "evidence_refs": sorted(str(r) for r in
                                     (h.get("evidence_refs") or ())),
            "origin": "differential",
        })
        seen_tokens.append(_tokens(name) | _tokens(h.get("root_cause", "")))

    # Prior suggestions (experience/KG futures) become low-confidence
    # candidates — the human "ranked differential from the pager text".
    suggested = evidence.get("_suggested_root_causes") or []
    added = 0
    for s in suggested:
        if added >= MAX_PRIOR_CANDIDATES:
            break
        text = str(s.get("cause") if isinstance(s, dict) else s)
        toks = _tokens(text)
        if not toks:
            continue
        if any(len(toks & prev) / max(1, len(toks | prev)) >= 0.5
                for prev in seen_tokens):
            continue                      # duplicate of an existing candidate
        cands.append({
            "name": text, "description": text,
            "confidence": PRIOR_INITIAL_CONFIDENCE,
            "evidence_refs": [], "origin": "prior",
        })
        seen_tokens.append(toks)
        added += 1
    return cands


# ---------------------------------------------------------------------------
# Refutation-probe selection (capability catalog + cost model — pure)
# ---------------------------------------------------------------------------

def select_disconfirmation_probe(
    incident_type: str, evidence_keys: set[str],
    allowed: set[str] | None = None,
) -> dict[str, Any] | None:
    """Highest expected-value capability whose evidence is NOT in hand.

    Reuses the deterministic planner's capability catalog and the
    strategy optimizer's cost model. Ties break on capability_id.
    """
    try:
        from supervisor.deterministic_planner.planner_rules import (
            _capability_catalog,
        )
        from sentinel_core.strategy_optimizer import CostModel
    except Exception:
        return None

    model = CostModel()
    catalog = _capability_catalog()
    best: tuple[tuple, str, Any] | None = None
    for key in sorted(catalog):
        cap = catalog[key]
        cap_id = str(cap.capability_id)
        if allowed is not None and cap_id not in allowed:
            continue
        missing = [e for e in cap.typical_evidence_yield
                   if e not in evidence_keys]
        if not missing:
            continue                       # nothing new to learn from it
        info_gain = len(missing) / max(1, len(cap.typical_evidence_yield))
        ev = model.overall_value(
            expected_information_gain=info_gain,
            expected_confidence_gain=int(cap.typical_confidence_gain),
            historical_success_rate=1.0,   # optimistic prior — no corpus read
            execution_cost=int(cap.typical_runtime_ms),
        )
        # Rank: cost-model EV, then raw gain, then cheaper runtime,
        # then capability_id — fully deterministic even when EV ties.
        rank = (float(ev),
                info_gain * cap.typical_confidence_gain,
                -int(cap.typical_runtime_ms))
        if best is None or rank > best[0]:
            best = (rank, cap_id, cap)

    if best is None:
        return None
    rank, cap_id, cap = best
    ev = rank[0]
    return {
        "capability_id": cap_id,
        "expected_value": round(float(ev), 4),
        "missing_evidence": sorted(
            e for e in cap.typical_evidence_yield if e not in evidence_keys
        ),
        "reason": (
            "highest expected-value acquisition whose evidence is absent; "
            "outcome would most strongly confirm or refute the leader"
        ),
    }


def _execute_probe(
    probe: Mapping[str, Any], sup: Any, budget: Any,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """ONE bounded live fetch (flag-gated). Never raises."""
    outcome = {"executed": False, "evidence_key": "", "empty": True}
    try:
        if budget is None or budget.remaining() < PROBE_MIN_BUDGET:
            outcome["skip_reason"] = "budget"
            return outcome
        mapping = _PROBE_EXECUTION.get(str(probe.get("capability_id", "")))
        if not mapping or sup is None:
            outcome["skip_reason"] = "no_execution_mapping"
            return outcome
        worker_name, action = mapping
        worker = getattr(sup, "workers", {}).get(worker_name)
        if worker is None:
            outcome["skip_reason"] = "worker_unavailable"
            return outcome
        fetched = worker.execute(action, {})
        key = f"disconfirm_{probe['capability_id'].replace('cap:', '')}"
        evidence[key] = fetched
        outcome.update({
            "executed": True,
            "evidence_key": key,
            "empty": not fetched or bool(
                isinstance(fetched, dict) and fetched.get("error")),
        })
    except Exception as exc:
        outcome["skip_reason"] = f"{type(exc).__name__}"
    return outcome


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def run_hypothesis_engine(
    result: dict[str, Any],
    evidence: dict[str, Any],
    incident_type: str,
    hypotheses_meta: list[dict[str, Any]] | None = None,
    sup: Any = None,
    budget: Any = None,
) -> None:
    """Run the reasoning engine; enrich ``result`` additively.

    No-op unless HYPOTHESIS_ENGINE_ENABLED. Never raises. Never touches
    root_cause / confidence (shadow authority contract).
    """
    if not _flag("HYPOTHESIS_ENGINE_ENABLED"):
        return
    try:
        _run(result, evidence, incident_type,
              hypotheses_meta or [], sup, budget)
    except Exception as exc:
        logger.warning(
            "hypothesis_engine.failed error_type=%s error=%s",
            type(exc).__name__, exc,
        )


def _run(result, evidence, incident_type, hypotheses_meta, sup, budget):
    from sentinel_core.hypotheses import HypothesisTracker
    from sentinel_core.hypotheses.scoring import score_hypothesis_graph

    cands = _candidates(hypotheses_meta, evidence)
    if not cands:
        # Fall back to the single reported root cause as one hypothesis
        rc = str(result.get("root_cause", ""))
        if not rc:
            return
        cands = [{"name": rc, "description": rc,
                   "confidence": _clamp(int(result.get("confidence", 50))),
                   "evidence_refs": [], "origin": "reported"}]

    tracker = HypothesisTracker(
        investigation_id=str(result.get("incident_id", "")))
    ids: dict[str, str] = {}
    for c in cands:
        h = tracker.propose(c["name"], description=c["description"],
                             initial_confidence=c["confidence"])
        ids[c["name"]] = h.hypothesis_id

    # The analysis's initial favorite — 'survived_disconfirmation' asks
    # whether THIS hypothesis (not a post-evidence leader) held through
    # belief revision + disconfirmation.
    initial_leader = sorted(
        tracker._by_id.values(),
        key=lambda h: (-h.confidence, h.hypothesis_id),
    )[0].hypothesis_id

    # ── Belief revision: supporting evidence, one transition per event
    for c in cands:
        hid = ids[c["name"]]
        for key in c["evidence_refs"]:
            if key not in evidence:
                continue
            tracker.add_supporting_evidence(hid, key, weight=1.0,
                                              reason="cited_by_analysis")
            cur = tracker._by_id[hid].confidence      # deterministic read
            tracker.transition(hid, "supported",
                                new_confidence=_clamp(cur + SUPPORT_DELTA),
                                reason=f"support:{key}")

    # ── Leader before disconfirmation (confidence desc, hypothesis_id asc)
    def _leader() -> str:
        return sorted(
            tracker._by_id.values(),
            key=lambda h: (-h.confidence, h.hypothesis_id),
        )[0].hypothesis_id

    leader_before = initial_leader
    leader_name_before = tracker._by_id[leader_before].name

    # ── Offline disconfirmation 1: competing-explanation evidence.
    # Evidence cited EXCLUSIVELY by rivals argues that a different
    # explanation fits the data (differential diagnosis). Applied
    # SYMMETRICALLY to every hypothesis — refuting only the leader
    # would hand victory to weaker rivals by construction.
    refs_by_id = {
        ids[c["name"]]: {k for k in c["evidence_refs"] if k in evidence}
        for c in cands
    }
    for c in sorted(cands, key=lambda x: str(x["name"])):
        hid = ids[c["name"]]
        own = refs_by_id[hid]
        rival_only = sorted({
            key for other_id, refs in refs_by_id.items()
            if other_id != hid for key in refs if key not in own
        })[:MAX_COMPETING_REFUTATIONS]
        for key in rival_only:
            tracker.add_refuting_evidence(hid, key, weight=0.5,
                                            reason="competing_explanation")
            cur = tracker._by_id[hid].confidence
            tracker.transition(hid, "supported",
                                new_confidence=_clamp(cur - REFUTE_DELTA),
                                reason=f"refute:competing:{key}")

    # ── Offline disconfirmation 2: known false-lead overlap.
    gaps = (result.get("_critique") or {}).get("gaps") or []
    gap_tokens = set()
    for g in gaps:
        gap_tokens |= _tokens(g if isinstance(g, str) else str(g))
    if gap_tokens & _tokens(leader_name_before):
        tracker.add_refuting_evidence(
            leader_before, "critique_gap_overlap", weight=0.5,
            reason="known_false_lead_overlap")
        cur = tracker._by_id[leader_before].confidence
        tracker.transition(leader_before, "supported",
                            new_confidence=_clamp(cur - REFUTE_DELTA),
                            reason="refute:false_lead_overlap")

    # ── Refutation probe: selection is always recorded; execution is a
    # separate flag + budget gate + ONE bounded call.
    evidence_keys = {k for k in evidence.keys() if not k.startswith("_")}
    probe = select_disconfirmation_probe(incident_type, evidence_keys)
    probe_outcome: dict[str, Any] = {"executed": False}
    if probe and _flag("HYPOTHESIS_DISCONFIRMATION_FETCH"):
        exec_probe = probe
        if probe["capability_id"] not in _PROBE_EXECUTION:
            exec_probe = select_disconfirmation_probe(
                incident_type, evidence_keys,
                allowed=set(_PROBE_EXECUTION),
            ) or probe
        probe_outcome = _execute_probe(exec_probe, sup, budget, evidence)
        if probe_outcome.get("executed"):
            hid = _leader()
            if probe_outcome.get("empty"):
                # Sought refutation, found nothing → survives, weak support
                tracker.add_supporting_evidence(
                    hid, probe_outcome["evidence_key"], weight=0.3,
                    reason="disconfirmation_probe_found_nothing")
            else:
                tracker.add_refuting_evidence(
                    hid, probe_outcome["evidence_key"], weight=0.5,
                    reason="disconfirmation_probe_returned_signal")
                cur = tracker._by_id[hid].confidence
                tracker.transition(hid, "supported",
                                    new_confidence=_clamp(cur - REFUTE_DELTA),
                                    reason="refute:probe")

    # ── Convergence: confirm the survivor, eliminate the rest.
    winner_id = _leader()
    survived = winner_id == leader_before
    winner = tracker._by_id[winner_id]
    tracker.confirm(
        winner_id, root_cause=winner.description or winner.name,
        reason=("survived_disconfirmation" if survived
                 else "leader_flipped_after_disconfirmation"),
        confidence=winner.confidence,
    )
    for hid in sorted(ids.values()):
        if hid == winner_id:
            continue
        h = tracker._by_id[hid]
        refuted_by = sorted(e.key for e in h.refuting_evidence)
        tracker.rule_out(
            hid,
            reason=(f"lower net support than winner"
                     + (f"; refuted by {','.join(refuted_by)}"
                        if refuted_by else "")),
        )
    graph = tracker.build_graph()
    scores = {s.hypothesis_id: s.net_score
              for s in score_hypothesis_graph(graph)}

    # ── Counterfactual: the evidence that would most likely change this.
    counterfactual = (
        "Evidence most likely to change this conclusion: "
        + ", ".join(probe["missing_evidence"])
        + f" (via {probe['capability_id']})"
    ) if probe else "No uncollected evidence source identified."

    # ── Additive enrichment (shadow authority: root_cause untouched).
    result["_hypothesis_graph"] = graph.to_dict()
    result["_elimination_narrative"] = {
        "considered": [
            {"name": tracker._by_id[hid].name,
             "origin": next(c["origin"] for c in cands
                             if ids[c["name"]] == hid),
             "final_confidence": tracker._by_id[hid].confidence,
             "net_support": scores.get(hid, 0.0),
             "status": tracker._by_id[hid].status}
            for hid in sorted(ids.values())
        ],
        "winner": winner.name,
        "survived_disconfirmation": survived,
        "ruled_out": [
            {"name": tracker._by_id[hid].name,
             "reason": tracker._by_id[hid].ruled_out_reason}
            for hid in sorted(ids.values()) if hid != winner_id
        ],
        "refutation_probe": probe,
        "probe_outcome": probe_outcome,
    }
    result["_counterfactual"] = counterfactual


__all__ = ["run_hypothesis_engine", "select_disconfirmation_probe"]
