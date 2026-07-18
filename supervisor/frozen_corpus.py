"""Frozen Corpus — R1 (Mission Alpha): pipeline determinism + hermetic replay.

Canonical refs: TRUTH_RECONCILIATION (d2a9061), CLAIM_RESTORATION (40b3cdf).

The authoritative investigation reads four learning stores that are also
written after each run, so run N mutates what run N+1 reads → non-deterministic
result and non-hermetic replay. This module makes those four stores ONE
immutable, content-addressed corpus captured once at ``investigate()`` entry.

The four stores (treated as one logical corpus):
    pattern_registry.json · evolved_strategy.json · experience_store.json ·
    knowledge_graph.json

Contract:
  * Captured once per investigation, immutable, shared, never re-read live.
  * ``corpus_version`` is content-addressed (sha256 of canonical JSON) — no
    timestamps/mtime/pid/hostname/uuid/object-identity influence it.
  * Learning still persists AFTER the investigation; the current run never
    observes its own writes; future runs do.
  * Replay consumes ONLY the recorded corpus; in replay mode a store read with
    no active corpus raises (fail verification) rather than reading live state.

Isolation is thread-local: an active corpus is set only for the duration of an
investigation, so every other caller of these stores is unaffected.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from typing import Any

FROZEN_CORPUS_SCHEMA_VERSION = 1

# The four authoritative stores + their default on-disk paths.
_STORE_PATHS = {
    "pattern_registry": os.getenv("PATTERN_REGISTRY_PATH",
                                  "eval/pattern_registry.json"),
    "evolved_strategy": os.getenv("EVOLVED_STRATEGY_PATH",
                                  "eval/evolved_strategy.json"),
    "experience": os.getenv("EXPERIENCE_STORE_PATH",
                            "eval/experience_store.json"),
    "knowledge_graph": os.getenv("KNOWLEDGE_GRAPH_PATH",
                                 "eval/knowledge_graph.json"),
}


class ReplayCorpusUnavailable(RuntimeError):
    """Raised when a store is read in replay mode with no recorded corpus.
    Replay must fail verification rather than silently read live state."""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class FrozenCorpus:
    """One immutable, content-addressed snapshot of the four learning stores.

    Each store blob is held as a canonical JSON string so the object is deeply
    immutable — accessors return a fresh parse, so a caller can never mutate the
    snapshot. ``corpus_version`` is a pure content hash.
    """
    schema_version: int
    corpus_version: str
    build_version: str
    _pattern_registry_json: str
    _evolved_strategy_json: str
    _experience_json: str
    _knowledge_graph_json: str

    # ---- immutable accessors (fresh copy each read) ----
    @property
    def pattern_registry(self) -> Any:
        return json.loads(self._pattern_registry_json)

    @property
    def evolved_strategy(self) -> Any:
        return json.loads(self._evolved_strategy_json)

    @property
    def experience(self) -> Any:
        return json.loads(self._experience_json)

    @property
    def knowledge_graph(self) -> Any:
        return json.loads(self._knowledge_graph_json)

    def stamp(self) -> dict[str, Any]:
        """The replay inputs recorded on every artifact."""
        return {
            "schema_version": self.schema_version,
            "corpus_version": self.corpus_version,
            "build_version": self.build_version,
        }

    def to_record(self) -> dict[str, Any]:
        """Serializable snapshot embedded in the replay artifact."""
        return {
            "schema_version": self.schema_version,
            "corpus_version": self.corpus_version,
            "build_version": self.build_version,
            "stores": {
                "pattern_registry": self._pattern_registry_json,
                "evolved_strategy": self._evolved_strategy_json,
                "experience": self._experience_json,
                "knowledge_graph": self._knowledge_graph_json,
            },
        }

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> "FrozenCorpus":
        s = rec.get("stores", {})
        return cls(
            schema_version=int(rec.get("schema_version",
                                       FROZEN_CORPUS_SCHEMA_VERSION)),
            corpus_version=str(rec.get("corpus_version", "")),
            build_version=str(rec.get("build_version", "")),
            _pattern_registry_json=s.get("pattern_registry", "[]"),
            _evolved_strategy_json=s.get("evolved_strategy", "{}"),
            _experience_json=s.get("experience", "{}"),
            _knowledge_graph_json=s.get("knowledge_graph", "{}"),
        )


def _read_store(path: str, empty: Any) -> Any:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        return empty


def capture(*, build_version: str = "",
            paths: dict[str, str] | None = None) -> FrozenCorpus:
    """Read the four stores once and build an immutable, content-addressed
    corpus. Pure w.r.t. wall-clock/pid — only the store *contents* determine
    ``corpus_version``."""
    p = paths or _STORE_PATHS
    pr = _canonical(_read_store(p["pattern_registry"], []))
    es = _canonical(_read_store(p["evolved_strategy"], {}))
    ex = _canonical(_read_store(p["experience"], {}))
    kg = _canonical(_read_store(p["knowledge_graph"], {}))
    # content-addressed version — canonical, no timestamps/mtime/pid/uuid
    digest = hashlib.sha256(
        "\x00".join((pr, es, ex, kg)).encode()).hexdigest()
    return FrozenCorpus(
        schema_version=FROZEN_CORPUS_SCHEMA_VERSION,
        corpus_version="corpus:" + digest[:32],
        build_version=build_version or os.getenv("SENTINELAI_BUILD", "unknown"),
        _pattern_registry_json=pr, _evolved_strategy_json=es,
        _experience_json=ex, _knowledge_graph_json=kg)


# ---------------------------------------------------------------------------
# Thread-local active corpus — set only for the duration of one investigation
# ---------------------------------------------------------------------------

_active = threading.local()


def set_active_corpus(corpus: FrozenCorpus | None, *, replay: bool = False) -> None:
    _active.corpus = corpus
    _active.replay = bool(replay)


def clear_active_corpus() -> None:
    _active.corpus = None
    _active.replay = False


def get_active_corpus() -> FrozenCorpus | None:
    return getattr(_active, "corpus", None)


def is_replay_mode() -> bool:
    return bool(getattr(_active, "replay", False))


def _frozen_or_live(attr: str):
    """Return the frozen store blob if an investigation-scoped corpus is active;
    None means 'read live' (no active investigation). In replay mode with no
    corpus, raise — replay must never fall back to live state."""
    corpus = get_active_corpus()
    if corpus is not None:
        return getattr(corpus, attr)
    if is_replay_mode():
        raise ReplayCorpusUnavailable(
            "replay requires a recorded FrozenCorpus; refusing to read live "
            f"{attr}")
    return None


__all__ = [
    "FROZEN_CORPUS_SCHEMA_VERSION", "FrozenCorpus", "ReplayCorpusUnavailable",
    "capture", "set_active_corpus", "clear_active_corpus", "get_active_corpus",
    "is_replay_mode", "_frozen_or_live",
]
