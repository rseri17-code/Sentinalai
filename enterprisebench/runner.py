"""EB-0 runner — the deterministic evaluation pipeline.

Corpus Discovery → Schema Validation → Scenario Loading → Investigation
Execution (pluggable) → Ground-Truth Evaluation (REUSE eic.score_submission) →
Determinism check → Scenario Report → Aggregate Report. Every stage produces an
explicit status; nothing is silently skipped or swallowed.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Optional

from eval.enterprise.validate import check_expected
from sentinel_core.eic import EIC_SCHEMA_VERSION, leaderboard, score_submission

from enterprisebench.loader import (
    canonical,
    corpus_hash,
    load_corpus,
    validate_corpus,
)

# Explicit outcomes (never collapsed into a boolean).
PASS = "PASS"
FAIL = "FAIL"
SKIPPED = "SKIPPED"
UNSUPPORTED = "UNSUPPORTED"
NOT_MEASURED = "NOT_MEASURED"
ERROR = "ERROR"

DEFAULT_PASS_THRESHOLD = 0.70  # deterministic; documented + configurable

SubmissionProvider = Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]]


def no_engine_provider(task: Mapping[str, Any]) -> None:
    """Default provider: the corpus is NOT wired to the live engine (EB-2
    BenchMCPSource is not built), so no submission exists → NOT_MEASURED. This
    is honest; EB-0 never fabricates an investigation."""
    return None


def file_provider(submissions: Mapping[str, Mapping[str, Any]]) -> SubmissionProvider:
    """Provider backed by a {task_id: neutral-submission} map — how a future
    EB-2/engine run (or a test) supplies real submissions."""
    def _provider(task: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        return submissions.get(str(task.get("task_id", "")))
    return _provider


def _dimensions(score: Mapping[str, Any]) -> dict[str, Any]:
    """Per-dimension view: raw value + measured/NOT_MEASURED state.

    The EIC scorer nests dimensions under a ``dimensions`` dict (each value in
    [0,1] or None => NOT_MEASURED)."""
    dims = score.get("dimensions", {}) or {}
    return {k: {"raw": v, "state": NOT_MEASURED if v is None else "measured"}
            for k, v in dims.items()}


def _scenario_result(tid: str, task: Mapping[str, Any], outcome: str, *,
                     reason: str = "", score: Optional[Mapping[str, Any]] = None,
                     determinism: Optional[bool] = None,
                     expected_checks: Optional[Mapping[str, Any]] = None,
                     composite: Optional[float] = None) -> dict[str, Any]:
    return {
        "scenario_id": tid,
        "category": task.get("category"),
        "difficulty": task.get("difficulty"),
        "outcome": outcome,
        "reason": reason,
        "composite": composite,
        "scorer_version": EIC_SCHEMA_VERSION,
        "dimensions": _dimensions(score) if score else {},
        "expected_checks": dict(expected_checks) if expected_checks else {},
        "scorer_determinism": determinism,
    }


def run(
    corpus_path: str | None = None,
    *,
    provider: SubmissionProvider = no_engine_provider,
    only: Optional[Iterable[str]] = None,
    pass_threshold: float = DEFAULT_PASS_THRESHOLD,
    commit: str = "",
    run_timestamp: str = "",
) -> dict[str, Any]:
    """Run EB-0 over a corpus and return the report dict.

    ``provider`` maps a task → a neutral submission (or None). ``only`` filters
    scenario ids. Raises ``loader.CorpusError`` on invalid corpus (caller maps to
    the invalid-corpus exit code)."""
    corpus = load_corpus(corpus_path)
    scenarios = validate_corpus(corpus)          # deterministic order; may raise
    only_set = {str(x) for x in only} if only is not None else None

    results: list[dict[str, Any]] = []
    scored_for_lb: list[dict[str, Any]] = []

    for s in scenarios:
        task, expected = s["task"], s["expected"]
        tid = str(task["task_id"])

        if only_set is not None and tid not in only_set:
            results.append(_scenario_result(tid, task, SKIPPED, reason="filtered"))
            continue
        if task.get("schema_version") != EIC_SCHEMA_VERSION:
            results.append(_scenario_result(
                tid, task, UNSUPPORTED,
                reason=f"task schema_version {task.get('schema_version')!r} "
                       f"!= supported {EIC_SCHEMA_VERSION}"))
            continue

        try:
            submission = provider(task)
        except Exception as ex:  # provider must not crash the run
            results.append(_scenario_result(tid, task, ERROR,
                                            reason=f"provider error: {ex}"))
            continue

        if submission is None:
            results.append(_scenario_result(
                tid, task, NOT_MEASURED,
                reason="no engine submission (corpus not wired to the engine; "
                       "see EB-2 BenchMCPSource)"))
            continue

        try:
            score1 = score_submission(task, submission)
            score2 = score_submission(task, submission)   # scorer determinism
            determinism = canonical(score1) == canonical(score2)
            composite = score1.get("eic_score")
            outcome = (PASS if isinstance(composite, (int, float))
                       and composite >= pass_threshold else FAIL)
            checks = check_expected(expected, submission)
            results.append(_scenario_result(
                tid, task, outcome, score=score1, determinism=determinism,
                expected_checks=checks, composite=composite))
            scored_for_lb.append({
                **score1, "engine": submission.get("engine", "?"),
                "category": task.get("category"),
                "difficulty": task.get("difficulty")})
        except Exception as ex:
            results.append(_scenario_result(tid, task, ERROR,
                                            reason=f"scoring error: {ex}"))

    counts = {o: sum(1 for r in results if r["outcome"] == o)
              for o in (PASS, FAIL, SKIPPED, UNSUPPORTED, NOT_MEASURED, ERROR)}
    determinism_ok = all(r.get("scorer_determinism") is not False
                         for r in results)

    return {
        "enterprisebench_version": _eb_version(),
        "corpus_schema_version": corpus.get("schema_version"),
        "corpus_hash": corpus_hash(corpus),
        "scorer_version": EIC_SCHEMA_VERSION,
        "commit": commit,
        "run_timestamp": run_timestamp,          # volatile; excluded from hash
        "pass_threshold": pass_threshold,
        "scenario_count": len(scenarios),
        "executed": counts[PASS] + counts[FAIL],
        "counts": counts,
        "aggregate": leaderboard(scored_for_lb) if scored_for_lb else None,
        "scorer_determinism_ok": determinism_ok,
        "replay_status": NOT_MEASURED,           # engine replay = EB-2 (not wired)
        "replay_reason": "engine execution/replay against the corpus requires "
                         "EB-2 BenchMCPSource (not built); EB-0 validates scorer "
                         "determinism only",
        "unsupported": [r["scenario_id"] for r in results
                        if r["outcome"] == UNSUPPORTED],
        "failures": [{"scenario_id": r["scenario_id"], "reason": r["reason"]}
                     for r in results if r["outcome"] in (FAIL, ERROR)],
        "scenarios": results,
    }


def _eb_version() -> str:
    # avoid a circular import at module load; read lazily
    from enterprisebench import EB_VERSION
    return EB_VERSION


__all__ = [
    "run", "no_engine_provider", "file_provider", "DEFAULT_PASS_THRESHOLD",
    "PASS", "FAIL", "SKIPPED", "UNSUPPORTED", "NOT_MEASURED", "ERROR",
]
