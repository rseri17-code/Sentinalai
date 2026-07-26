"""EB-2 pipeline orchestrator — run the Investigation Evaluation Pipeline over a
corpus and produce a deterministic, replayable report.

For each scenario (deterministic ``task_id`` order):

    EFIC task -> render telemetry -> drive unmodified investigate() (isolated
    subprocess) -> capture trace -> evaluate (ground truth + investigation spec)
    -> per-scenario result.

Outcomes are explicit and never collapsed: ``PASS`` / ``FAIL`` / ``NOT_MEASURED``
/ ``ERROR``. The engine scoring low is a real, honest measurement — nothing is
tuned to inflate it.

Determinism: each scenario runs in a fresh isolated subprocess with empty learning
state (see ``execute``); the report excludes volatile timing fields from its
content hash. The engine's one repo-anchored side-effect (an episodic-memory
append that is never read back) is snapshotted and restored so a run leaves the
working tree unchanged.
"""
from __future__ import annotations

import json
import os
from typing import Any, Iterable, Mapping, Optional

from sentinel_core.eic import EIC_SCHEMA_VERSION, leaderboard

from enterprisebench.loader import canonical, sha256_16
from enterprisebench.pipeline import EB2_VERSION
from enterprisebench.pipeline.evaluate import MEASURED, evaluate
from enterprisebench.pipeline.execute import (EVALUATED_CONFIG, InvestigationError,
                                              run_investigation)

PASS = "PASS"
FAIL = "FAIL"
NOT_MEASURED = "NOT_MEASURED"
ERROR = "ERROR"

DEFAULT_PASS_THRESHOLD = 0.70

# Repo-anchored, write-only engine side-effects (never read back in the isolated
# config; the subprocess sandboxes them, this is a belt-and-suspenders restore).
_SIDE_EFFECT_FILES = (
    os.path.join("eval", "episodic_memory.jsonl"),
    os.path.join("eval", "causal_graph.jsonl"),
    os.path.join("eval", "resolution_knowledge.jsonl"),
)


def _default_corpus() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "eval", "efic", "corpus.json")


def load_efic_corpus(path: str | None = None) -> dict[str, Any]:
    with open(path or _default_corpus()) as f:
        return json.load(f)


class _RestoreFiles:
    """Snapshot each file's bytes on enter, restore on exit (crash-tolerant)."""

    def __init__(self, paths: Iterable[str]) -> None:
        self._paths = list(paths)
        self._snap: dict[str, bytes | None] = {}

    def __enter__(self) -> "_RestoreFiles":
        for p in self._paths:
            self._snap[p] = open(p, "rb").read() if os.path.exists(p) else None
        return self

    def __exit__(self, *exc: Any) -> None:
        for p, data in self._snap.items():
            try:
                if data is not None:
                    with open(p, "wb") as f:
                        f.write(data)
                elif os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


def _outcome(eic_score: Optional[float], threshold: float) -> str:
    if not isinstance(eic_score, (int, float)):
        return NOT_MEASURED
    return PASS if eic_score >= threshold else FAIL


def _scenario_result(entry: Mapping[str, Any], trace: Mapping[str, Any],
                     evaluation: Mapping[str, Any], outcome: str,
                     reason: str = "") -> dict[str, Any]:
    task, efic = entry["task"], entry.get("efic", {})
    eic = evaluation.get("eic", {})
    return {
        "scenario_id": str(task["task_id"]),
        "category": task.get("category"),
        "difficulty": task.get("difficulty"),
        "outcome": outcome,
        "reason": reason,
        "ground_truth_root_cause": task.get("ground_truth", {}).get("root_cause"),
        "engine_root_cause": trace.get("root_cause"),
        "engine_confidence": trace.get("confidence"),
        "engine_localized_service": trace.get("localized_service"),
        "eic_score": eic.get("eic_score"),
        "eic_dimensions": eic.get("dimensions", {}),
        "process": evaluation.get("process", {}),
        "investigation_score": evaluation.get("investigation_score"),
        "reachability": evaluation.get("reachability", {}),
        "servers_queried": trace.get("servers_queried", []),
        "rendered_channels_served": trace.get("rendered_channels_served", []),
        "render_provenance": trace.get("render_provenance", {}),
        "hypothesis_count": trace.get("hypothesis_count", 0),
    }


def run_corpus(corpus_path: str | None = None, *,
               only: Optional[Iterable[str]] = None,
               pass_threshold: float = DEFAULT_PASS_THRESHOLD,
               timeout: int = 180,
               restore_side_effects: bool = True) -> dict[str, Any]:
    """Run EB-2 over the EFIC corpus and return the deterministic report dict."""
    corpus = load_efic_corpus(corpus_path)
    entries = sorted(corpus.get("corpus", []),
                     key=lambda e: str(e["task"]["task_id"]))
    only_set = {str(x) for x in only} if only is not None else None

    results: list[dict[str, Any]] = []
    scored_for_lb: list[dict[str, Any]] = []

    ctx = _RestoreFiles(_SIDE_EFFECT_FILES) if restore_side_effects else _Nullctx()
    with ctx:
        for entry in entries:
            task = entry["task"]
            efic = entry.get("efic", {})
            tid = str(task["task_id"])
            if only_set is not None and tid not in only_set:
                continue
            try:
                trace = run_investigation(task, timeout=timeout)
            except (InvestigationError, Exception) as ex:  # never crash the run
                results.append({
                    "scenario_id": tid, "category": task.get("category"),
                    "difficulty": task.get("difficulty"), "outcome": ERROR,
                    "reason": f"investigation failed: {str(ex)[:300]}"})
                continue
            evaluation = evaluate(task, efic, trace)
            eic_score = evaluation.get("eic", {}).get("eic_score")
            outcome = _outcome(eic_score, pass_threshold)
            reach = evaluation.get("reachability", {})
            reason = ""
            if outcome == FAIL and reach.get("decisive_reachable") is False:
                reason = ("decisive evidence lives in an MCP the engine has no "
                          "native channel for (EB-3 simulator gap)")
            results.append(_scenario_result(entry, trace, evaluation, outcome,
                                            reason))
            if isinstance(eic_score, (int, float)):
                scored_for_lb.append({**evaluation["eic"], "engine": "sentinelai",
                                      "category": task.get("category"),
                                      "difficulty": task.get("difficulty")})

    return _assemble_report(corpus, results, scored_for_lb, pass_threshold)


def _assemble_report(corpus: Mapping[str, Any], results: list[dict[str, Any]],
                     scored_for_lb: list[dict[str, Any]],
                     pass_threshold: float) -> dict[str, Any]:
    counts = {o: sum(1 for r in results if r["outcome"] == o)
              for o in (PASS, FAIL, NOT_MEASURED, ERROR)}
    measured_eic = [r["eic_score"] for r in results
                    if isinstance(r.get("eic_score"), (int, float))]
    measured_inv = [r["investigation_score"] for r in results
                    if isinstance(r.get("investigation_score"), (int, float))]

    # Process-dimension coverage: how often each dim was measurable vs NOT_MEASURED.
    proc_measured: dict[str, int] = {}
    proc_positive: dict[str, float] = {}
    for r in results:
        for dim, cell in (r.get("process", {}) or {}).items():
            if cell.get("state") == MEASURED:
                proc_measured[dim] = proc_measured.get(dim, 0) + 1
                proc_positive[dim] = proc_positive.get(dim, 0.0) + (cell.get("raw") or 0)

    report = {
        "eb2_version": EB2_VERSION,
        "kind": "enterprisebench_eb2_investigation_evaluation",
        "corpus_schema_version": corpus.get("schema_version"),
        "corpus_hash": _corpus_hash(corpus),
        "scorer_version": EIC_SCHEMA_VERSION,
        "evaluated_config": EVALUATED_CONFIG,
        "pass_threshold": pass_threshold,
        "scenario_count": len(results),
        "counts": counts,
        "mean_eic_score": round(sum(measured_eic) / len(measured_eic), 4)
        if measured_eic else None,
        "mean_investigation_score": round(sum(measured_inv) / len(measured_inv), 4)
        if measured_inv else None,
        "aggregate": leaderboard(scored_for_lb) if scored_for_lb else None,
        "process_dimension_coverage": {
            dim: {"measured_count": proc_measured.get(dim, 0),
                  "positive_rate": round(proc_positive[dim] / proc_measured[dim], 4)
                  if proc_measured.get(dim) else None}
            for dim in sorted(set(list(proc_measured) + [
                d for r in results for d in (r.get("process", {}) or {})]))
        },
        "scenarios": sorted(results, key=lambda r: r["scenario_id"]),
    }
    report["content_hash"] = sha256_16(
        {k: v for k, v in report.items() if k not in ("content_hash",)})
    return report


def _corpus_hash(corpus: Mapping[str, Any]) -> str:
    return sha256_16({"schema_version": corpus.get("schema_version"),
                      "task_hashes": sorted(
                          str(e.get("task", {}).get("task_hash", ""))
                          for e in corpus.get("corpus", []))})


class _Nullctx:
    def __enter__(self) -> "_Nullctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def canonical_report(report: Mapping[str, Any]) -> str:
    return canonical(report)


__all__ = ["run_corpus", "load_efic_corpus", "canonical_report",
           "PASS", "FAIL", "NOT_MEASURED", "ERROR", "DEFAULT_PASS_THRESHOLD"]
