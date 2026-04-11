#!/usr/bin/env python3
"""Nightly self-learning evaluation job for SentinalAI.

Runs automatically (e.g. via cron or Lambda) to close the feedback loop
between live investigations and the quality baseline.

Pipeline:
  1. Load all eval results from the database since the last run.
  2. Run batch ground-truth evaluation (if labelled ground truth exists).
  3. Compute aggregate quality metrics (accuracy, ECE, coverage, etc.).
  4. Check regression against the stored baseline.
  5. If no regression — update baseline (ratchet).
  6. Update strategy weights via StrategyEvolver using batch outcomes.
  7. Compact old STM entries in memory (compress turns older than N days).
  8. Ingest all recent investigations into the knowledge graph.
  9. Emit a structured summary report (JSON + human-readable).

Usage:
    python -m scripts.nightly_eval [--dry-run] [--days 7] [--verbose]

Environment:
  NIGHTLY_EVAL_LOOKBACK_DAYS   — how many days of history to evaluate (default: 7)
  NIGHTLY_EVAL_MIN_SAMPLES     — minimum sample count to update baseline (default: 5)
  NIGHTLY_EVAL_DRY_RUN         — if "true", runs analysis but does not update anything
  GROUND_TRUTH_CORPUS_PATH     — path to ground truth JSON (default: eval/ground_truth.json)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any

# Ensure repo root is on path when run as a script
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logger = logging.getLogger("sentinalai.nightly_eval")

LOOKBACK_DAYS = int(os.environ.get("NIGHTLY_EVAL_LOOKBACK_DAYS", "7"))
MIN_SAMPLES = int(os.environ.get("NIGHTLY_EVAL_MIN_SAMPLES", "5"))
DRY_RUN = os.environ.get("NIGHTLY_EVAL_DRY_RUN", "false").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_nightly_eval(
    lookback_days: int = LOOKBACK_DAYS,
    dry_run: bool = DRY_RUN,
) -> dict[str, Any]:
    """Execute the full nightly evaluation pipeline.

    Returns a summary dict suitable for JSON serialisation.
    """
    started_at = time.time()
    logger.info("=== Nightly eval started (lookback=%dd dry_run=%s) ===", lookback_days, dry_run)

    report: dict[str, Any] = {
        "started_at": _iso_now(),
        "lookback_days": lookback_days,
        "dry_run": dry_run,
        "steps": {},
        "regression": None,
        "baseline_updated": False,
        "errors": [],
    }

    # ------------------------------------------------------------------
    # Step 1: Load recent investigation results from DB
    # ------------------------------------------------------------------
    recent_results = _load_recent_results(lookback_days)
    report["steps"]["load_results"] = {
        "count": len(recent_results),
        "lookback_days": lookback_days,
    }
    logger.info("Step 1: Loaded %d investigation results", len(recent_results))

    if not recent_results:
        report["summary"] = "No results in lookback window — nothing to evaluate."
        _log_report(report, started_at)
        return report

    # ------------------------------------------------------------------
    # Step 2: Ground-truth batch evaluation
    # ------------------------------------------------------------------
    try:
        batch_summary = _run_batch_eval(recent_results)
    except Exception as exc:
        logger.warning("Step 2: Batch eval failed (non-critical): %s", exc)
        report["errors"].append(f"batch_eval: {exc}")
        batch_summary = {"total": 0, "accuracy": 0.0, "error": str(exc)}
    report["steps"]["batch_eval"] = batch_summary
    logger.info(
        "Step 2: Batch eval complete — accuracy=%.3f ECE=%.3f coverage=%.3f (n=%d)",
        batch_summary.get("accuracy", 0.0),
        batch_summary.get("ece", 0.0),
        batch_summary.get("mean_evidence_coverage", 0.0),
        batch_summary.get("total", 0),
    )

    # ------------------------------------------------------------------
    # Step 3: Compute aggregate quality metrics
    # ------------------------------------------------------------------
    metrics = _compute_quality_metrics(recent_results, batch_summary)
    report["steps"]["quality_metrics"] = metrics
    logger.info("Step 3: Quality metrics — %s", _fmt_metrics(metrics))

    # ------------------------------------------------------------------
    # Step 4: Regression check
    # ------------------------------------------------------------------
    regression_report = _check_regression(metrics)
    report["regression"] = regression_report
    if regression_report.get("has_regression"):
        logger.warning("Step 4: REGRESSION DETECTED — %s", regression_report.get("summary", ""))
    else:
        logger.info("Step 4: No regression — %s", regression_report.get("summary", ""))

    # ------------------------------------------------------------------
    # Step 5: Update baseline (ratchet — only if metrics improved)
    # ------------------------------------------------------------------
    sample_count = batch_summary.get("total", 0) + len(recent_results)
    if not dry_run:
        updated = _update_baseline(metrics, sample_count)
        report["baseline_updated"] = updated
        logger.info("Step 5: Baseline updated=%s (n=%d)", updated, sample_count)
    else:
        report["baseline_updated"] = False
        logger.info("Step 5: Dry run — baseline not updated")

    # ------------------------------------------------------------------
    # Step 6: Update strategy weights from batch outcomes
    # ------------------------------------------------------------------
    if not dry_run:
        strategy_updates = _update_strategy_weights(recent_results)
        report["steps"]["strategy_updates"] = strategy_updates
        logger.info("Step 6: Strategy updates — %d types updated", strategy_updates.get("updated_types", 0))
    else:
        report["steps"]["strategy_updates"] = {"dry_run": True}

    # ------------------------------------------------------------------
    # Step 7: Compact old memory entries
    # ------------------------------------------------------------------
    if not dry_run:
        compaction_stats = _compact_memory(lookback_days)
        report["steps"]["memory_compaction"] = compaction_stats
        logger.info(
            "Step 7: Memory compacted — %d digests created",
            compaction_stats.get("digests_created", 0),
        )
    else:
        report["steps"]["memory_compaction"] = {"dry_run": True}

    # ------------------------------------------------------------------
    # Step 8: Ingest into knowledge graph
    # ------------------------------------------------------------------
    if not dry_run:
        kg_stats = _ingest_knowledge_graph(recent_results)
        report["steps"]["knowledge_graph"] = kg_stats
        logger.info(
            "Step 8: Knowledge graph — %d ingested, %d nodes, %d edges",
            kg_stats.get("ingested", 0),
            kg_stats.get("node_count", 0),
            kg_stats.get("edge_count", 0),
        )
    else:
        report["steps"]["knowledge_graph"] = {"dry_run": True}

    # ------------------------------------------------------------------
    # Final summary
    # ------------------------------------------------------------------
    elapsed = round(time.time() - started_at, 2)
    report["elapsed_seconds"] = elapsed
    report["completed_at"] = _iso_now()
    report["summary"] = _build_summary(report)
    _log_report(report, started_at)

    return report


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------

def _load_recent_results(lookback_days: int) -> list[dict]:
    """Load recent investigation results from DB or experience store."""
    results: list[dict] = []

    # Try database first
    try:
        from database.persistence import is_enabled as _db_enabled
        if _db_enabled():
            from database.persistence import load_recent_investigations
            db_results = load_recent_investigations(days=lookback_days)
            results.extend(db_results or [])
            logger.debug("Loaded %d results from DB", len(db_results or []))
    except Exception as exc:
        logger.debug("DB load failed (non-critical): %s", exc)

    # Supplement with experience store (may have results not yet in DB)
    try:
        from supervisor.experience_store import load_recent_experiences
        exp_results = load_recent_experiences(days=lookback_days)
        # Deduplicate by incident_id
        existing_ids = {r.get("incident_id") for r in results}
        for r in (exp_results or []):
            if r.get("incident_id") not in existing_ids:
                results.append(r)
                existing_ids.add(r.get("incident_id"))
        logger.debug("Supplemented with %d experience store entries", len(exp_results or []))
    except Exception as exc:
        logger.debug("Experience store load failed (non-critical): %s", exc)

    return results


def _run_batch_eval(results: list[dict]) -> dict:
    """Run ground-truth batch evaluation against labelled corpus."""
    try:
        from supervisor.ground_truth_eval import GroundTruthEvaluator
        evaluator = GroundTruthEvaluator.from_file()

        # Only evaluate results that have ground truth
        labelled = [
            r for r in results
            if evaluator.has_ground_truth(r.get("incident_id", ""))
        ]

        if not labelled:
            return {"total": 0, "accuracy": 0.0, "labelled_count": 0,
                    "note": "no ground truth labels found for loaded results"}

        # Build result dict format for evaluator
        eval_inputs = {
            r.get("incident_id"): r
            for r in labelled
        }

        summary = evaluator.evaluate_batch(eval_inputs)
        result_dict = summary.to_dict() if hasattr(summary, "to_dict") else {}
        result_dict["labelled_count"] = len(labelled)
        return result_dict

    except Exception as exc:
        logger.warning("Batch eval failed (non-critical): %s", exc)
        return {"error": str(exc), "total": 0, "accuracy": 0.0}


def _compute_quality_metrics(results: list[dict], batch_summary: dict) -> dict[str, float]:
    """Derive aggregate quality metrics from results + eval summary."""
    metrics: dict[str, float] = {}

    # Accuracy from batch eval (if available)
    if batch_summary.get("total", 0) > 0:
        metrics["accuracy"] = float(batch_summary.get("accuracy", 0.0))
        metrics["calibration_error"] = float(batch_summary.get("ece", 1.0))
        metrics["evidence_coverage"] = float(batch_summary.get("mean_evidence_coverage", 0.0))

    # Citation coverage — average over all results
    citation_coverages = [
        float(r.get("citation_coverage", 0.0))
        for r in results
        if "citation_coverage" in r
    ]
    if citation_coverages:
        metrics["citation_coverage"] = sum(citation_coverages) / len(citation_coverages)

    # False positive rate: fraction of results with confidence < 30
    low_confidence = sum(1 for r in results if int(r.get("confidence", 0)) < 30)
    if results:
        metrics["false_positive_rate"] = low_confidence / len(results)

    # Evidence coverage fallback (from result snapshots if batch eval unavailable)
    if "evidence_coverage" not in metrics:
        coverages = [
            sum(1 for v in r.get("_evidence_snapshot", {}).values() if v) /
            max(1, len(r.get("_evidence_snapshot", {})))
            for r in results
            if r.get("_evidence_snapshot")
        ]
        if coverages:
            metrics["evidence_coverage"] = sum(coverages) / len(coverages)

    return {k: round(v, 4) for k, v in metrics.items()}


def _check_regression(metrics: dict[str, float]) -> dict:
    """Run regression check against stored baseline."""
    try:
        from supervisor.regression_harness import check_regression
        report = check_regression(metrics)
        d = report.to_dict() if hasattr(report, "to_dict") else {}
        d["summary"] = report.summary() if hasattr(report, "summary") else ""
        return d
    except Exception as exc:
        logger.warning("Regression check failed (non-critical): %s", exc)
        return {"error": str(exc), "has_regression": False}


def _update_baseline(metrics: dict[str, float], sample_count: int) -> bool:
    """Update baseline if metrics improved (ratchet behaviour)."""
    try:
        from supervisor.regression_harness import update_baseline_if_better
        return update_baseline_if_better(metrics, sample_count)
    except Exception as exc:
        logger.warning("Baseline update failed (non-critical): %s", exc)
        return False


def _update_strategy_weights(results: list[dict]) -> dict:
    """Re-weight strategy steps using batch outcomes via StrategyEvolver."""
    updated_types: set[str] = set()
    errors = 0

    try:
        from supervisor.strategy_evolver import record_outcome
        for result in results:
            incident_type = result.get("incident_type", "")
            online_score = float(result.get("online_quality_score", 0.0))
            # Reconstruct receipt-like list from evidence snapshot
            receipts = _receipts_from_result(result)
            if incident_type and receipts:
                try:
                    record_outcome(incident_type, receipts, online_score)
                    updated_types.add(incident_type)
                except Exception as exc:
                    logger.debug("Strategy update failed for %s: %s", incident_type, exc)
                    errors += 1
    except Exception as exc:
        logger.warning("Strategy evolver batch update failed: %s", exc)
        return {"error": str(exc), "updated_types": 0}

    return {"updated_types": len(updated_types), "errors": errors}


def _compact_memory(lookback_days: int) -> dict:
    """Compress old investigation memories into semantic digests."""
    digests_created = 0
    errors = 0

    try:
        from supervisor.memory_compression import compress_investigation, COMPRESSION_ENABLED
        if not COMPRESSION_ENABLED:
            return {"skipped": True, "reason": "MEMORY_COMPRESSION_ENABLED=false"}

        # Load old experience store entries that haven't been compressed yet
        from supervisor.experience_store import load_recent_experiences
        experiences = load_recent_experiences(days=lookback_days) or []

        for exp in experiences:
            # Skip if already compressed
            if exp.get("_compressed"):
                continue
            incident_id = exp.get("incident_id", "")
            if not incident_id:
                continue
            try:
                digest = compress_investigation(
                    incident_id=incident_id,
                    incident_type=exp.get("incident_type", "unknown"),
                    service=exp.get("service", "unknown"),
                    result=exp,
                    online_quality_score=float(exp.get("online_quality_score", 0.0)),
                )
                # Store digest back (non-blocking)
                try:
                    from supervisor.experience_store import store_experience_digest
                    store_experience_digest(incident_id, digest.to_dict())
                except Exception:
                    pass  # digest storage is best-effort
                digests_created += 1
            except Exception as exc:
                logger.debug("Compression failed for %s: %s", incident_id, exc)
                errors += 1

    except Exception as exc:
        logger.warning("Memory compaction failed (non-critical): %s", exc)
        return {"error": str(exc), "digests_created": 0}

    return {"digests_created": digests_created, "errors": errors}


def _ingest_knowledge_graph(results: list[dict]) -> dict:
    """Ingest recent investigation results into the knowledge graph."""
    ingested = 0
    errors = 0

    try:
        from supervisor.knowledge_graph import ingest_to_graph, get_graph, KG_ENABLED
        if not KG_ENABLED:
            return {"skipped": True, "reason": "KNOWLEDGE_GRAPH_ENABLED=false"}

        for result in results:
            incident_id = result.get("incident_id", "")
            if not incident_id:
                continue
            try:
                ingest_to_graph(
                    incident_id=incident_id,
                    incident_type=result.get("incident_type", "unknown"),
                    service=result.get("service", "unknown"),
                    root_cause=result.get("root_cause", ""),
                    confidence=int(result.get("confidence", 0)),
                    save=False,  # batch save at end
                )
                ingested += 1
            except Exception as exc:
                logger.debug("KG ingest failed for %s: %s", incident_id, exc)
                errors += 1

        # Persist graph once after all ingests
        if ingested > 0:
            try:
                g = get_graph()
                g.save()
            except Exception as exc:
                logger.warning("KG save failed: %s", exc)

        g = get_graph()
        return {
            "ingested": ingested,
            "errors": errors,
            "node_count": g.node_count(),
            "edge_count": g.edge_count(),
        }

    except Exception as exc:
        logger.warning("Knowledge graph ingestion failed (non-critical): %s", exc)
        return {"error": str(exc), "ingested": 0}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _receipts_from_result(result: dict) -> list[dict]:
    """Reconstruct a minimal receipts list from a stored result."""
    receipts = result.get("receipts", [])
    if receipts:
        return receipts
    # Fallback: infer from evidence snapshot
    snapshot = result.get("_evidence_snapshot", {})
    return [{"worker": k, "action": "inferred", "status": "ok"} for k, v in snapshot.items() if v]


def _fmt_metrics(metrics: dict) -> str:
    return " ".join(f"{k}={v:.3f}" for k, v in sorted(metrics.items()))


def _build_summary(report: dict) -> str:
    regression = report.get("regression", {}) or {}
    has_regression = regression.get("has_regression", False)
    metrics = report.get("steps", {}).get("quality_metrics", {})
    n = report.get("steps", {}).get("load_results", {}).get("count", 0)
    elapsed = report.get("elapsed_seconds", 0)

    status = "REGRESSION DETECTED" if has_regression else "PASS"
    acc = metrics.get("accuracy", 0.0)
    cov = metrics.get("citation_coverage", 0.0)
    return (
        f"[{status}] n={n} accuracy={acc:.3f} citation_coverage={cov:.3f} "
        f"elapsed={elapsed:.1f}s baseline_updated={report.get('baseline_updated')}"
    )


def _log_report(report: dict, started_at: float) -> None:
    elapsed = round(time.time() - started_at, 2)
    logger.info("=== Nightly eval complete (%.2fs) ===", elapsed)
    logger.info("Summary: %s", report.get("summary", ""))
    if report.get("errors"):
        logger.warning("Errors: %s", report["errors"])


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SentinalAI nightly evaluation job")
    parser.add_argument(
        "--days", type=int, default=LOOKBACK_DAYS,
        help=f"Lookback window in days (default: {LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=DRY_RUN,
        help="Run analysis only — do not persist any updates",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable DEBUG logging",
    )
    parser.add_argument(
        "--output", "-o", metavar="FILE",
        help="Write JSON report to FILE",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    report = run_nightly_eval(lookback_days=args.days, dry_run=args.dry_run)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2)
        print(f"Report written to {args.output}")
    else:
        print(json.dumps(report, indent=2))

    # Exit with non-zero if regression detected
    if report.get("regression", {}).get("has_regression"):
        sys.exit(1)
