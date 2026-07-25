"""EB-0 corpus loader + strict validation.

Loads the existing enterprise corpus (`eval/enterprise/corpus.json`) and
validates it before execution. No silent coercion, no fabricated fields:
structural problems raise ``CorpusError`` (→ invalid-corpus exit); per-scenario
schema-version mismatches are surfaced as UNSUPPORTED at run time.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

SUPPORTED_CORPUS_SCHEMA = 1

_DEFAULT_CORPUS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "eval", "enterprise", "corpus.json")


class CorpusError(Exception):
    """Raised for a structurally invalid corpus (invalid-corpus exit code)."""


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sha256_16(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()[:16]


def load_corpus(path: str | None = None) -> dict[str, Any]:
    with open(path or _DEFAULT_CORPUS) as f:
        return json.load(f)


def corpus_hash(corpus: dict[str, Any]) -> str:
    """Deterministic content hash of the corpus scenarios (order-independent)."""
    ids = sorted(str(e.get("task", {}).get("task_id", ""))
                 for e in corpus.get("corpus", []))
    return sha256_16({"schema_version": corpus.get("schema_version"),
                      "task_hashes": sorted(
                          str(e.get("task", {}).get("task_hash", ""))
                          for e in corpus.get("corpus", [])),
                      "ids": ids})


def validate_corpus(corpus: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate + return scenarios in deterministic (task_id) order.

    Raises CorpusError on: non-object root, missing/empty corpus array,
    unsupported corpus schema, missing task/task_id, duplicate ids, missing
    ground-truth root cause, malformed necessary evidence, invalid confidence
    bounds.
    """
    if not isinstance(corpus, dict) or "corpus" not in corpus:
        raise CorpusError("corpus root must be an object containing a 'corpus' array")
    if corpus.get("schema_version") != SUPPORTED_CORPUS_SCHEMA:
        raise CorpusError(
            f"unsupported corpus schema_version: {corpus.get('schema_version')!r} "
            f"(supported: {SUPPORTED_CORPUS_SCHEMA})")
    entries = corpus["corpus"]
    if not isinstance(entries, list) or not entries:
        raise CorpusError("corpus.corpus must be a non-empty array")

    seen: set[str] = set()
    scenarios: list[dict[str, Any]] = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict) or not isinstance(e.get("task"), dict):
            raise CorpusError(f"entry {i}: missing 'task' object")
        task = e["task"]
        expected = e.get("expected", {}) or {}
        tid = task.get("task_id")
        if not tid:
            raise CorpusError(f"entry {i}: missing task_id")
        if tid in seen:
            raise CorpusError(f"duplicate scenario id: {tid}")
        seen.add(tid)

        gt = task.get("ground_truth", {}) or {}
        if not gt.get("root_cause"):
            raise CorpusError(f"{tid}: missing ground-truth root_cause")
        nec = gt.get("necessary_evidence")
        if not isinstance(nec, list) or not nec:
            raise CorpusError(f"{tid}: malformed/missing necessary_evidence")

        cmin, cmax = expected.get("confidence_min"), expected.get("confidence_max")
        if not (isinstance(cmin, (int, float)) and isinstance(cmax, (int, float))
                and 0 <= cmin <= cmax <= 100):
            raise CorpusError(
                f"{tid}: invalid confidence bounds {cmin!r}..{cmax!r}")

        scenarios.append({"task": task, "expected": expected})

    scenarios.sort(key=lambda s: str(s["task"]["task_id"]))
    return scenarios


__all__ = [
    "SUPPORTED_CORPUS_SCHEMA", "CorpusError", "load_corpus", "validate_corpus",
    "corpus_hash", "canonical", "sha256_16",
]
