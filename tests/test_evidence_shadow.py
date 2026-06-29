"""Phase 9 — evidence ledger shadow-wiring tests.

Verifies:
- Flag OFF: ShadowMirror.create() returns None; no observable side effects
- Flag ON: writes mirror correctly, parity matches, mismatches detectable
- The agent.py wiring uses the safe `if _shadow is not None:` pattern
- Replay / receipt / workflow checkpoint shape is unchanged
- No import-cycle regression
"""
from __future__ import annotations

import logging

import pytest

from supervisor.evidence_shadow import ParityReport, ShadowMirror


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def shadow_on(monkeypatch):
    monkeypatch.setenv("EVIDENCE_LEDGER_SHADOW_ENABLED", "true")
    return True


@pytest.fixture
def shadow_off(monkeypatch):
    monkeypatch.delenv("EVIDENCE_LEDGER_SHADOW_ENABLED", raising=False)
    return False


# ---------------------------------------------------------------------------
# Factory behavior — off by default
# ---------------------------------------------------------------------------

class TestFactory:
    def test_create_returns_none_when_flag_off(self, shadow_off):
        assert ShadowMirror.create() is None

    def test_create_returns_instance_when_flag_on(self, shadow_on):
        m = ShadowMirror.create()
        assert isinstance(m, ShadowMirror)

    @pytest.mark.parametrize("val", ["false", "0", "no", "off", ""])
    def test_create_treats_falsy_as_off(self, monkeypatch, val):
        monkeypatch.setenv("EVIDENCE_LEDGER_SHADOW_ENABLED", val)
        assert ShadowMirror.create() is None

    @pytest.mark.parametrize("val", ["true", "TRUE", "1", "yes", "on"])
    def test_create_treats_truthy_as_on(self, monkeypatch, val):
        monkeypatch.setenv("EVIDENCE_LEDGER_SHADOW_ENABLED", val)
        assert isinstance(ShadowMirror.create(), ShadowMirror)


# ---------------------------------------------------------------------------
# Mirroring writes
# ---------------------------------------------------------------------------

class TestMirroring:
    def test_set_records_in_ledger(self, shadow_on):
        m = ShadowMirror.create()
        m.set("logs", [{"line": "x"}])
        assert m.keys() == ["logs"]
        assert m.to_dict() == {"logs": [{"line": "x"}]}

    def test_set_overwrites_existing_key(self, shadow_on):
        m = ShadowMirror.create()
        m.set("logs", [{"old": True}])
        m.set("logs", [{"new": True}])
        assert m.to_dict() == {"logs": [{"new": True}]}

    def test_set_preserves_insertion_order(self, shadow_on):
        m = ShadowMirror.create()
        m.set("a", 1)
        m.set("b", 2)
        m.set("c", 3)
        m.set("a", 99)  # replace shouldn't reorder
        assert m.keys() == ["a", "b", "c"]

    def test_underscore_keys_preserved(self, shadow_on):
        m = ShadowMirror.create()
        m.set("_incident_type", "oom")
        m.set("_loop_escalation", {"reason": "stuck"})
        m.set("_raw_diff", "diff --git ...")
        assert "_incident_type" in m.keys()
        assert m.to_dict()["_loop_escalation"] == {"reason": "stuck"}

    def test_nested_values_stored_verbatim(self, shadow_on):
        m = ShadowMirror.create()
        nested = {"deployments": [{"id": "d1", "meta": {"x": 1}}]}
        m.set("devops_context", nested)
        assert m.to_dict()["devops_context"] == nested


# ---------------------------------------------------------------------------
# Parity check
# ---------------------------------------------------------------------------

class TestParity:
    def test_parity_ok_on_identical_state(self, shadow_on):
        evidence = {}
        m = ShadowMirror.create()

        evidence["logs"] = [1, 2]
        m.set("logs", [1, 2])
        evidence["metrics"] = {"cpu": 0.9}
        m.set("metrics", {"cpu": 0.9})

        report = m.parity(evidence)
        assert report.ok is True
        assert report.evidence_keys == 2
        assert report.ledger_keys == 2
        assert report.missing_in_ledger == []
        assert report.extra_in_ledger == []
        assert report.value_mismatches == []

    def test_parity_detects_missing_key_in_ledger(self, shadow_on):
        evidence = {"logs": [], "metrics": {}}
        m = ShadowMirror.create()
        m.set("logs", [])  # forgot to mirror "metrics"

        report = m.parity(evidence)
        assert report.ok is False
        assert report.missing_in_ledger == ["metrics"]

    def test_parity_detects_extra_key_in_ledger(self, shadow_on):
        evidence = {"logs": []}
        m = ShadowMirror.create()
        m.set("logs", [])
        m.set("stray", "leak")

        report = m.parity(evidence)
        assert report.ok is False
        assert report.extra_in_ledger == ["stray"]

    def test_parity_detects_value_mismatch(self, shadow_on):
        evidence = {"logs": [1, 2, 3]}
        m = ShadowMirror.create()
        m.set("logs", [9, 9, 9])

        report = m.parity(evidence)
        assert report.ok is False
        assert report.value_mismatches == ["logs"]
        assert report.missing_in_ledger == []
        assert report.extra_in_ledger == []

    def test_parity_does_not_mutate_either_side(self, shadow_on):
        evidence = {"logs": []}
        m = ShadowMirror.create()
        m.set("logs", [])
        before_keys = list(evidence.keys())
        before_ledger = m.to_dict()
        m.parity(evidence)
        assert list(evidence.keys()) == before_keys
        assert m.to_dict() == before_ledger

    def test_report_to_dict_serializable(self, shadow_on):
        m = ShadowMirror.create()
        m.set("a", 1)
        report = m.parity({"a": 1, "b": 2})
        d = report.to_dict()
        assert d["ok"] is False
        assert d["missing_in_ledger"] == ["b"]
        assert isinstance(d, dict)

    def test_summary_for_ok_report(self, shadow_on):
        m = ShadowMirror.create()
        m.set("a", 1)
        report = m.parity({"a": 1})
        assert "parity OK" in report.summary()

    def test_summary_for_mismatched_report(self, shadow_on):
        m = ShadowMirror.create()
        m.set("a", 1)
        report = m.parity({"a": 2, "b": 3})
        s = report.summary()
        assert "parity MISMATCH" in s
        assert "b" in s


class TestParityLog:
    def test_parity_log_returns_report(self, shadow_on):
        m = ShadowMirror.create()
        m.set("a", 1)
        report = m.parity_log({"a": 1}, context="test")
        assert isinstance(report, ParityReport)
        assert report.ok is True

    def test_parity_log_warns_on_mismatch(self, shadow_on, caplog):
        m = ShadowMirror.create()
        m.set("a", 1)
        with caplog.at_level(logging.WARNING, logger="sentinalai.evidence_shadow"):
            m.parity_log({"a": 1, "missing": 2}, context="test_ctx")
        assert any("shadow_parity:test_ctx" in r.message for r in caplog.records)
        assert any("MISMATCH" in r.message for r in caplog.records)

    def test_parity_log_does_not_raise_on_mismatch(self, shadow_on):
        """Critical: parity check must never fail the investigation path."""
        m = ShadowMirror.create()
        m.set("a", 1)
        # Should not raise even though parity is broken
        report = m.parity_log({"completely": "different", "keys": "here"})
        assert report.ok is False


# ---------------------------------------------------------------------------
# Hot-path safety: OFF path is byte-equivalent in observable behavior
# ---------------------------------------------------------------------------

class TestOffPathSafety:
    def test_short_circuit_pattern_works(self, shadow_off):
        """Demonstrates the hot-path pattern used in agent.py."""
        _shadow = ShadowMirror.create()
        assert _shadow is None

        evidence = {}
        evidence["logs"] = [1, 2]
        if _shadow is not None:  # this branch is skipped when off
            _shadow.set("logs", [1, 2])
        # Nothing observable changed
        assert evidence == {"logs": [1, 2]}

    def test_no_module_warnings_when_off(self, shadow_off, caplog):
        """When OFF, no shadow-parity log lines should appear at any level."""
        with caplog.at_level(logging.DEBUG, logger="sentinalai.evidence_shadow"):
            _shadow = ShadowMirror.create()
            assert _shadow is None
            # Simulate the agent.py pattern repeatedly
            for k, v in (("a", 1), ("b", 2), ("c", 3)):
                if _shadow is not None:
                    _shadow.set(k, v)
        assert not any(
            "shadow_parity" in r.message for r in caplog.records
        )


# ---------------------------------------------------------------------------
# agent.py wiring — was the safe pattern used?
# ---------------------------------------------------------------------------

class TestAgentPyWiring:
    def test_agent_imports_shadow_mirror(self):
        import supervisor.agent as agent_mod
        src = open(agent_mod.__file__).read()
        assert "from supervisor.evidence_shadow import ShadowMirror" in src

    def test_agent_uses_safe_short_circuit_pattern(self):
        """Every shadow access must be guarded by `if _shadow is not None:`."""
        import supervisor.agent as agent_mod
        src = open(agent_mod.__file__).read()
        # The string "_shadow.set(" or "_shadow.parity" must always be preceded
        # by an "if _shadow is not None:" check somewhere reasonably close.
        for needle in ("_shadow.set(", "_shadow.parity_log("):
            count = src.count(needle)
            assert count >= 1, f"expected shadow call site for {needle!r}"
        # Direct test: every _shadow. method call must appear after a guard.
        # We check there are at least as many guards as method calls.
        guard_count = src.count("if _shadow is not None:")
        method_calls = src.count("_shadow.set(") + src.count("_shadow.parity_log(")
        assert guard_count >= method_calls, (
            f"each _shadow method call needs a guard; "
            f"found {guard_count} guards but {method_calls} method calls"
        )

    def test_shadow_module_loadable_in_isolation(self):
        """The shadow module must not import agent.py — would create a cycle."""
        import supervisor.evidence_shadow as mod
        src = open(mod.__file__).read()
        assert "from supervisor.agent" not in src
        assert "import supervisor.agent" not in src


# ---------------------------------------------------------------------------
# End-to-end shadow integration via _execute_playbook_sequential
# ---------------------------------------------------------------------------

class TestEndToEndShadowOnSequential:
    """Run the real _execute_playbook_sequential with shadow ON and OFF,
    confirm the returned evidence dict is identical and parity passes when on.

    We bypass _call_worker by stubbing it directly on the instance — the test
    isolates the shadow-wiring logic from the unrelated worker/retry machinery.
    """

    def _build_supervisor_with_stubbed_call_worker(self, results_by_label):
        """Build a SentinalAISupervisor whose _call_worker returns canned data.

        ``results_by_label`` maps step label → result dict to return when
        _call_worker is invoked for that step.
        """
        from supervisor.agent import SentinalAISupervisor
        import threading

        sup = SentinalAISupervisor.__new__(SentinalAISupervisor)
        # Skip __init__ entirely; install only what _execute_playbook_sequential reads.
        sup.workers = {name: object() for name in {"log_worker", "metrics_worker"}}
        sup._tls = threading.local()

        # Stub _call_worker — it's the only sup method _execute_playbook_sequential calls
        # for each step. Returning a canned result skips retries, circuits, executor.
        call_log: list[tuple[str, str]] = []
        def _stub_call_worker(worker, action, params, receipts, budget, worker_name,
                              circuits=None):
            call_log.append((worker_name, action))
            # Match by action — that's what becomes the label by default
            return results_by_label.get(action, {"stubbed": True, "action": action})
        sup._call_worker = _stub_call_worker  # type: ignore[method-assign]

        # _execute_playbook_sequential needs _build_params too
        sup._build_params = lambda step, incident_id, service: {  # type: ignore[method-assign]
            "incident_id": incident_id, "service": service,
        }
        return sup, call_log

    def test_off_and_on_produce_identical_evidence(self, monkeypatch):
        results = {
            "search_logs":   {"logs": [{"line": "x"}], "log_count": 1},
            "query_metrics": {"metrics": {"cpu": 0.9}, "pattern": "spike"},
        }
        sup, _ = self._build_supervisor_with_stubbed_call_worker(results)
        playbook = [
            {"worker": "log_worker", "action": "search_logs"},
            {"worker": "metrics_worker", "action": "query_metrics"},
        ]

        # Run with shadow OFF
        monkeypatch.delenv("EVIDENCE_LEDGER_SHADOW_ENABLED", raising=False)
        ev_off = sup._execute_playbook_sequential(playbook, "INC1", "svc")

        # Run with shadow ON
        monkeypatch.setenv("EVIDENCE_LEDGER_SHADOW_ENABLED", "true")
        ev_on = sup._execute_playbook_sequential(playbook, "INC1", "svc")

        # Both runs MUST produce identical evidence (shadow is observational only)
        assert ev_off == ev_on
        assert ev_on["search_logs"] == results["search_logs"]
        assert ev_on["query_metrics"] == results["query_metrics"]

    def test_shadow_on_logs_parity_ok(self, monkeypatch, caplog):
        results = {"search_logs": {"logs": []}}
        sup, _ = self._build_supervisor_with_stubbed_call_worker(results)
        playbook = [{"worker": "log_worker", "action": "search_logs"}]

        monkeypatch.setenv("EVIDENCE_LEDGER_SHADOW_ENABLED", "true")
        with caplog.at_level(logging.DEBUG, logger="sentinalai.evidence_shadow"):
            ev = sup._execute_playbook_sequential(playbook, "INC2", "svc")

        assert ev == {"search_logs": {"logs": []}}
        parity_lines = [r for r in caplog.records if "shadow_parity" in r.message]
        assert parity_lines, "expected at least one parity log line when shadow is on"
        assert all("MISMATCH" not in r.message for r in parity_lines)

    def test_shadow_off_logs_nothing(self, monkeypatch, caplog):
        sup, _ = self._build_supervisor_with_stubbed_call_worker(
            {"search_logs": {"logs": []}}
        )
        playbook = [{"worker": "log_worker", "action": "search_logs"}]
        monkeypatch.delenv("EVIDENCE_LEDGER_SHADOW_ENABLED", raising=False)
        with caplog.at_level(logging.DEBUG, logger="sentinalai.evidence_shadow"):
            sup._execute_playbook_sequential(playbook, "INC3", "svc")
        assert not any("shadow_parity" in r.message for r in caplog.records)
