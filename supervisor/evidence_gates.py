"""Evidence quality gates — mid-investigation circuit breakers.

Quality gates are checked at key decision points during an investigation.
A gate can:
  - PASS:   investigation continues normally
  - WARN:   investigation continues but a warning is recorded
  - BLOCK:  investigation is halted with an explanation (budget saved)
  - ESCALATE: investigation triggers deep-dive mode

Gates are evaluated in priority order and the first BLOCK or ESCALATE wins.

Gates:
  G1 — MinSources:     at least MIN_EVIDENCE_SOURCES unique data sources after Phase 1
  G2 — ConfidenceFloor: if confidence < CONFIDENCE_FLOOR after all budget used → ESCALATE
  G3 — CitationFloor:  if citation_coverage < CITATION_FLOOR after analysis → WARN/BLOCK
  G4 — DeadEvidence:   if all evidence returns empty → BLOCK early (no data to analyze)
  G5 — HallucinationRisk: if no citations found for root cause → BLOCK output

Configuration (all overridable via env):
  GATE_MIN_SOURCES       — minimum unique evidence sources (default: 2)
  GATE_CONFIDENCE_FLOOR  — minimum confidence to accept result (default: 25)
  GATE_CITATION_FLOOR    — minimum citation_coverage (default: 0.35)
  GATE_DEAD_EVIDENCE_N   — how many empty responses trigger dead-evidence gate (default: 5)
  GATE_STALE_FRACTION    — fraction of sources that can be stale before warning (default: 0.60)
  EVIDENCE_GATES_ENABLED — on/off (default: true)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("sentinalai.evidence_gates")

GATES_ENABLED = os.environ.get("EVIDENCE_GATES_ENABLED", "true").lower() in ("1", "true", "yes")
MIN_SOURCES    = int(float(os.environ.get("GATE_MIN_SOURCES",      "2")))
CONFIDENCE_FLOOR = int(float(os.environ.get("GATE_CONFIDENCE_FLOOR", "25")))
CITATION_FLOOR   = float(os.environ.get("GATE_CITATION_FLOOR",       "0.35"))
DEAD_EVIDENCE_N  = int(os.environ.get("GATE_DEAD_EVIDENCE_N",         "5"))
STALE_FRACTION   = float(os.environ.get("GATE_STALE_FRACTION",        "0.60"))


class GateVerdict(str, Enum):
    PASS     = "pass"
    WARN     = "warn"
    BLOCK    = "block"
    ESCALATE = "escalate"


@dataclass
class GateResult:
    gate_name: str
    verdict: GateVerdict
    reason: str
    metric_value: float = 0.0
    threshold: float = 0.0

    @property
    def passed(self) -> bool:
        return self.verdict == GateVerdict.PASS

    @property
    def is_critical(self) -> bool:
        return self.verdict in (GateVerdict.BLOCK, GateVerdict.ESCALATE)


@dataclass
class GateCheckResult:
    """Aggregate result of all gate checks."""
    passed: bool
    verdict: GateVerdict                    # worst verdict seen
    gates: list[GateResult] = field(default_factory=list)
    blocking_gate: GateResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "verdict": self.verdict.value,
            "gates": [
                {
                    "gate": g.gate_name,
                    "verdict": g.verdict.value,
                    "reason": g.reason,
                    "metric_value": g.metric_value,
                    "threshold": g.threshold,
                }
                for g in self.gates
            ],
            "blocking_gate": (
                {
                    "gate": self.blocking_gate.gate_name,
                    "verdict": self.blocking_gate.verdict.value,
                    "reason": self.blocking_gate.reason,
                }
                if self.blocking_gate else None
            ),
        }


# ---------------------------------------------------------------------------
# Gate checks
# ---------------------------------------------------------------------------

def check_post_collection(evidence: dict[str, Any], budget_used: int) -> GateCheckResult:
    """G1 + G4: Check evidence quality immediately after collection phase.

    Call this after all workers have run their initial evidence collection.
    """
    if not GATES_ENABLED:
        return GateCheckResult(passed=True, verdict=GateVerdict.PASS)

    gates: list[GateResult] = []

    # G4 — Dead evidence: how many evidence keys are empty/missing?
    empty_keys = _count_empty_evidence(evidence)
    total_keys = max(1, len([k for k in evidence if not k.startswith("_")]))
    if empty_keys >= DEAD_EVIDENCE_N:
        r = GateResult(
            gate_name="G4_DeadEvidence",
            verdict=GateVerdict.BLOCK,
            reason=(
                f"{empty_keys}/{total_keys} evidence sources returned empty results. "
                "Cannot perform RCA without data."
            ),
            metric_value=float(empty_keys),
            threshold=float(DEAD_EVIDENCE_N),
        )
        gates.append(r)
        logger.warning("Gate G4 BLOCK: %s", r.reason)
        return GateCheckResult(passed=False, verdict=GateVerdict.BLOCK, gates=gates, blocking_gate=r)

    # G1 — Min sources: at least MIN_SOURCES unique populated source types
    unique_sources = _count_unique_sources(evidence)
    if unique_sources < MIN_SOURCES:
        verdict = GateVerdict.ESCALATE if budget_used < 10 else GateVerdict.WARN
        r = GateResult(
            gate_name="G1_MinSources",
            verdict=verdict,
            reason=(
                f"Only {unique_sources} unique evidence source(s) found "
                f"(minimum: {MIN_SOURCES}). "
                + ("Triggering deep-dive." if verdict == GateVerdict.ESCALATE else "Proceeding with caution.")
            ),
            metric_value=float(unique_sources),
            threshold=float(MIN_SOURCES),
        )
        gates.append(r)
        logger.info("Gate G1 %s: %s", verdict.value.upper(), r.reason)
        if verdict == GateVerdict.ESCALATE:
            return GateCheckResult(passed=False, verdict=verdict, gates=gates, blocking_gate=r)
    else:
        gates.append(GateResult(
            gate_name="G1_MinSources", verdict=GateVerdict.PASS,
            reason=f"{unique_sources} unique sources ≥ {MIN_SOURCES}",
            metric_value=float(unique_sources), threshold=float(MIN_SOURCES),
        ))

    worst = max((g.verdict for g in gates), key=lambda v: _verdict_rank(v), default=GateVerdict.PASS)
    return GateCheckResult(passed=worst in (GateVerdict.PASS, GateVerdict.WARN), verdict=worst, gates=gates)


def check_post_analysis(
    result: dict[str, Any],
    evidence: dict[str, Any],
    budget_remaining: int,
) -> GateCheckResult:
    """G2 + G3 + G5: Check result quality after LLM analysis.

    Call this after _analyze_evidence() returns a result.
    """
    if not GATES_ENABLED:
        return GateCheckResult(passed=True, verdict=GateVerdict.PASS)

    gates: list[GateResult] = []
    confidence = int(result.get("confidence", 0))
    citation_coverage = float(result.get("citation_coverage", 0.0))
    citations = result.get("citations", [])
    root_cause = result.get("root_cause", "")

    # G5 — Hallucination risk: root cause has zero citations
    # Only trigger when the citation list is non-empty (some claims were cited)
    # but none of them support the root cause.  When citations is empty, G3
    # already warns via citation_coverage; G5 is specifically for the case
    # where we have OTHER citations but the root cause is unsupported.
    rc_citations = [c for c in citations if _rc_cited(root_cause, c)]
    if root_cause and citations and not rc_citations:
        # Only BLOCK if we actually have evidence but none supports root cause
        unique_sources = _count_unique_sources(evidence)
        if unique_sources >= MIN_SOURCES:
            r = GateResult(
                gate_name="G5_HallucinationRisk",
                verdict=GateVerdict.BLOCK,
                reason=(
                    f"Root cause '{root_cause[:80]}' has no supporting citations "
                    f"despite {unique_sources} evidence sources. Possible hallucination."
                ),
                metric_value=0.0,
                threshold=1.0,
            )
            gates.append(r)
            logger.warning("Gate G5 BLOCK: %s", r.reason)
            return GateCheckResult(passed=False, verdict=GateVerdict.BLOCK, gates=gates, blocking_gate=r)

    # G3 — Citation floor
    if citation_coverage < CITATION_FLOOR:
        verdict = GateVerdict.WARN  # warn but don't block — citation can be low for short results
        r = GateResult(
            gate_name="G3_CitationFloor",
            verdict=verdict,
            reason=(
                f"Citation coverage {citation_coverage:.1%} < floor {CITATION_FLOOR:.1%}. "
                "RCA claims may lack evidence backing."
            ),
            metric_value=citation_coverage,
            threshold=CITATION_FLOOR,
        )
        gates.append(r)
        logger.info("Gate G3 WARN: citation_coverage=%.2f", citation_coverage)
    else:
        gates.append(GateResult(
            gate_name="G3_CitationFloor", verdict=GateVerdict.PASS,
            reason=f"citation_coverage {citation_coverage:.1%} ≥ {CITATION_FLOOR:.1%}",
            metric_value=citation_coverage, threshold=CITATION_FLOOR,
        ))

    # G2 — Confidence floor (only enforce if budget is exhausted)
    if confidence < CONFIDENCE_FLOOR and budget_remaining <= 2:
        r = GateResult(
            gate_name="G2_ConfidenceFloor",
            verdict=GateVerdict.ESCALATE,
            reason=(
                f"Confidence {confidence}% < floor {CONFIDENCE_FLOOR}% with budget exhausted. "
                "Result marked as LOW_CONFIDENCE."
            ),
            metric_value=float(confidence),
            threshold=float(CONFIDENCE_FLOOR),
        )
        gates.append(r)
        logger.warning("Gate G2 ESCALATE: confidence=%d%%", confidence)
    else:
        gates.append(GateResult(
            gate_name="G2_ConfidenceFloor", verdict=GateVerdict.PASS,
            reason=f"confidence {confidence}% {'(budget remaining)' if budget_remaining > 2 else f'>= {CONFIDENCE_FLOOR}%'}",
            metric_value=float(confidence), threshold=float(CONFIDENCE_FLOOR),
        ))

    worst = max((g.verdict for g in gates), key=lambda v: _verdict_rank(v), default=GateVerdict.PASS)
    blocking = next((g for g in gates if g.verdict in (GateVerdict.BLOCK, GateVerdict.ESCALATE)), None)
    return GateCheckResult(
        passed=worst in (GateVerdict.PASS, GateVerdict.WARN),
        verdict=worst,
        gates=gates,
        blocking_gate=blocking,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_empty_evidence(evidence: dict) -> int:
    """Count evidence keys that returned empty/error results."""
    empty = 0
    for key, val in evidence.items():
        if key.startswith("_"):
            continue
        if not val:
            empty += 1
            continue
        if isinstance(val, dict):
            if val.get("error") or (not any(
                v for k, v in val.items() if k not in ("error", "status") and v
            )):
                empty += 1
        elif isinstance(val, (list,)) and len(val) == 0:
            empty += 1
    return empty


def _count_unique_sources(evidence: dict) -> int:
    """Count distinct non-empty evidence source categories."""
    _SOURCE_MAP = {
        "logs": "splunk", "log_data": "splunk", "change_data": "splunk",
        "metrics": "sysdig", "metric_data": "sysdig", "golden_signals": "sysdig",
        "apm_data": "dynatrace", "apm": "dynatrace",
        "itsm_context": "servicenow", "cmdb_blast_radius": "servicenow",
        "diff_analysis": "github", "devops_data": "github",
    }
    sources: set[str] = set()
    for key, val in evidence.items():
        if key.startswith("_") or not val:
            continue
        if isinstance(val, dict) and val.get("error"):
            continue
        src = _SOURCE_MAP.get(key, key)
        sources.add(src)
    return len(sources)


def _rc_cited(root_cause: str, citation: dict) -> bool:
    """Check if a citation supports the root cause."""
    claim = citation.get("claim", "").lower()
    rc_words = set(root_cause.lower().split())
    claim_words = set(claim.split())
    return len(rc_words & claim_words) >= 2


def check_source_staleness(
    evidence: dict,
    collected_at: str | None = None,
) -> GateCheckResult:
    """G6 — StaleEvidence: warn when most evidence sources are stale.

    Uses source_confidence staleness model. Fires when stale_fraction
    of non-empty sources have freshness_factor < 0.50.

    Args:
        evidence:     The evidence dict from the investigation.
        collected_at: ISO timestamp when evidence was collected (used for
                      age computation if individual timestamps not present).

    Returns:
        GateCheckResult with G6 verdict.
    """
    if not GATES_ENABLED:
        return GateCheckResult(passed=True, verdict=GateVerdict.PASS)

    try:
        from supervisor.retrieval.source_confidence import score_evidence_dict
        scores = score_evidence_dict(evidence, collected_at=collected_at)
    except Exception as exc:
        logger.debug("G6 source_confidence import failed: %s", exc)
        return GateCheckResult(passed=True, verdict=GateVerdict.PASS)

    if not scores:
        return GateCheckResult(passed=True, verdict=GateVerdict.PASS, gates=[
            GateResult("G6_StaleEvidence", GateVerdict.PASS,
                       "No evidence sources to evaluate", 0.0, STALE_FRACTION)
        ])

    stale = [s for s in scores.values() if s.is_stale()]
    fraction = len(stale) / len(scores)

    if fraction >= STALE_FRACTION:
        stale_keys = [s.source_type for s in stale][:5]
        r = GateResult(
            gate_name="G6_StaleEvidence",
            verdict=GateVerdict.WARN,
            reason=(
                f"{len(stale)}/{len(scores)} evidence sources are stale "
                f"(freshness < 50%). Stale: {stale_keys}. "
                "RCA confidence may be degraded."
            ),
            metric_value=round(fraction, 3),
            threshold=STALE_FRACTION,
        )
        logger.info("Gate G6 WARN: stale_fraction=%.2f (%d/%d)", fraction, len(stale), len(scores))
        return GateCheckResult(passed=True, verdict=GateVerdict.WARN, gates=[r])

    return GateCheckResult(passed=True, verdict=GateVerdict.PASS, gates=[
        GateResult(
            gate_name="G6_StaleEvidence",
            verdict=GateVerdict.PASS,
            reason=f"stale_fraction={fraction:.1%} < {STALE_FRACTION:.1%}",
            metric_value=round(fraction, 3),
            threshold=STALE_FRACTION,
        )
    ])


def _verdict_rank(v: GateVerdict) -> int:
    return {GateVerdict.PASS: 0, GateVerdict.WARN: 1, GateVerdict.ESCALATE: 2, GateVerdict.BLOCK: 3}[v]
