"""Synthetic Enterprise Validation Platform — corpus loader + validator.

Loads the deterministic enterprise corpus and grades a set of engine
submissions against it, REUSING the EIC scorer (``score_submission``) for the
investigation dimensions and checking the operator-facing expectations (owner,
confidence range, recommendation keywords). It invents no results: with no
submissions it returns ``NOT_MEASURED`` — the corpus is the answer key, not a
claim about how any engine scores.
"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping

from sentinel_core.eic import leaderboard, score_submission

_HERE = os.path.dirname(os.path.abspath(__file__))


def load_corpus(path: str | None = None) -> dict:
    with open(path or os.path.join(_HERE, "corpus.json")) as f:
        return json.load(f)


def _tokens(s: str) -> set[str]:
    return {t for t in str(s or "").lower().replace("/", " ").split()
            if len(t) >= 3}


def check_expected(expected: Mapping[str, Any],
                   submission: Mapping[str, Any]) -> dict[str, Any]:
    """Operator-facing checks (owner / confidence / recommendation). Each is
    True / False / None(=NOT_MEASURED when the submission omits the field)."""
    owner_ok = None
    if submission.get("owner") is not None:
        owner_ok = str(submission.get("owner", "")) == expected.get("owner", "")

    conf = submission.get("confidence")
    conf_ok = None
    if isinstance(conf, (int, float)):
        conf_ok = (expected.get("confidence_min", 0) <= conf
                   <= expected.get("confidence_max", 100))

    rec_ok = None
    if submission.get("recommendation") is not None:
        exp_tokens = _tokens(expected.get("recommendation", ""))
        got_tokens = _tokens(submission.get("recommendation", ""))
        rec_ok = bool(exp_tokens & got_tokens) if exp_tokens else None

    return {"owner_ok": owner_ok, "confidence_in_range": conf_ok,
            "recommendation_match": rec_ok}


def validate(submissions: Mapping[str, Mapping[str, Any]] | None = None,
             corpus: dict | None = None) -> dict[str, Any]:
    """Grade submissions (keyed by task_id) against the corpus.

    ``submissions`` — {task_id: neutral-submission-dict}. With none provided,
    returns NOT_MEASURED (no engine has run against the corpus here)."""
    corpus = corpus or load_corpus()
    submissions = submissions or {}
    entries = corpus.get("corpus", [])

    if not submissions:
        return {
            "status": "NOT_MEASURED",
            "reason": "no engine submissions supplied; corpus is the answer key",
            "tasks": len(entries),
            "tool_sources_exercised": corpus.get("tool_sources_exercised", []),
        }

    per_task = []
    scores = []
    for e in entries:
        task = e["task"]
        tid = task["task_id"]
        sub = submissions.get(tid)
        if sub is None:
            per_task.append({"task_id": tid, "status": "NOT_MEASURED"})
            continue
        score = score_submission(task, sub)
        scores.append(score)
        per_task.append({
            "task_id": tid,
            "eic_score": score,
            "expected_checks": check_expected(e["expected"], sub),
        })

    return {
        "status": "measured",
        "tasks": len(entries),
        "graded": len(scores),
        "leaderboard": leaderboard(scores) if scores else None,
        "per_task": per_task,
    }


__all__ = ["load_corpus", "validate", "check_expected"]
