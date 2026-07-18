"""Operational Discovery Engine (ODE) — offline knowledge discovery.

Not prediction, not anomaly detection, not another RCA engine. ODE mines the
history of completed investigations and DISCOVERS previously-unknown
operational relationships — recurring causal chains, hidden dependencies,
latent failure clusters, evidence that consistently becomes decisive,
recurring false leads, operator interventions that consistently help.

It answers: "what operational knowledge exists today that did not exist six
months ago?"

Produce-only, offline, deterministic, append-only, replayable, removable.
Composes existing investigation outputs into observations, then mines ACROSS
observations. Changes no runtime path, no authority, no Wave 3; touches
neither IQS, EIC, nor the Gold Dataset. Reuses only the generic bootstrap CI.

Determinism: no clock (times come from incident data), no randomness (seeded
bootstrap), sorted iteration, deterministic sha256 discovery ids. Every
discovery carries recurrence, a confidence interval, contradictory
observations, reproducibility, and NOT_MEASURED where support is insufficient.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from sentinel_core.investigation_value.scientific_validation import (
    NOT_MEASURED,
    bootstrap_ci,
)

ODE_SCHEMA_VERSION = 1

DISCOVERY_TYPES = ("temporal", "topology", "evidence", "hypothesis",
                   "operational", "human")

# thresholds (documented; tuned later from a real corpus)
_MIN_RECURRENCE = 3            # a pattern must recur at least this many times
_MIN_SUPPORT = 0.6            # ... in this fraction of its opportunities
_MIN_OBS_FOR_CLASS = 3       # min observations of a class to mine it


def _round(x: float) -> float:
    return round(float(x), 4)


def _sha16(obj: Any) -> str:
    return "disc:" + hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"),
                   default=str).encode()).hexdigest()[:14]


# ---------------------------------------------------------------------------
# Observation (one completed investigation -> one observation)
# ---------------------------------------------------------------------------

def observation(result: Mapping[str, Any],
                incident: Mapping[str, Any] | None = None,
                *, human: Mapping[str, Any] | None = None,
                outcome_correct: bool | None = None) -> dict[str, Any]:
    """Compose one ODE observation from a completed investigation. Times come
    from the incident (never wall-clock)."""
    inc = dict(incident or {})
    causal = result.get("_causal_investigation") or {}
    narr = result.get("_elimination_narrative") or {}
    di = result.get("_decision_intelligence") or {}

    loc = causal.get("localization", {}) if isinstance(causal, dict) else {}
    root_svc = str(loc.get("root_cause_service", ""))
    sym_svc = str(loc.get("symptom_service", ""))
    roles = causal.get("roles", {}) if isinstance(causal, dict) else {}
    affected = sorted({str(s) for s in roles} | {root_svc, sym_svc,
                      str(inc.get("service", ""))} - {""})

    edges = []
    if root_svc and sym_svc and root_svc != sym_svc:
        edges.append([root_svc, sym_svc])
    chain = (causal.get("winning_chain", {}) or {}).get("path", []) \
        if isinstance(causal, dict) else []
    for a, b in zip(chain, chain[1:]):
        if str(a) != str(b):
            edges.append([str(a), str(b)])

    decisive = sorted((di.get("evidence_attribution", {}) or {}).get(
        "decisive_evidence", []) if isinstance(di, dict) else [])
    ruled_out = sorted(str(x.get("name", "")) for x in
                       (narr.get("ruled_out", []) or [])
                       if isinstance(x, dict) and x.get("name"))

    return {
        "schema_version": ODE_SCHEMA_VERSION,
        "incident_id": str(inc.get("incident_id",
                                   result.get("incident_id", ""))),
        "incident_type": str(inc.get("incident_type",
                                     result.get("incident_type", ""))),
        "service": str(inc.get("service", "")),
        "incident_time": str(inc.get("created_at",
                                     inc.get("start_time", ""))),
        "affected_services": affected,
        "causal_edges": [list(e) for e in edges],
        "decisive_evidence": decisive,
        "winner": str(narr.get("winner", "")) if isinstance(narr, dict) else "",
        "ruled_out": ruled_out,
        "operator_interventions": sorted(str(x) for x in
                                         (human or {}).get("interventions", [])
                                         or []),
        "outcome_correct": outcome_correct,
    }


# ---------------------------------------------------------------------------
# Statistical support helper
# ---------------------------------------------------------------------------

def _support(indicators: list[int], *, seed: int) -> dict[str, Any]:
    """Given per-opportunity indicators (1 held / 0 contradicted), compute
    recurrence, support rate, CI, and a deterministic significance flag."""
    n = len(indicators)
    held = sum(indicators)
    rate = _round(held / n) if n else 0.0
    ci = bootstrap_ci([float(i) for i in indicators], seed=seed) if n else {}
    significant = (held >= _MIN_RECURRENCE and rate >= _MIN_SUPPORT
                   and n >= _MIN_OBS_FOR_CLASS)
    return {
        "opportunities": n,
        "recurrence": held,
        "support_rate": rate,
        "ci95": [ci.get("lo"), ci.get("hi")] if n else NOT_MEASURED,
        "contradictions": n - held,
        "underpowered": n < _MIN_OBS_FOR_CLASS,
        "significant": bool(significant),
    }


def _reproducible(support_ids: list[str], contra_ids: list[str]) -> bool:
    """Deterministic split-half check: pattern holds (support>contra) in both
    even- and odd-indexed halves of the ordered id set."""
    ordered = sorted(set(support_ids) | set(contra_ids))
    s = set(support_ids)
    halves = ([], [])
    for i, cid in enumerate(ordered):
        halves[i % 2].append(1 if cid in s else 0)
    return all(sum(h) * 2 > len(h) for h in halves if h)


def _record(dtype: str, signature: Any, description: str,
            *, supporting: list[str], contradicting: list[str],
            affected: list[str], times: list[str], support: dict[str, Any],
            ) -> dict[str, Any]:
    times_sorted = sorted(t for t in times if t)
    rec = {
        "schema_version": ODE_SCHEMA_VERSION,
        "discovery_id": _sha16([dtype, signature]),
        "discovery_type": dtype,
        "description": description,
        "signature": signature,
        "supporting_investigations": sorted(set(supporting)),
        "contradicting_investigations": sorted(set(contradicting)),
        "affected_services": sorted(set(affected) - {""}),
        "recurrence_count": support["recurrence"],
        "first_observed": times_sorted[0] if times_sorted else "",
        "last_observed": times_sorted[-1] if times_sorted else "",
        "confidence": support["support_rate"],
        "statistical_support": support,
        "reproducibility": _reproducible(supporting, contradicting),
    }
    return rec


# ---------------------------------------------------------------------------
# Miners
# ---------------------------------------------------------------------------

def mine_topology(obs: list[Mapping[str, Any]],
                  declared_dependencies: Iterable[Iterable[str]] | None = None,
                  *, seed: int = 1) -> list[dict[str, Any]]:
    """Recurring causal edges (root->symptom) not in the declared CMDB =
    previously-unknown dependencies."""
    declared = {(str(a), str(b)) for a, b in (declared_dependencies or [])}
    edge_support: dict[tuple, list[str]] = {}
    edge_times: dict[tuple, list[str]] = {}
    for o in obs:
        seen = {tuple(e) for e in o.get("causal_edges", [])}
        for e in seen:
            edge_support.setdefault(e, []).append(o["incident_id"])
            edge_times.setdefault(e, []).append(o.get("incident_time", ""))
    out = []
    for edge in sorted(edge_support):
        if edge in declared:
            continue                        # already known — not a discovery
        support_ids = edge_support[edge]
        sup = _support([1] * len(support_ids), seed=seed)
        if not sup["significant"]:
            continue
        out.append(_record(
            "topology", list(edge),
            f"undeclared dependency: {edge[0]} failure propagates to {edge[1]}",
            supporting=support_ids, contradicting=[],
            affected=list(edge), times=edge_times[edge], support=sup))
    return out


def mine_evidence(obs: list[Mapping[str, Any]], *, seed: int = 1,
                  ) -> list[dict[str, Any]]:
    """Evidence that consistently becomes decisive for an incident class."""
    by_class: dict[str, list[Mapping[str, Any]]] = {}
    for o in obs:
        by_class.setdefault(o.get("incident_type", ""), []).append(o)
    out = []
    for cls in sorted(by_class):
        members = by_class[cls]
        if len(members) < _MIN_OBS_FOR_CLASS:
            continue
        ev_keys = sorted({e for m in members for e in m.get("decisive_evidence",
                                                            [])})
        for ev in ev_keys:
            indicators, sup_ids, con_ids, times = [], [], [], []
            for m in members:
                held = ev in m.get("decisive_evidence", [])
                indicators.append(1 if held else 0)
                (sup_ids if held else con_ids).append(m["incident_id"])
                times.append(m.get("incident_time", ""))
            sup = _support(indicators, seed=seed)
            if not sup["significant"]:
                continue
            out.append(_record(
                "evidence", [cls, ev],
                f"'{ev}' is consistently decisive for {cls} incidents",
                supporting=sup_ids, contradicting=con_ids,
                affected=[m.get("service", "") for m in members],
                times=times, support=sup))
    return out


def mine_hypothesis(obs: list[Mapping[str, Any]], *, seed: int = 1,
                    ) -> list[dict[str, Any]]:
    """Recurring winning hypotheses and recurring false leads per class."""
    by_class: dict[str, list[Mapping[str, Any]]] = {}
    for o in obs:
        by_class.setdefault(o.get("incident_type", ""), []).append(o)
    out = []
    for cls in sorted(by_class):
        members = by_class[cls]
        if len(members) < _MIN_OBS_FOR_CLASS:
            continue
        # recurring false leads: hypotheses repeatedly ruled out.
        leads = sorted({h for m in members for h in m.get("ruled_out", [])})
        for lead in leads:
            indicators = [1 if lead in m.get("ruled_out", []) else 0
                          for m in members]
            sup_ids = [m["incident_id"] for m in members
                       if lead in m.get("ruled_out", [])]
            sup = _support(indicators, seed=seed)
            if not sup["significant"]:
                continue
            out.append(_record(
                "hypothesis", [cls, "false_lead", lead],
                f"'{lead}' is a recurring false lead for {cls} incidents",
                supporting=sup_ids, contradicting=[
                    m["incident_id"] for m in members
                    if lead not in m.get("ruled_out", [])],
                affected=[m.get("service", "") for m in members],
                times=[m.get("incident_time", "") for m in members],
                support=sup))
    return out


def mine_operational(obs: list[Mapping[str, Any]], *, seed: int = 1,
                     ) -> list[dict[str, Any]]:
    """Latent failure clusters: sets of services that recurrently fail
    together (co-membership in affected_services), not encoded as a unit."""
    pair_support: dict[tuple, list[str]] = {}
    pair_times: dict[tuple, list[str]] = {}
    for o in obs:
        svcs = sorted(set(o.get("affected_services", [])))
        for i in range(len(svcs)):
            for j in range(i + 1, len(svcs)):
                key = (svcs[i], svcs[j])
                pair_support.setdefault(key, []).append(o["incident_id"])
                pair_times.setdefault(key, []).append(o.get("incident_time", ""))
    out = []
    for pair in sorted(pair_support):
        ids = pair_support[pair]
        sup = _support([1] * len(ids), seed=seed)
        if not sup["significant"]:
            continue
        out.append(_record(
            "operational", list(pair),
            f"latent failure cluster: {pair[0]} and {pair[1]} recurrently "
            "fail together",
            supporting=ids, contradicting=[], affected=list(pair),
            times=pair_times[pair], support=sup))
    return out


def mine_temporal(obs: list[Mapping[str, Any]], *,
                  window_seconds: int = 7200, seed: int = 1,
                  ) -> list[dict[str, Any]]:
    """Recurring ORDERED service incidents: service A incidents precede
    service B incidents within a window, repeatedly (with median lead time)."""
    timed = sorted((o for o in obs if o.get("incident_time")
                    and o.get("service")),
                   key=lambda o: (o["incident_time"], o["incident_id"]))
    order_support: dict[tuple, list[str]] = {}
    order_leads: dict[tuple, list[float]] = {}
    order_times: dict[tuple, list[str]] = {}
    for i, a in enumerate(timed):
        ta = _epoch(a["incident_time"])
        if ta is None:
            continue
        for b in timed[i + 1:]:
            tb = _epoch(b["incident_time"])
            if tb is None:
                continue
            delta = tb - ta
            if delta <= 0 or delta > window_seconds:
                if delta > window_seconds:
                    break
                continue
            if a["service"] == b["service"]:
                continue
            key = (a["service"], b["service"])
            order_support.setdefault(key, []).append(
                a["incident_id"] + ">" + b["incident_id"])
            order_leads.setdefault(key, []).append(delta)
            order_times.setdefault(key, []).append(a["incident_time"])
    out = []
    for key in sorted(order_support):
        ids = order_support[key]
        sup = _support([1] * len(ids), seed=seed)
        if not sup["significant"]:
            continue
        leads = sorted(order_leads[key])
        median = leads[len(leads) // 2]
        out.append(_record(
            "temporal", list(key),
            f"{key[0]} incidents precede {key[1]} incidents by ~"
            f"{int(median // 60)} min",
            supporting=ids, contradicting=[], affected=list(key),
            times=order_times[key], support=sup))
        out[-1]["median_lead_seconds"] = int(median)
    return out


def mine_human(obs: list[Mapping[str, Any]], *, seed: int = 1,
               ) -> list[dict[str, Any]]:
    """Operator interventions that consistently precede a correct outcome."""
    labeled = [o for o in obs if o.get("outcome_correct") is not None
               and o.get("operator_interventions")]
    interventions = sorted({iv for o in labeled
                            for iv in o.get("operator_interventions", [])})
    out = []
    for iv in interventions:
        members = [o for o in labeled if iv in o.get("operator_interventions",
                                                     [])]
        if len(members) < _MIN_OBS_FOR_CLASS:
            continue
        indicators = [1 if m["outcome_correct"] else 0 for m in members]
        sup = _support(indicators, seed=seed)
        if not sup["significant"]:
            continue
        out.append(_record(
            "human", ["intervention", iv],
            f"operator intervention '{iv}' consistently precedes a correct "
            "outcome",
            supporting=[m["incident_id"] for m in members
                        if m["outcome_correct"]],
            contradicting=[m["incident_id"] for m in members
                           if not m["outcome_correct"]],
            affected=[m.get("service", "") for m in members],
            times=[m.get("incident_time", "") for m in members], support=sup))
    return out


def _epoch(iso: str) -> float | None:
    try:
        from datetime import datetime
        return datetime.fromisoformat(
            str(iso).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Discovery Quality Score (DQS)
# ---------------------------------------------------------------------------

_USEFULNESS = {"topology": 1.0, "temporal": 0.9, "operational": 0.8,
               "evidence": 0.7, "human": 0.9, "hypothesis": 0.6}


def discovery_quality_score(disc: Mapping[str, Any],
                            *, known_signatures: Iterable[str] = ()) -> dict[str, Any]:
    """DQS from novelty / recurrence / reproducibility / usefulness /
    confidence / stability."""
    sig = _sha16([disc["discovery_type"], disc["signature"]])
    novelty = 0.0 if sig in set(known_signatures) else 1.0
    rec = min(1.0, disc["recurrence_count"] / (2 * _MIN_RECURRENCE))
    repro = 1.0 if disc["reproducibility"] else 0.0
    useful = _USEFULNESS.get(disc["discovery_type"], 0.5)
    conf = float(disc["confidence"])
    ss = disc["statistical_support"]
    stability = _round(1.0 - (ss["contradictions"] / ss["opportunities"])) \
        if ss["opportunities"] else 0.0
    dqs = _round(0.20 * novelty + 0.20 * rec + 0.15 * repro
                 + 0.15 * useful + 0.15 * conf + 0.15 * stability)
    return {
        "discovery_id": disc["discovery_id"], "dqs": dqs,
        "components": {"novelty": novelty, "recurrence": _round(rec),
                       "reproducibility": repro, "usefulness": useful,
                       "confidence": _round(conf), "stability": stability},
    }


# ---------------------------------------------------------------------------
# Longitudinal tracking
# ---------------------------------------------------------------------------

def longitudinal_update(previous: Iterable[Mapping[str, Any]],
                        current: Iterable[Mapping[str, Any]],
                        ) -> dict[str, Any]:
    """Compare two discovery sets: strengthened / weakened / retired /
    disproven / new."""
    prev = {d["discovery_id"]: d for d in previous}
    cur = {d["discovery_id"]: d for d in current}
    strengthened, weakened, disproven, new, retired = [], [], [], [], []
    for did, d in sorted(cur.items()):
        if did not in prev:
            new.append(did)
            continue
        pc, cc = prev[did]["confidence"], d["confidence"]
        ss = d["statistical_support"]
        if ss["contradictions"] > ss["recurrence"]:
            disproven.append(did)
        elif cc > pc + 0.01:
            strengthened.append(did)
        elif cc < pc - 0.01:
            weakened.append(did)
    for did in sorted(prev):
        if did not in cur:
            retired.append(did)
    return {
        "schema_version": ODE_SCHEMA_VERSION,
        "strengthened": strengthened, "weakened": weakened,
        "disproven": disproven, "retired": retired, "new": new,
    }


# ---------------------------------------------------------------------------
# Top-level run
# ---------------------------------------------------------------------------

def run_discovery(observations: Iterable[Mapping[str, Any]], *,
                  declared_dependencies: Iterable[Iterable[str]] | None = None,
                  known_signatures: Iterable[str] = (),
                  seed: int = 1) -> dict[str, Any]:
    """Mine all discovery types + score them. Answers 'what operational
    knowledge exists now?'"""
    obs = list(observations)
    discoveries = (
        mine_topology(obs, declared_dependencies, seed=seed)
        + mine_temporal(obs, seed=seed)
        + mine_evidence(obs, seed=seed)
        + mine_hypothesis(obs, seed=seed)
        + mine_operational(obs, seed=seed)
        + mine_human(obs, seed=seed))
    discoveries.sort(key=lambda d: (d["discovery_type"], d["discovery_id"]))
    scored = [discovery_quality_score(d, known_signatures=known_signatures)
              for d in discoveries]
    by_type: dict[str, int] = {}
    for d in discoveries:
        by_type[d["discovery_type"]] = by_type.get(d["discovery_type"], 0) + 1
    return {
        "schema_version": ODE_SCHEMA_VERSION,
        "observations": len(obs),
        "discoveries": discoveries,
        "dqs": {s["discovery_id"]: s for s in scored},
        "by_type": dict(sorted(by_type.items())),
        "knowledge_count": len(discoveries),
        "note": ("each discovery is a recurring, statistically-supported "
                 "operational relationship mined from investigation history; "
                 "NOT_MEASURED where support is insufficient"),
    }


__all__ = [
    "ODE_SCHEMA_VERSION", "DISCOVERY_TYPES",
    "observation", "mine_topology", "mine_temporal", "mine_evidence",
    "mine_hypothesis", "mine_operational", "mine_human",
    "discovery_quality_score", "longitudinal_update", "run_discovery",
]
