"""Reasoning evaluation — score an investigation trace against ground truth and
the hidden Enterprise Investigation Specification.

Two layers, both honest:

1. **Ground-truth dimensions (REUSED, not re-implemented):** ``eic.score_submission``
   grades the neutral submission on its 10 weighted dimensions (rca_correctness,
   localization, false_lead_avoidance, decisive_evidence_latency,
   evidence_efficiency, distractor_avoidance, hypothesis_quality,
   confidence_calibration, explainability, replayability). Each is a value in
   ``[0,1]`` or ``None`` (NOT_MEASURED).

2. **Process dimensions vs the Investigation Specification:** measured ONLY from
   what the black box actually exposed — which servers it queried, which rendered
   channels it consumed, whether it explored/eliminated hypotheses, and whether its
   final confidence lands in the specified range. Dimensions the engine does not
   expose in the evaluated configuration (per-step confidence-evolution trajectory,
   detailed evidence attribution, blast-radius / business-context / recovery
   reasoning) are reported ``NOT_MEASURED`` — never faked.

The evaluator is the ONLY component that reads the hidden knowledge
(``task.ground_truth``, ``efic.investigation_spec``). It runs AFTER the engine has
finished; that knowledge never touches the engine (see the isolation proof test).
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sentinel_core.eic import score_submission

from enterprisebench.pipeline.render import NATIVE_CHANNELS

MEASURED = "measured"
NOT_MEASURED = "NOT_MEASURED"

# Process dimensions the engine does not expose in the evaluated configuration.
# Named explicitly so the report is honest about the ceiling of what EB-2 measures.
UNEXPOSED_PROCESS_DIMS = (
    "confidence_evolution_trajectory",   # engine emits a final confidence, not a curve
    "evidence_attribution_detail",       # per-evidence primary/supporting/red-herring
    "blast_radius_understanding",
    "business_context",
    "recovery_validation",
)


def _state(v: Optional[float]) -> dict[str, Any]:
    return {"raw": v, "state": NOT_MEASURED if v is None else MEASURED}


def _required_native(efic: Mapping[str, Any]) -> list[str]:
    """EFIC required MCP sources that map to an engine collection channel."""
    util = efic.get("mcp_utilization", {}) or {}
    return sorted(s for s, u in util.items()
                  if u == "required" and s in NATIVE_CHANNELS)


def _required_unreachable(efic: Mapping[str, Any]) -> list[str]:
    """Required MCP sources the engine has no native channel for (EB-3 gap)."""
    util = efic.get("mcp_utilization", {}) or {}
    return sorted(s for s, u in util.items()
                  if u == "required" and s not in NATIVE_CHANNELS)


def _evidence_collection(trace: Mapping[str, Any],
                         efic: Mapping[str, Any]) -> Optional[float]:
    """Fraction of the required *reachable* sources whose channel the engine served.

    Scored only over sources the engine can natively query; unreachable required
    sources are reported separately (a corpus/engine coverage gap, not an engine
    failure)."""
    required = _required_native(efic)
    if not required:
        return None
    served_servers = set(trace.get("servers_queried", []))
    # A source is "collected" if the engine queried its server AND our source
    # served a rendered channel for it.
    served_channels = set(trace.get("rendered_channels_served", []))
    channel_servers = {c.split(".", 1)[0] for c in served_channels}
    hit = sum(1 for s in required
              if _source_server(s) in served_servers
              and _source_server(s) in channel_servers)
    return round(hit / len(required), 4)


_SOURCE_SERVER = {  # EFIC source -> the engine server that carries it
    "splunk": "splunk", "dynatrace": "dynatrace", "sysdig": "sysdig",
    "database": "sysdig", "kubernetes": "sysdig", "servicenow": "servicenow",
    "github": "github",
}


def _source_server(source: str) -> str:
    return _SOURCE_SERVER.get(source, source)


def _mcp_contract_fulfillment(trace: Mapping[str, Any],
                              spec: Mapping[str, Any]) -> Optional[float]:
    """Fraction of the Investigation Specification's MCP contract that the engine
    fulfilled — of the contract's expected MCPs that map to an engine-reachable
    channel, how many did the engine actually query. Reads
    ``investigation_spec.mcp_investigation_contract`` directly (the mission's
    Cross-MCP Correlation / Evidence Collection process check)."""
    contract = spec.get("mcp_investigation_contract") or []
    reachable = [c for c in contract
                 if _source_server(str(c.get("mcp", ""))) in _SOURCE_SERVER.values()]
    if not reachable:
        return None
    served = set(trace.get("servers_queried", []))
    hit = sum(1 for c in reachable
              if _source_server(str(c.get("mcp", ""))) in served)
    return round(hit / len(reachable), 4)


def _confidence_in_range(trace: Mapping[str, Any],
                         efic: Mapping[str, Any]) -> Optional[float]:
    rng = efic.get("expected_confidence_range")
    if not (isinstance(rng, (list, tuple)) and len(rng) == 2):
        return None
    lo, hi = rng
    conf = trace.get("confidence")
    if not isinstance(conf, (int, float)):
        return None
    return 1.0 if lo <= conf <= hi else 0.0


def evaluate(task: Mapping[str, Any], efic: Mapping[str, Any],
             trace: Mapping[str, Any]) -> dict[str, Any]:
    """Score one investigation. Returns EIC dims + process dims + composite."""
    submission = trace.get("submission", {}) or {}
    spec = efic.get("investigation_spec", {}) or {}
    eic = score_submission(task, submission)          # REUSED 10-dim scorer
    eic_dims = eic.get("dimensions", {}) or {}

    n_native_channels = len(set(trace.get("rendered_channels_served", [])))

    process = {
        "evidence_collection": _state(_evidence_collection(trace, efic)),
        "cross_mcp_correlation": _state(
            1.0 if n_native_channels >= 2 else (0.0 if n_native_channels else None)),
        "hypothesis_exploration": _state(
            1.0 if trace.get("hypothesis_count", 0) >= 2
            else (0.0 if trace.get("winner_hypothesis") else None)),
        "hypothesis_elimination": _state(
            1.0 if trace.get("ruled_out") else 0.0),
        "mcp_contract_fulfillment": _state(_mcp_contract_fulfillment(trace, spec)),
        "confidence_in_expected_range": _state(_confidence_in_range(trace, efic)),
        "recommendation_present": _state(
            1.0 if trace.get("recommendation") else 0.0),
        "localization_correct": _state(eic_dims.get("localization")),
        "root_cause_correct": _state(eic_dims.get("rca_correctness")),
    }
    for d in UNEXPOSED_PROCESS_DIMS:
        process[d] = _state(None)

    measured = [d["raw"] for d in process.values() if d["state"] == MEASURED]
    eic_score = eic.get("eic_score")
    # Composite: the reused EIC composite blended with the observable process mean.
    components = [x for x in
                  ([eic_score] if isinstance(eic_score, (int, float)) else [])
                  + measured]
    investigation_score = round(sum(components) / len(components), 4) if components \
        else None

    return {
        "task_id": str(task.get("task_id", "")),
        "eic": eic,
        "process": process,
        "investigation_score": investigation_score,
        "reachability": {
            "required_native": _required_native(efic),
            "required_unreachable": _required_unreachable(efic),
            "decisive_reachable": all(
                _source_server(s) in {c.split(".", 1)[0]
                                      for c in trace.get("rendered_channels_served", [])}
                for s in task.get("ground_truth", {}).get("decisive_evidence", [])
                if _source_server(s) in _SOURCE_SERVER.values()) if
            task.get("ground_truth", {}).get("decisive_evidence") else None,
        },
    }


__all__ = ["evaluate", "MEASURED", "NOT_MEASURED", "UNEXPOSED_PROCESS_DIMS"]
