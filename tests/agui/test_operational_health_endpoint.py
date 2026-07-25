"""Convergence tests — Operational Health wired to the runtime.

Proves the vertical slice: completed investigation states -> OIP adapter ->
sentinel_core.oip.operational_health, exposed by the agui endpoint, with a
drill-down link to the supporting investigation and honest signal coverage.
No investigation logic is duplicated; absent shadow signals are not fabricated.
"""
from __future__ import annotations

import os

# The agui auth module guards at import time (auth is required by default — the
# correct secure posture). Configure a test secret before importing any router.
os.environ.setdefault("AGUI_AUTH_REQUIRED", "false")

import asyncio  # noqa: E402

from agui.oip_adapter import states_to_oip_inputs, signal_coverage  # noqa: E402
from sentinel_core.models.incidents import (  # noqa: E402
    IncidentState, InvestigationStatus)


def _state(iid, service, rc, itype="saturation", status=InvestigationStatus.COMPLETED,
           confidence=80.0, corpus="corpus:abc", lifecycle=None, completed="2026-07-06T09:00:00Z"):
    return IncidentState(
        investigation_id=f"inv-{iid}", incident_id=iid,
        affected_service=service, incident_type=itype, severity="high",
        status=status, root_cause=rc, confidence=confidence,
        completed_at=completed, corpus_version=corpus,
        evidence_lifecycle=(lifecycle or {"counts": {"used": 5, "unavailable": 0,
                                                       "error": 0, "filtered": 0}}),
    )


class TestOipAdapter:
    def test_completed_states_map_to_oip_inputs(self):
        states = [
            _state("INC-1", "payments", "db pool exhaustion"),
            _state("INC-2", "checkout", "regression in deploy v4"),
        ]
        results, incidents, drilldown = states_to_oip_inputs(states)
        assert len(results) == 2
        assert {i["service"] for i in incidents.values()} == {"payments", "checkout"}
        # localization + R1/R2 signals carried from the real state
        r = next(r for r in results if r["incident_id"] == "INC-1")
        assert r["_causal_investigation"]["localization"]["root_cause_service"] == "payments"
        assert r["_corpus_version"] == "corpus:abc"
        assert r["_evidence_lifecycle"]["counts"]["used"] == 5

    def test_only_completed_states_included(self):
        states = [
            _state("INC-1", "payments", "cause", status=InvestigationStatus.RUNNING),
            _state("INC-2", "payments", "cause", status=InvestigationStatus.COMPLETED),
        ]
        results, _, _ = states_to_oip_inputs(states)
        assert [r["incident_id"] for r in results] == ["INC-2"]

    def test_drilldown_maps_service_to_latest_investigation(self):
        states = [
            _state("INC-1", "payments", "cause", completed="2026-07-01T00:00:00Z"),
            _state("INC-2", "payments", "cause", completed="2026-07-06T00:00:00Z"),
        ]
        _, _, drilldown = states_to_oip_inputs(states)
        assert drilldown["payments"] == "inv-INC-2"     # newest wins

    def test_missing_shadow_signals_not_fabricated(self):
        # no validation/causal engine output -> those keys simply absent
        results, _, _ = states_to_oip_inputs([_state("INC-1", "svc", "cause")])
        assert "_investigation_validation" not in results[0]
        assert "_decision_intelligence" not in results[0]

    def test_absent_corpus_stamp_stays_absent(self):
        results, _, _ = states_to_oip_inputs(
            [_state("INC-1", "svc", "cause", corpus=None)])
        assert "_corpus_version" not in results[0]

    def test_signal_coverage_is_honest(self):
        results, _, _ = states_to_oip_inputs([
            _state("INC-1", "payments", "cause"),
            _state("INC-2", "checkout", "cause", corpus=None),
        ])
        cov = signal_coverage(results)
        assert cov["investigations"] == 2
        assert cov["with_corpus_version"] == 1
        assert cov["with_validation_signals"] == 0
        assert cov["deferred_signals"]          # discloses off-by-default signals


class TestOperationalHealthEndpoint:
    def _seed_and_call(self, states):
        # Seed the runtime state store, then call the async handler directly
        # (avoids the middleware stack; proves the composition wiring).
        from agui.state_store import get_state_store
        from agui.api.operational_health import get_operational_health

        store = get_state_store()
        # InMemory backend: reach the underlying dict deterministically
        loop = asyncio.new_event_loop()
        try:
            for s in states:
                loop.run_until_complete(store.put_state(s))
            actor = object()  # get_actor is bypassed by calling handler directly
            return loop.run_until_complete(
                get_operational_health(limit=200, actor=actor))
        finally:
            loop.close()

    def test_endpoint_composes_operational_health_over_runtime(self):
        payload = self._seed_and_call([
            _state("INC-OH1", "payments", "db pool exhaustion"),
            _state("INC-OH2", "checkout", "regression in deploy"),
        ])
        # operational_health output shape present
        assert "services" in payload and "attention_order" in payload
        assert "payments" in payload["services"]
        # convergence extras
        assert payload["drilldown"]["payments"] == "inv-INC-OH1"
        assert payload["signal_coverage"]["investigations"] >= 2
        # verifiable flows from the R1 corpus stamp carried through the adapter
        assert payload["services"]["payments"]["verifiable"] is True
