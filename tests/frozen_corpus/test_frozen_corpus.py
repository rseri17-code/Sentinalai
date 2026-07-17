"""R1 — Frozen Corpus & Hermetic Replay. Acceptance tests.

Proves the product contract from source: same incident + same corpus_version →
identical corpus reads; content-addressed version (no wall-clock/ordering);
snapshot immutability; no read-your-own-write; replay hermeticity (missing
snapshot fails, never reads live); concurrent isolation; learning preserved for
future investigations.
"""
from __future__ import annotations

import json
import threading

import pytest

from supervisor import frozen_corpus as fc


def _write(tmp_path, **stores):
    paths = {}
    defaults = {"pattern_registry": [], "evolved_strategy": {},
                "experience": {}, "knowledge_graph": {}}
    for name, empty in defaults.items():
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(stores.get(name, empty)))
        paths[name] = str(p)
    return paths


# ---------------------------------------------------------------------------
# Content addressing / determinism
# ---------------------------------------------------------------------------

class TestContentAddressing:
    def test_content_hash_stability(self, tmp_path):
        paths = _write(tmp_path, experience={"experiences": [{"rc": "db"}]})
        a = fc.capture(paths=paths)
        b = fc.capture(paths=paths)
        assert a.corpus_version == b.corpus_version

    def test_content_hash_changes_with_content(self, tmp_path):
        (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
        p1 = _write(tmp_path / "a", experience={"experiences": [{"rc": "db"}]})
        p2 = _write(tmp_path / "b", experience={"experiences": [{"rc": "dns"}]})
        assert fc.capture(paths=p1).corpus_version != \
            fc.capture(paths=p2).corpus_version

    def test_dict_ordering_stability(self, tmp_path):
        # same content, different key insertion order → same version (canonical)
        (tmp_path / "x").mkdir(); (tmp_path / "y").mkdir()
        px = _write(tmp_path / "x",
                    evolved_strategy={"b": 2, "a": 1})
        py = _write(tmp_path / "y",
                    evolved_strategy={"a": 1, "b": 2})
        assert fc.capture(paths=px).corpus_version == \
            fc.capture(paths=py).corpus_version

    def test_version_ignores_wall_clock(self, tmp_path):
        # capture twice with time passing → identical (no now()/mtime in hash)
        paths = _write(tmp_path, pattern_registry=[{"fingerprint": "f1"}])
        v1 = fc.capture(paths=paths).corpus_version
        import os
        os.utime(paths["pattern_registry"], (0, 0))   # change mtime
        v2 = fc.capture(paths=paths).corpus_version
        assert v1 == v2


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------

class TestImmutability:
    def test_snapshot_immutable_across_reads(self, tmp_path):
        paths = _write(tmp_path, experience={"experiences": [{"rc": "db"}]})
        c = fc.capture(paths=paths)
        got = c.experience
        got["experiences"].append({"rc": "injected"})       # mutate the copy
        assert c.experience == {"experiences": [{"rc": "db"}]}   # snapshot intact

    def test_frozen_dataclass(self, tmp_path):
        c = fc.capture(paths=_write(tmp_path))
        with pytest.raises(Exception):
            c.corpus_version = "tampered"       # frozen dataclass


# ---------------------------------------------------------------------------
# Active corpus lifecycle + replay guard
# ---------------------------------------------------------------------------

class TestActiveCorpus:
    def teardown_method(self):
        fc.clear_active_corpus()

    def test_no_active_corpus_reads_live(self):
        fc.clear_active_corpus()
        assert fc._frozen_or_live("experience") is None     # → live path

    def test_active_corpus_returns_frozen(self, tmp_path):
        c = fc.capture(paths=_write(tmp_path,
                                    experience={"experiences": [{"rc": "x"}]}))
        fc.set_active_corpus(c)
        assert fc._frozen_or_live("experience") == {"experiences": [{"rc": "x"}]}

    def test_no_read_your_own_write(self, tmp_path):
        # capture, then mutate the live file; active-corpus read still frozen
        paths = _write(tmp_path, experience={"experiences": [{"rc": "orig"}]})
        c = fc.capture(paths=paths)
        fc.set_active_corpus(c)
        # simulate a post-run write to the live store
        json.dump({"experiences": [{"rc": "mutated"}]},
                  open(paths["experience"], "w"))
        assert fc._frozen_or_live("experience")["experiences"][0]["rc"] == "orig"

    def test_missing_snapshot_fails_in_replay(self):
        fc.set_active_corpus(None, replay=True)
        with pytest.raises(fc.ReplayCorpusUnavailable):
            fc._frozen_or_live("pattern_registry")

    def test_replay_with_recorded_corpus_ok(self, tmp_path):
        c = fc.capture(paths=_write(tmp_path,
                                    pattern_registry=[{"fingerprint": "f"}]))
        fc.set_active_corpus(c, replay=True)
        assert fc._frozen_or_live("pattern_registry") == [{"fingerprint": "f"}]


# ---------------------------------------------------------------------------
# Concurrency isolation
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_concurrent_investigations_isolated(self, tmp_path):
        (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
        ca = fc.capture(paths=_write(tmp_path / "a",
                                     experience={"experiences": [{"rc": "A"}]}))
        cb = fc.capture(paths=_write(tmp_path / "b",
                                     experience={"experiences": [{"rc": "B"}]}))
        seen = {}

        def worker(name, corpus):
            fc.set_active_corpus(corpus)
            import time
            for _ in range(50):
                seen[name] = fc._frozen_or_live("experience")["experiences"][0]["rc"]
            fc.clear_active_corpus()

        ta = threading.Thread(target=worker, args=("a", ca))
        tb = threading.Thread(target=worker, args=("b", cb))
        ta.start(); tb.start(); ta.join(); tb.join()
        assert seen == {"a": "A", "b": "B"}     # no cross-contamination


# ---------------------------------------------------------------------------
# Record round-trip (replay artifact embedding) + learning preserved
# ---------------------------------------------------------------------------

class TestRecordRoundTrip:
    def test_to_record_from_record(self, tmp_path):
        c = fc.capture(paths=_write(tmp_path,
                                    knowledge_graph={"nodes": {"n": 1}}))
        rec = c.to_record()
        c2 = fc.FrozenCorpus.from_record(rec)
        assert c2.corpus_version == c.corpus_version
        assert c2.knowledge_graph == {"nodes": {"n": 1}}

    def test_stamp_fields(self, tmp_path):
        c = fc.capture(paths=_write(tmp_path), build_version="abc123")
        s = c.stamp()
        assert s["corpus_version"] == c.corpus_version
        assert s["build_version"] == "abc123"
        assert s["schema_version"] == fc.FROZEN_CORPUS_SCHEMA_VERSION

    def test_learning_preserved_after_clear(self, tmp_path):
        # after an investigation clears its corpus, reads go live again → future
        # investigations observe prior writes (learning preserved).
        fc.clear_active_corpus()
        assert fc.get_active_corpus() is None
        assert fc._frozen_or_live("experience") is None    # live path restored
