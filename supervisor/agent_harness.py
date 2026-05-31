"""Agent Harness — self-correcting, self-aware investigation orchestrator.

Wraps SentinalAISupervisor with three layers of self-awareness:

Layer 1 — Pre-flight context
  Loads calibration state, experience store matches, strategy quality, and PIL
  predictions before running the first investigation pass. This context is
  surfaced to the agent so it knows its own historical accuracy and similar
  past incidents before it starts.

Layer 2 — Multi-round self-correction loop
  After the initial investigation, scores quality via online_evaluator and
  self_critique. If the score is below HARNESS_QUALITY_GATE, runs a targeted
  gap-fill round: executes the gap_queries from the critique using the
  supervisor's workers, enriches evidence, and re-analyzes. Repeats up to
  HARNESS_MAX_ROUNDS times or until quality is satisfactory or improvement
  plateaus (stuck detection).

Layer 3 — Post-flight learning
  Records outcome to strategy_evolver, online quality to adaptive_thresholds,
  stores high-quality investigations in experience_store, runs the learning
  loop step, and emits a HARNESS_REFLECTION event so the UI shows what the
  agent learned and changed.

Configuration (environment variables):
  HARNESS_ENABLED           — master switch (default: true)
  HARNESS_MAX_ROUNDS        — correction rounds beyond the initial pass (default: 2)
  HARNESS_QUALITY_GATE      — minimum acceptable quality score (default: 0.70)
  HARNESS_MIN_IMPROVEMENT   — minimum score delta to keep iterating (default: 0.04)
  HARNESS_REFLECTION_LLM    — use LLM for reflection narrative (default: follows LLM_ENABLED)

Usage (from agui/api/investigations.py):
    from supervisor.agent_harness import run_with_harness
    result = run_with_harness(incident_id, investigation_id=investigation_id)
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("sentinalai.agent_harness")

HARNESS_ENABLED       = os.environ.get("HARNESS_ENABLED", "true").lower() in ("1", "true", "yes")
HARNESS_MAX_ROUNDS    = int(os.environ.get("HARNESS_MAX_ROUNDS", "2"))
HARNESS_QUALITY_GATE  = float(os.environ.get("HARNESS_QUALITY_GATE", "0.70"))
HARNESS_MIN_IMPROVEMENT = float(os.environ.get("HARNESS_MIN_IMPROVEMENT", "0.04"))
HARNESS_REFLECTION_LLM  = os.environ.get("HARNESS_REFLECTION_LLM", "").lower() not in ("0", "false", "no")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CorrectionRecord:
    """One self-correction round."""
    round_num: int
    score_before: float
    score_after: float
    gaps_addressed: list[str] = field(default_factory=list)
    gap_queries_run: int = 0
    improved: bool = False


@dataclass
class HarnessReflection:
    """Complete reflection record for one investigation run."""
    investigation_id: str
    incident_id: str

    # Quality trajectory
    initial_quality: float = 0.0
    final_quality: float = 0.0
    rounds_run: int = 1
    corrections: list[CorrectionRecord] = field(default_factory=list)
    stuck: bool = False

    # Confidence calibration
    confidence_raw: int = 0
    confidence_calibrated: int = 0

    # Pre-flight context
    experience_matches: int = 0
    similar_incident_types: list[str] = field(default_factory=list)
    calibration_ece: float = 0.0
    strategy_quality: float | None = None

    # Post-flight
    learning_updated: bool = False
    experience_stored: bool = False

    # Phase 4: pattern intelligence context
    pattern_match_count: int = 0
    pattern_top_hypothesis: str | None = None

    # Narrative
    narrative: str = ""

    # Timing
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "investigation_id": self.investigation_id,
            "incident_id": self.incident_id,
            "initial_quality": round(self.initial_quality, 3),
            "final_quality": round(self.final_quality, 3),
            "rounds_run": self.rounds_run,
            "corrections": [
                {
                    "round": c.round_num,
                    "score_before": round(c.score_before, 3),
                    "score_after": round(c.score_after, 3),
                    "gaps_addressed": c.gaps_addressed,
                    "gap_queries_run": c.gap_queries_run,
                    "improved": c.improved,
                }
                for c in self.corrections
            ],
            "stuck": self.stuck,
            "confidence_raw": self.confidence_raw,
            "confidence_calibrated": self.confidence_calibrated,
            "experience_matches": self.experience_matches,
            "similar_incident_types": self.similar_incident_types,
            "calibration_ece": round(self.calibration_ece, 4),
            "strategy_quality": self.strategy_quality,
            "learning_updated": self.learning_updated,
            "experience_stored": self.experience_stored,
            "pattern_match_count": self.pattern_match_count,
            "pattern_top_hypothesis": self.pattern_top_hypothesis,
            "narrative": self.narrative,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class InvestigationHarness:
    """Self-correcting, self-aware investigation orchestrator."""

    def __init__(self) -> None:
        self._supervisor: Any = None  # lazy init

    def run(
        self,
        incident_id: str,
        investigation_id: str = "",
    ) -> dict[str, Any]:
        """Run a self-correcting investigation.

        Falls back to a plain supervisor call if HARNESS_ENABLED is false
        or any harness-level failure occurs — the investigation always completes.
        """
        if not HARNESS_ENABLED:
            return self._plain_investigate(incident_id, investigation_id)

        t0 = time.monotonic()
        reflection = HarnessReflection(
            investigation_id=investigation_id,
            incident_id=incident_id,
        )

        try:
            # --- Layer 1: Pre-flight ---
            meta = self._load_meta_state(incident_id)
            reflection.experience_matches   = meta.get("experience_matches", 0)
            reflection.similar_incident_types = meta.get("similar_types", [])
            reflection.calibration_ece      = meta.get("calibration_ece", 0.0)
            reflection.strategy_quality     = meta.get("strategy_quality")

            self._emit_harness_event(investigation_id, incident_id, "pre_flight", meta)

            # --- Initial investigation pass ---
            supervisor = self._get_supervisor()
            result = supervisor.investigate(incident_id)

            if not result:
                return result or {}

            # Retrieve the cached evidence immediately after the first pass.
            # supervisor.investigate() stores evidence in TLS; get_last_evidence()
            # returns a copy so we can safely mutate it during gap filling.
            cached_evidence = supervisor.get_last_evidence()

            reflection.confidence_raw = result.get("confidence", 0)
            initial_score = result.get("_online_quality_score", 0.0)
            reflection.initial_quality = initial_score
            prev_score = initial_score

            # Phase 4: enrich reflection with pattern registry context
            try:
                from supervisor.pattern_registry import get_registry
                _fp = result.get("_dna_fingerprint", "")
                if _fp:
                    _prec = get_registry().get(_fp)
                    if _prec:
                        reflection.pattern_match_count = _prec.match_count
                        reflection.pattern_top_hypothesis = _prec.top_hypothesis()
            except Exception as exc:
                logger.debug("Harness: pattern meta enrichment failed: %s", exc)

            # --- Layer 2: Evidence-cached self-correction loop ---
            # Correction rounds call supervisor.reanalyze(enriched_evidence) instead
            # of supervisor.investigate() — skipping the expensive playbook re-run
            # (typically 90-110s saved per round).
            for round_num in range(1, HARNESS_MAX_ROUNDS + 1):
                critique_meta = result.get("_critique", {})
                score = critique_meta.get("score", initial_score)
                gaps = critique_meta.get("gaps", [])

                self._emit_harness_event(
                    investigation_id, incident_id, "round_evaluated",
                    {
                        "round": round_num,
                        "score": score,
                        "quality_gate": HARNESS_QUALITY_GATE,
                        "gaps": gaps,
                        "needs_correction": score < HARNESS_QUALITY_GATE,
                    },
                )

                if score >= HARNESS_QUALITY_GATE:
                    logger.info(
                        "Harness: quality gate met (score=%.3f >= %.2f) after round %d",
                        score, HARNESS_QUALITY_GATE, round_num,
                    )
                    break

                improvement = score - prev_score
                if round_num > 1 and improvement < HARNESS_MIN_IMPROVEMENT:
                    reflection.stuck = True
                    logger.info(
                        "Harness: stuck (improvement=%.4f < %.3f) — stopping after round %d",
                        improvement, HARNESS_MIN_IMPROVEMENT, round_num,
                    )
                    break

                if not gaps:
                    logger.debug("Harness: no gaps identified — stopping correction loop")
                    break

                gap_queries = self._build_gap_queries_from_critique(
                    critique_meta, result, incident_id
                )
                if not gap_queries:
                    break

                # Run only the targeted gap queries (fast — no playbook)
                evidence_patch, queries_run = self._run_gap_queries(
                    supervisor, gap_queries, incident_id
                )

                score_after = score
                if evidence_patch and cached_evidence:
                    enriched = {**cached_evidence, **evidence_patch}
                    try:
                        # reanalyze() skips incident fetch + playbook entirely:
                        # analyze → calibrate → cite → online-eval only.
                        re_result = supervisor.reanalyze(incident_id, enriched)
                        if re_result:
                            new_score = re_result.get("_online_quality_score", score)
                            new_conf  = re_result.get("confidence", 0)
                            orig_conf = result.get("confidence", 0)
                            if new_conf >= orig_conf:
                                result = re_result
                                cached_evidence = enriched  # next round uses richer evidence
                                score_after = new_score
                                logger.info(
                                    "Harness: round %d reanalyze improved conf %d→%d quality %.3f→%.3f",
                                    round_num, orig_conf, new_conf, score, score_after,
                                )
                            else:
                                logger.info(
                                    "Harness: round %d reanalyze did not improve (%d vs %d) — original kept",
                                    round_num, new_conf, orig_conf,
                                )
                    except Exception as exc:
                        logger.warning("Harness: reanalyze in round %d failed: %s", round_num, exc)
                elif not cached_evidence:
                    logger.warning("Harness: no cached evidence available for round %d — skipping", round_num)
                    break

                correction = CorrectionRecord(
                    round_num=round_num,
                    score_before=round(prev_score, 3),
                    score_after=round(score_after, 3),
                    gaps_addressed=gaps[:5],
                    gap_queries_run=queries_run,
                    improved=score_after > prev_score,
                )
                reflection.corrections.append(correction)
                reflection.rounds_run = round_num + 1
                prev_score = score_after

            # --- Confidence calibration ---
            reflection.confidence_raw = result.get("confidence", 0)
            calibrated = self._calibrate_confidence(reflection.confidence_raw)
            if calibrated != reflection.confidence_raw:
                result["confidence"] = calibrated
                result["confidence_raw"] = reflection.confidence_raw
            reflection.confidence_calibrated = calibrated

            # --- Final quality score ---
            reflection.final_quality = result.get("_critique", {}).get("score", initial_score)

            # --- Layer 3: Post-flight learning ---
            reflection.learning_updated = self._post_flight_learning(
                incident_id, result, reflection
            )
            experience_quality = result.get("_online_quality_score", 0.0)
            reflection.experience_stored = experience_quality >= 0.6

            # --- Reflection narrative ---
            reflection.narrative = self._generate_narrative(result, reflection)
            reflection.elapsed_ms = (time.monotonic() - t0) * 1000

            # Embed in result
            reflection_dict = reflection.to_dict()
            result["harness_reflection"] = reflection_dict

            # Persist replay metadata durably
            try:
                from database.ops_persistence import get_ops_store
                get_ops_store().persist_replay_meta(reflection_dict)
            except Exception:
                pass

            # Emit final reflection event
            self._emit_harness_event(
                investigation_id, incident_id, "reflection_complete",
                reflection.to_dict(),
            )

            logger.info(
                "Harness complete: inv=%s rounds=%d quality=%.3f→%.3f conf=%d→%d elapsed=%.0fms",
                investigation_id, reflection.rounds_run,
                reflection.initial_quality, reflection.final_quality,
                reflection.confidence_raw, reflection.confidence_calibrated,
                reflection.elapsed_ms,
            )
            return result

        except Exception as exc:
            logger.exception("Harness outer loop failed — falling back to plain investigation: %s", exc)
            return self._plain_investigate(incident_id, investigation_id)

    # ------------------------------------------------------------------
    # Pre-flight
    # ------------------------------------------------------------------

    def _load_meta_state(self, incident_id: str) -> dict[str, Any]:
        """Load calibration, strategy quality, and experience matches."""
        meta: dict[str, Any] = {
            "experience_matches": 0,
            "similar_types": [],
            "calibration_ece": 0.0,
            "strategy_quality": None,
        }

        # Calibration health
        try:
            from supervisor.confidence_calibrator import get_calibrator, _calibrator_lock
            with _calibrator_lock:
                cal = get_calibrator()
                report = cal.get_calibration_report()
            meta["calibration_ece"] = float(report.get("ece", 0.0))
            meta["calibration_samples"] = int(report.get("total_samples", 0))
        except Exception as exc:
            logger.debug("Meta-state: calibration read failed: %s", exc)

        # Strategy quality
        try:
            from supervisor.strategy_evolver import get_rolling_quality_stats
            rolling = get_rolling_quality_stats()
            meta["strategy_quality"] = rolling.get("avg")
            meta["strategy_status"] = rolling.get("status", "ok")
        except Exception as exc:
            logger.debug("Meta-state: strategy read failed: %s", exc)

        # Experience store matches (top-3 similar incidents)
        try:
            from supervisor.experience_store import retrieve_similar
            matches = retrieve_similar(incident_id=incident_id) or []
            meta["experience_matches"] = len(matches)
            meta["similar_types"] = list({m.get("incident_type", "") for m in matches[:5] if m.get("incident_type")})
        except Exception as exc:
            logger.debug("Meta-state: experience store read failed: %s", exc)

        return meta

    # ------------------------------------------------------------------
    # Gap queries
    # ------------------------------------------------------------------

    def _build_gap_queries_from_critique(
        self,
        critique_meta: dict,
        result: dict,
        incident_id: str,
    ) -> list[dict]:
        """Reconstruct gap queries from critique metadata."""
        gaps = critique_meta.get("gaps", [])
        incident_type = result.get("incident_type", "error_spike")
        service = result.get("service", "unknown")

        # Map gap descriptions to worker calls
        _gap_to_worker: dict[str, dict] = {
            "log":     {"worker": "log_worker",     "action": "get_error_logs",
                        "params": {"query": f"{incident_type} error", "limit": 50, "service": service}},
            "metric":  {"worker": "metrics_worker", "action": "query_metrics",
                        "params": {"metric_name": "error_rate", "window_minutes": 30, "service": service}},
            "signal":  {"worker": "apm_worker",     "action": "get_golden_signals",
                        "params": {"service": service}},
            "event":   {"worker": "apm_worker",     "action": "get_k8s_events",
                        "params": {"service": service}},
            "change":  {"worker": "log_worker",     "action": "get_change_data",
                        "params": {"service": service}},
            "confidence": {"worker": "log_worker",  "action": "search_logs",
                           "params": {"query": f"{incident_type} {service}", "limit": 100}},
        }

        queries: list[dict] = []
        seen: set[str] = set()
        for gap in gaps[:3]:
            gap_lower = gap.lower()
            for keyword, step in _gap_to_worker.items():
                if keyword in gap_lower and step["action"] not in seen:
                    queries.append(dict(step))
                    seen.add(step["action"])
                    break

        return queries

    def _run_gap_queries(
        self,
        supervisor: Any,
        gap_queries: list[dict],
        incident_id: str,
    ) -> tuple[dict, int]:
        """Execute gap queries using the supervisor's workers. Returns (evidence_patch, queries_run)."""
        evidence_patch: dict = {}
        queries_run = 0

        for step in gap_queries:
            worker_name = step.get("worker", "")
            action = step.get("action", "")
            params = step.get("params", {})

            if not worker_name or not action:
                continue

            worker = supervisor.workers.get(worker_name)
            if not worker:
                logger.debug("Gap query: worker %s not available", worker_name)
                continue

            try:
                method = getattr(worker, action, None)
                if method is None:
                    logger.debug("Gap query: action %s not found on %s", action, worker_name)
                    continue
                gap_result = method(**params)
                if gap_result and "error" not in (gap_result if isinstance(gap_result, dict) else {}):
                    evidence_patch[f"harness_gap_{action}"] = gap_result
                    queries_run += 1
                    logger.info("Harness gap evidence: %s.%s OK", worker_name, action)
            except Exception as exc:
                logger.debug("Gap query %s.%s failed: %s", worker_name, action, exc)

        return evidence_patch, queries_run

    # ------------------------------------------------------------------
    # Confidence calibration
    # ------------------------------------------------------------------

    def _calibrate_confidence(self, raw_confidence: int) -> int:
        try:
            from supervisor.confidence_calibrator import get_calibrator, _calibrator_lock
            with _calibrator_lock:
                cal = get_calibrator()
                return cal.calibrate(raw_confidence)
        except Exception:
            return raw_confidence

    # ------------------------------------------------------------------
    # Post-flight learning
    # ------------------------------------------------------------------

    def _post_flight_learning(
        self,
        incident_id: str,
        result: dict,
        reflection: HarnessReflection,
    ) -> bool:
        """Run all post-investigation learning updates. Non-blocking."""
        updated = False

        # Learning loop (ground truth eval if available)
        try:
            from supervisor.learning_loop import run_learning_step
            run_learning_step(incident_id, result)
            updated = True
        except Exception as exc:
            logger.debug("Post-flight: learning step failed: %s", exc)

        # Online quality → adaptive thresholds
        try:
            from supervisor.adaptive_thresholds import record_quality_observation
            score = result.get("_online_quality_score", 0.0)
            if score > 0:
                record_quality_observation(score)
        except Exception as exc:
            logger.debug("Post-flight: quality observation failed: %s", exc)

        # Strategy evolver — record all playbook steps as a batch
        try:
            from supervisor.strategy_evolver import record_outcome as _record_strategy
            incident_type = result.get("incident_type", "unknown")
            online_score = result.get("_online_quality_score", 0.0)
            steps = result.get("_evidence_snapshot", {})
            for step_label in steps:
                _record_strategy(incident_type, step_label, online_score)
        except Exception as exc:
            logger.debug("Post-flight: strategy evolver update failed: %s", exc)

        # Phase 4: Pattern outcome — use online quality score as correctness proxy.
        # Quality >= 0.70 (the harness gate) is treated as a correct investigation.
        try:
            from supervisor.learning_loop import _update_pattern_outcome
            fingerprint = result.get("_dna_fingerprint", "")
            root_cause = result.get("root_cause", "")
            quality = result.get("_online_quality_score", 0.0)
            if fingerprint and root_cause and quality > 0:
                _update_pattern_outcome(fingerprint, root_cause, quality >= HARNESS_QUALITY_GATE)
        except Exception as exc:
            logger.debug("Post-flight: pattern outcome update failed: %s", exc)

        return updated

    # ------------------------------------------------------------------
    # Reflection narrative
    # ------------------------------------------------------------------

    def _generate_narrative(self, result: dict, reflection: HarnessReflection) -> str:
        """Generate a human-readable reflection explaining what the agent changed."""
        if HARNESS_REFLECTION_LLM:
            narrative = self._llm_narrative(result, reflection)
            if narrative:
                return narrative

        # Fallback heuristic narrative
        lines: list[str] = []

        if reflection.corrections:
            improved = [c for c in reflection.corrections if c.improved]
            if improved:
                lines.append(
                    f"Self-correction improved confidence across {len(improved)} round(s). "
                    f"Quality trajectory: {reflection.initial_quality:.2f} → {reflection.final_quality:.2f}."
                )
                addressed = []
                for c in improved:
                    addressed.extend(c.gaps_addressed)
                if addressed:
                    lines.append(f"Evidence gaps addressed: {', '.join(set(addressed[:4]))}.")
            else:
                lines.append(
                    "Self-correction ran but found no improvement — original result retained."
                )

        if reflection.stuck:
            lines.append("Quality plateau detected; additional rounds would not improve results.")

        conf_delta = reflection.confidence_calibrated - reflection.confidence_raw
        if abs(conf_delta) >= 2:
            direction = "adjusted down" if conf_delta < 0 else "adjusted up"
            lines.append(
                f"Confidence {direction} {abs(conf_delta)}pp by calibrator "
                f"(historical ECE={reflection.calibration_ece:.3f})."
            )

        if reflection.experience_matches > 0:
            lines.append(
                f"Found {reflection.experience_matches} similar past investigation(s) "
                f"for context ({', '.join(reflection.similar_incident_types[:3])})."
            )

        if not lines:
            lines.append("Investigation completed within quality gate on first pass. No self-correction needed.")

        return " ".join(lines)

    def _llm_narrative(self, result: dict, reflection: HarnessReflection) -> str:
        """Use LLM to generate a richer reflection narrative (optional)."""
        try:
            from supervisor.llm import converse, is_enabled
            if not is_enabled():
                return ""

            corrections_summary = ""
            if reflection.corrections:
                corrections_summary = "; ".join(
                    f"Round {c.round_num}: {', '.join(c.gaps_addressed[:2])} "
                    f"(score {c.score_before:.2f}→{c.score_after:.2f})"
                    for c in reflection.corrections
                )

            system = (
                "You are the self-reflection module of an AI SRE agent. "
                "Produce a concise 2-3 sentence first-person explanation of what you did during "
                "this investigation — what you found lacking, how you corrected it, and what you learned. "
                "Be specific. Do not include filler phrases."
            )
            user = (
                f"Incident type: {result.get('incident_type', 'unknown')}\n"
                f"Service: {result.get('service', 'unknown')}\n"
                f"Root cause found: {result.get('root_cause', '')[:120]}\n"
                f"Final confidence: {reflection.confidence_calibrated}%\n"
                f"Quality: {reflection.initial_quality:.2f} → {reflection.final_quality:.2f}\n"
                f"Corrections: {corrections_summary or 'none'}\n"
                f"Experience matches: {reflection.experience_matches}\n"
                f"Stuck: {reflection.stuck}\n\n"
                "Write the self-reflection in first person."
            )

            resp = converse(system_prompt=system, user_message=user, max_tokens=120)
            return resp.get("text", "").strip()
        except Exception as exc:
            logger.debug("LLM narrative failed: %s", exc)
            return ""

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_harness_event(
        self,
        investigation_id: str,
        incident_id: str,
        phase: str,
        payload: dict,
    ) -> None:
        """Emit a HARNESS_REFLECTION event to the AGUI event bus."""
        try:
            import asyncio
            from agui.event_bus import get_bus
            from agui.schemas.events import AGUIEvent, EventType

            event = AGUIEvent(
                event_type=EventType.HARNESS_REFLECTION,
                investigation_id=investigation_id or "harness",
                incident_id=incident_id,
                payload={"phase": phase, **payload},
            )
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(get_bus().publish(event))
        except Exception as exc:
            logger.debug("Harness event emit failed: %s", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_supervisor(self) -> Any:
        from supervisor.agent import SentinalAISupervisor
        return SentinalAISupervisor()

    def _plain_investigate(self, incident_id: str, investigation_id: str) -> dict:
        """Plain investigation without harness (fallback)."""
        from supervisor.agent import SentinalAISupervisor
        supervisor = SentinalAISupervisor()
        return supervisor.investigate(incident_id) or {}


# ---------------------------------------------------------------------------
# Module-level singleton + public API
# ---------------------------------------------------------------------------

_harness: InvestigationHarness | None = None


def get_harness() -> InvestigationHarness:
    global _harness
    if _harness is None:
        _harness = InvestigationHarness()
    return _harness


def run_with_harness(incident_id: str, investigation_id: str = "") -> dict:
    """Public entry point — run a self-correcting investigation.

    Drop-in replacement for SentinalAISupervisor().investigate(incident_id).
    Falls back gracefully if harness fails.
    """
    return get_harness().run(incident_id, investigation_id=investigation_id)


def get_harness_status() -> dict:
    """Return current self-learning stack health for the /harness/status endpoint."""
    status: dict = {
        "harness_enabled": HARNESS_ENABLED,
        "max_rounds": HARNESS_MAX_ROUNDS,
        "quality_gate": HARNESS_QUALITY_GATE,
        "components": {},
        "overall_status": "OK",
    }

    # Calibration
    try:
        from supervisor.confidence_calibrator import get_calibrator, _calibrator_lock
        with _calibrator_lock:
            cal = get_calibrator()
            report = cal.get_calibration_report()
        status["components"]["confidence_calibrator"] = {
            "ece": report.get("ece", 0),
            "total_samples": report.get("total_samples", 0),
            "stale": cal.is_stale(),
        }
        if cal.is_stale():
            status["overall_status"] = "WARNING"
    except Exception as exc:
        status["components"]["confidence_calibrator"] = {"error": str(exc)}

    # Strategy evolver
    try:
        from supervisor.strategy_evolver import get_rolling_quality_stats
        rolling = get_rolling_quality_stats()
        status["components"]["strategy_evolver"] = rolling
        if rolling.get("status") == "degraded":
            status["overall_status"] = "DEGRADED"
    except Exception as exc:
        status["components"]["strategy_evolver"] = {"error": str(exc)}

    # Experience store
    try:
        from supervisor.experience_store import EXPERIENCE_STORE_PATH, _store_lock
        import json
        with _store_lock:
            with open(EXPERIENCE_STORE_PATH) as f:
                experiences = json.load(f)
        scores = [e.get("online_quality_score", 0) for e in experiences if e.get("online_quality_score")]
        status["components"]["experience_store"] = {
            "total": len(experiences),
            "avg_quality": round(sum(scores) / len(scores), 3) if scores else 0,
        }
    except FileNotFoundError:
        status["components"]["experience_store"] = {"total": 0, "avg_quality": 0}
    except Exception as exc:
        status["components"]["experience_store"] = {"error": str(exc)}

    # Adaptive thresholds drift
    try:
        from supervisor.adaptive_thresholds import detect_drift
        drift = detect_drift()
        drifted = [k for k, v in drift.items() if v.get("drifted")]
        status["components"]["adaptive_thresholds"] = {
            "drifted_count": len(drifted),
            "drifted_keys": drifted,
        }
        if drifted:
            if status["overall_status"] == "OK":
                status["overall_status"] = "WARNING"
    except Exception as exc:
        status["components"]["adaptive_thresholds"] = {"error": str(exc)}

    return status
