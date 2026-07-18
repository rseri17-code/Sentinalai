"""R2 Part B/C — evidence observability. Failing-first + acceptance.

Every evidence object terminates in exactly one state (used/filtered/
suppressed/unavailable/error). No silent loss: malformed responses, worker
exceptions, and future-await failures all become visible.
"""
from __future__ import annotations

from supervisor.phases.collect import (
    EVIDENCE_STATES,
    _evidence_lifecycle,
    _record_unavailable,
    _scan_worker_errors,
)


class TestRecordUnavailable:
    def test_records_state(self):
        ev = {}
        _record_unavailable(ev, "trace_correlation", "timeout", state="unavailable")
        assert ev["_sources_unavailable"] == [
            {"source": "trace_correlation", "reason": "timeout",
             "state": "unavailable"}]

    def test_invalid_state_falls_back(self):
        ev = {}
        _record_unavailable(ev, "x", "y", state="bogus")
        assert ev["_sources_unavailable"][0]["state"] == "unavailable"


class TestScanWorkerErrors:
    def test_error_dict_becomes_error_state(self):
        ev = {"search_logs": {"error": "connection refused"}}
        _scan_worker_errors(ev)
        e = ev["_sources_unavailable"][0]
        assert e["source"] == "search_logs" and e["state"] == "error"

    def test_malformed_raw_response_becomes_unavailable(self):
        ev = {"apm": {"raw_response": "<html>502</html>"}}
        _scan_worker_errors(ev)
        e = ev["_sources_unavailable"][0]
        assert e["source"] == "apm" and e["state"] == "unavailable"

    def test_healthy_source_not_flagged(self):
        ev = {"golden": {"latency": 1}}
        _scan_worker_errors(ev)
        assert "_sources_unavailable" not in ev


class TestEvidenceLifecycle:
    def test_every_object_has_terminal_state(self):
        ev = {"golden": {"latency": 1},          # used
              "logs": {"error": "boom"},          # error
              "empty": [],                         # filtered
              "_private": {"x": 1}}                # ignored
        _scan_worker_errors(ev)
        lc = _evidence_lifecycle(ev)
        assert lc["by_source"]["golden"] == "used"
        assert lc["by_source"]["logs"] == "error"
        assert lc["by_source"]["empty"] == "filtered"
        assert "_private" not in lc["by_source"]
        # no "unknown" state anywhere
        assert set(lc["by_source"].values()) <= set(EVIDENCE_STATES)

    def test_counts_sum_to_sources(self):
        ev = {"a": {"x": 1}, "b": {"error": "e"}, "c": None}
        _scan_worker_errors(ev)
        lc = _evidence_lifecycle(ev)
        assert sum(lc["counts"].values()) == len(lc["by_source"])

    def test_unavailable_source_without_key_still_listed(self):
        ev = {}
        _record_unavailable(ev, "knowledge_graph", "timeout", state="unavailable")
        lc = _evidence_lifecycle(ev)
        assert lc["by_source"]["knowledge_graph"] == "unavailable"

    def test_deterministic(self):
        ev1 = {"b": {"error": "e"}, "a": {"x": 1}}
        ev2 = {"a": {"x": 1}, "b": {"error": "e"}}
        _scan_worker_errors(ev1); _scan_worker_errors(ev2)
        assert _evidence_lifecycle(ev1) == _evidence_lifecycle(ev2)
