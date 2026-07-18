"""Production Readiness Closure — regression coverage for blockers B-1/B-2/B-3/F-obs.

Each test pins a *documented* production blocker closed:
  B-1  change-query window must be wall-clock-independent (deterministic replay)
  B-2  DNA incident_hour must derive from the incident timestamp, not now()
  B-3  replay artifact + RCA report must serialize canonically (sort_keys)
  F-obs unavailable evidence sources must be recorded, never silently swallowed
"""
from __future__ import annotations

import json

from supervisor.agent import SentinalAISupervisor, _parse_incident_ts


# ---------------------------------------------------------------------------
# B-1 — change-query window anchored to immutable incident timestamps
# ---------------------------------------------------------------------------

class TestB1ChangeWindow:
    def _sup(self, incident):
        sup = SentinalAISupervisor()
        sup._tls.current_incident = incident
        return sup

    def test_window_independent_of_wall_clock(self):
        inc = {"created_at": "2026-01-01T08:00:00Z",
               "detected_at": "2026-01-01T14:00:00Z"}
        # two calls (wall clock advances between them) must agree
        w1 = self._sup(inc)._get_change_time_window()
        w2 = self._sup(inc)._get_change_time_window()
        assert w1 == w2

    def test_window_sized_from_incident_duration(self):
        # 6h incident duration + 2h buffer = 8
        inc = {"created_at": "2026-01-01T08:00:00Z",
               "detected_at": "2026-01-01T14:00:00Z"}
        assert self._sup(inc)._get_change_time_window() == 8

    def test_default_when_no_end_timestamp(self):
        inc = {"created_at": "2026-01-01T08:00:00Z"}
        assert self._sup(inc)._get_change_time_window() == 24

    def test_no_incident_defaults(self):
        sup = SentinalAISupervisor()
        sup._tls.current_incident = None
        assert sup._get_change_time_window() == 24

    def test_source_has_no_wall_clock(self):
        import inspect
        src = inspect.getsource(
            SentinalAISupervisor._get_change_time_window)
        assert "datetime.now" not in src
        assert "time.time" not in src


# ---------------------------------------------------------------------------
# B-2 — DNA incident_hour derived from the incident timestamp
# ---------------------------------------------------------------------------

class TestB2IncidentHour:
    def test_hour_from_incident_timestamp(self):
        sup = SentinalAISupervisor()
        sup._tls.current_incident = {"created_at": "2026-01-01T08:30:00Z"}
        flat = sup._build_dna_evidence_dict({}, {}, {"search_logs": {"x": 1}},
                                            "oomkill")
        assert flat.get("incident_hour") == 8

    def test_hour_deterministic_across_calls(self):
        sup = SentinalAISupervisor()
        sup._tls.current_incident = {"created_at": "2026-01-01T23:00:00Z"}
        a = sup._build_dna_evidence_dict({}, {}, {"x": {"y": 1}}, "oomkill")
        b = sup._build_dna_evidence_dict({}, {}, {"x": {"y": 1}}, "oomkill")
        assert a.get("incident_hour") == b.get("incident_hour") == 23

    def test_no_hour_when_no_timestamp(self):
        sup = SentinalAISupervisor()
        sup._tls.current_incident = {}
        flat = sup._build_dna_evidence_dict({}, {}, {"x": {"y": 1}}, "oomkill")
        assert "incident_hour" not in flat

    def test_source_has_no_wall_clock(self):
        import inspect
        src = inspect.getsource(SentinalAISupervisor._build_dna_evidence_dict)
        assert "datetime.now" not in src


class TestParseIncidentTs:
    def test_parses_z_suffix(self):
        assert _parse_incident_ts("2026-01-01T08:00:00Z").hour == 8

    def test_none_on_garbage(self):
        assert _parse_incident_ts("not-a-date") is None
        assert _parse_incident_ts("") is None
        assert _parse_incident_ts(None) is None
        assert _parse_incident_ts(12345) is None


# ---------------------------------------------------------------------------
# B-3 — canonical serialization of replay artifact + RCA report
# ---------------------------------------------------------------------------

class TestB3Canonical:
    def test_rca_report_json_sorted(self):
        from supervisor.rca_report import RCAReport
        import inspect
        assert "sort_keys=True" in inspect.getsource(RCAReport.to_json)

    def test_replay_save_sorted(self):
        from supervisor import replay
        import inspect
        assert "sort_keys=True" in inspect.getsource(replay.ReplayStore.save)

    def test_rca_report_byte_identical_across_key_order(self):
        from supervisor.rca_report import RCAReport
        import dataclasses
        # build two reports with the same content; canonical output must match
        fields = {f.name for f in dataclasses.fields(RCAReport)}
        kw = {}
        if "root_cause" in fields:
            kw["root_cause"] = "db pool exhaustion"
        if "confidence" in fields:
            kw["confidence"] = 80
        r1 = RCAReport(**kw)
        r2 = RCAReport(**kw)
        assert r1.to_json() == r2.to_json()
        # keys are emitted in sorted order
        loaded = json.loads(r1.to_json())
        assert list(loaded.keys()) == sorted(loaded.keys())


# ---------------------------------------------------------------------------
# F-obs — unavailable evidence sources are recorded, never silent
# ---------------------------------------------------------------------------

class TestFObsObservability:
    def test_record_unavailable_appends_and_dedups(self):
        from supervisor.phases.collect import _record_unavailable
        ev = {}
        _record_unavailable(ev, "experience_store", "timeout")
        _record_unavailable(ev, "experience_store", "timeout")   # dup
        _record_unavailable(ev, "knowledge_graph", "refused")
        assert ev["_sources_unavailable"] == [
            {"source": "experience_store", "reason": "timeout",
             "state": "unavailable"},
            {"source": "knowledge_graph", "reason": "refused",
             "state": "unavailable"},
        ]

    def test_worker_error_scan_records_sources(self):
        from supervisor.phases.collect import _scan_worker_errors
        ev = {"search_logs": {"error": "connection refused"},
              "golden_signals": {"latency": 1},
              "_private": {"error": "ignored"}}
        _scan_worker_errors(ev)
        srcs = {e["source"] for e in ev["_sources_unavailable"]}
        assert "search_logs" in srcs
        assert "golden_signals" not in srcs      # healthy source
        assert "_private" not in srcs            # underscore keys skipped

    def test_scan_is_deterministic(self):
        from supervisor.phases.collect import _scan_worker_errors
        ev1 = {"b_src": {"error": "x"}, "a_src": {"error": "y"}}
        ev2 = {"b_src": {"error": "x"}, "a_src": {"error": "y"}}
        _scan_worker_errors(ev1)
        _scan_worker_errors(ev2)
        assert ev1["_sources_unavailable"] == ev2["_sources_unavailable"]
        # sorted key iteration → a_src before b_src
        assert [e["source"] for e in ev1["_sources_unavailable"]] == [
            "a_src", "b_src"]
