"""Ground truth evaluation framework for SentinalAI.

Compares agent investigation outputs against a corpus of labeled ground truth
incidents. Provides objective accuracy metrics independent of the LLM-as-judge
(which is self-referential when the same LLM produces and evaluates results).

Evaluation dimensions:
- Root cause accuracy: exact/partial/miss against labeled root cause
- Confidence calibration: compares predicted confidence vs actual correctness
- Evidence coverage: did the agent examine the required evidence sources?
- Timeline accuracy: are timeline entries consistent with ground truth?

Usage:
    from supervisor.ground_truth_eval import GroundTruthEvaluator

    evaluator = GroundTruthEvaluator.from_file("eval/ground_truth.json")
    report = evaluator.evaluate(incident_id, result)
    summary = evaluator.evaluate_batch(results)

Ground truth format (JSON):
    [
        {
            "incident_id": "INC001",
            "root_cause": "Database connection pool exhaustion",
            "root_cause_keywords": ["connection pool", "database", "exhaustion"],
            "incident_type": "saturation",
            "service": "payment-service",
            "severity": 2,
            "required_evidence": ["logs", "metrics", "golden_signals"],
            "expected_confidence_min": 70,
            "expected_confidence_max": 95
        }
    ]
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("sentinalai.eval.ground_truth")

# Default path for ground truth corpus
DEFAULT_CORPUS_PATH = os.environ.get(
    "GROUND_TRUTH_CORPUS_PATH",
    os.path.join(os.path.dirname(__file__), "..", "eval", "ground_truth.json"),
)


@dataclass
class EvalResult:
    """Result of evaluating a single investigation against ground truth."""

    incident_id: str
    root_cause_match: str = "miss"  # exact, partial, miss
    root_cause_score: float = 0.0  # 0.0 to 1.0
    confidence_error: float = 0.0  # abs difference from expected midpoint
    confidence_calibrated: bool = False  # within expected range?
    evidence_coverage: float = 0.0  # fraction of required evidence found
    missing_evidence: list[str] = field(default_factory=list)
    predicted_confidence: int = 0
    actual_correct: bool = False  # root_cause_match in (exact, partial)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BatchEvalSummary:
    """Summary statistics for a batch evaluation."""

    total: int = 0
    exact_matches: int = 0
    partial_matches: int = 0
    misses: int = 0
    accuracy: float = 0.0  # (exact + partial) / total
    mean_root_cause_score: float = 0.0
    mean_confidence_error: float = 0.0
    calibration_score: float = 0.0  # fraction within expected range
    mean_evidence_coverage: float = 0.0
    ece: float = 0.0  # Expected Calibration Error
    results: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class GroundTruthEvaluator:
    """Evaluates agent outputs against labeled ground truth corpus."""

    def __init__(self, corpus: list[dict]):
        self._corpus: dict[str, dict] = {}
        for entry in corpus:
            iid = entry.get("incident_id", "")
            if iid:
                self._corpus[iid] = entry
        logger.info("Ground truth evaluator loaded: %d entries", len(self._corpus))

    @classmethod
    def from_file(cls, path: str | None = None) -> GroundTruthEvaluator:
        """Load ground truth corpus from a JSON file."""
        path = path or DEFAULT_CORPUS_PATH
        try:
            with open(path, "r") as f:
                corpus = json.load(f)
            return cls(corpus)
        except FileNotFoundError:
            logger.warning("Ground truth corpus not found: %s", path)
            return cls([])
        except json.JSONDecodeError as exc:
            logger.warning("Invalid ground truth JSON: %s", exc)
            return cls([])

    @property
    def incident_ids(self) -> list[str]:
        """Return all incident IDs in the corpus."""
        return list(self._corpus.keys())

    def has_ground_truth(self, incident_id: str) -> bool:
        """Check if ground truth exists for this incident."""
        return incident_id in self._corpus

    def evaluate(self, incident_id: str, result: dict) -> EvalResult | None:
        """Evaluate a single investigation result against ground truth.

        Returns EvalResult or None if no ground truth exists.
        """
        gt = self._corpus.get(incident_id)
        if not gt:
            return None

        # Root cause matching
        predicted_rc = result.get("root_cause", "").lower().strip()
        gt_rc = gt.get("root_cause", "").lower().strip()
        gt_keywords = [kw.lower() for kw in gt.get("root_cause_keywords", [])]

        rc_match, rc_score = self._match_root_cause(predicted_rc, gt_rc, gt_keywords)

        # Confidence calibration
        predicted_conf = result.get("confidence", 0)
        conf_min = gt.get("expected_confidence_min", 0)
        conf_max = gt.get("expected_confidence_max", 100)
        conf_midpoint = (conf_min + conf_max) / 2
        conf_error = abs(predicted_conf - conf_midpoint) / 100.0
        conf_calibrated = conf_min <= predicted_conf <= conf_max

        # Evidence coverage
        required = gt.get("required_evidence", [])
        evidence_timeline = result.get("evidence_timeline", [])
        evidence_sources = {e.get("source", "") for e in evidence_timeline}

        # Map evidence source names to categories
        coverage_map = {
            "logs": {"logs", "log_summary", "search_logs"},
            "metrics": {"metrics", "query_metrics", "get_resource_metrics"},
            "golden_signals": {"golden_signals", "get_golden_signals"},
            "events": {"events", "get_events"},
            "changes": {"changes", "get_change_data", "itsm_changes"},
        }

        found = 0
        missing = []
        for req in required:
            aliases = coverage_map.get(req, {req})
            if aliases & evidence_sources:
                found += 1
            else:
                missing.append(req)

        coverage = found / len(required) if required else 1.0
        is_correct = rc_match in ("exact", "partial")

        return EvalResult(
            incident_id=incident_id,
            root_cause_match=rc_match,
            root_cause_score=rc_score,
            confidence_error=round(conf_error, 3),
            confidence_calibrated=conf_calibrated,
            evidence_coverage=round(coverage, 3),
            missing_evidence=missing,
            predicted_confidence=predicted_conf,
            actual_correct=is_correct,
        )

    def evaluate_batch(self, results: dict[str, dict]) -> BatchEvalSummary:
        """Evaluate a batch of investigation results.

        Args:
            results: dict mapping incident_id -> investigation result

        Returns:
            BatchEvalSummary with aggregate metrics.
        """
        eval_results: list[EvalResult] = []

        for incident_id, result in results.items():
            er = self.evaluate(incident_id, result)
            if er:
                eval_results.append(er)

        if not eval_results:
            return BatchEvalSummary()

        total = len(eval_results)
        exact = sum(1 for r in eval_results if r.root_cause_match == "exact")
        partial = sum(1 for r in eval_results if r.root_cause_match == "partial")
        misses = total - exact - partial
        accuracy = (exact + partial) / total

        mean_rc = sum(r.root_cause_score for r in eval_results) / total
        mean_ce = sum(r.confidence_error for r in eval_results) / total
        calibrated = sum(1 for r in eval_results if r.confidence_calibrated) / total
        mean_ev = sum(r.evidence_coverage for r in eval_results) / total

        # Expected Calibration Error (ECE) — binned calibration metric
        ece = self._compute_ece(eval_results)

        return BatchEvalSummary(
            total=total,
            exact_matches=exact,
            partial_matches=partial,
            misses=misses,
            accuracy=round(accuracy, 3),
            mean_root_cause_score=round(mean_rc, 3),
            mean_confidence_error=round(mean_ce, 3),
            calibration_score=round(calibrated, 3),
            mean_evidence_coverage=round(mean_ev, 3),
            ece=round(ece, 4),
            results=[r.to_dict() for r in eval_results],
        )

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _match_root_cause(
        self, predicted: str, expected: str, keywords: list[str],
    ) -> tuple[str, float]:
        """Match predicted root cause against expected.

        Returns (match_type, score).
        """
        if not predicted or not expected:
            return ("miss", 0.0)

        # Exact substring match (normalized)
        if expected in predicted or predicted in expected:
            return ("exact", 1.0)

        # Keyword matching
        if keywords:
            matched_kw = sum(1 for kw in keywords if kw in predicted)
            kw_ratio = matched_kw / len(keywords)
            if kw_ratio >= 0.8:
                return ("exact", kw_ratio)
            if kw_ratio >= 0.4:
                return ("partial", kw_ratio)

        # Token overlap (Jaccard similarity)
        pred_tokens = set(re.findall(r'\w+', predicted))
        exp_tokens = set(re.findall(r'\w+', expected))
        if pred_tokens and exp_tokens:
            jaccard = len(pred_tokens & exp_tokens) / len(pred_tokens | exp_tokens)
            if jaccard >= 0.5:
                return ("partial", jaccard)

        return ("miss", 0.0)

    def _compute_ece(self, results: list[EvalResult], n_bins: int = 10) -> float:
        """Compute Expected Calibration Error.

        Groups predictions into confidence bins and measures the gap between
        average confidence and actual accuracy within each bin.
        """
        if not results:
            return 0.0

        bins: list[list[EvalResult]] = [[] for _ in range(n_bins)]
        for r in results:
            bin_idx = min(int(r.predicted_confidence / (100 / n_bins)), n_bins - 1)
            bins[bin_idx].append(r)

        ece = 0.0
        total = len(results)
        for bin_results in bins:
            if not bin_results:
                continue
            avg_conf = sum(r.predicted_confidence for r in bin_results) / len(bin_results) / 100.0
            avg_acc = sum(1 for r in bin_results if r.actual_correct) / len(bin_results)
            ece += len(bin_results) / total * abs(avg_conf - avg_acc)

        return ece
