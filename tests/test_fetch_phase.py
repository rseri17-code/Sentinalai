"""Phase 10 — FetchPhase behavioral tests.

Verifies the first behavioral decomposition of supervisor.agent:
the FETCH stage now lives in supervisor.phases.fetch.FetchPhase and the
supervisor delegates to it. These tests use a fake supervisor surface so
they exercise the phase in isolation, then a real-supervisor wiring smoke
test confirms the integration.
"""
from __future__ import annotations

import threading

import pytest

from sentinel_core.context import ContextBuilder, InvestigationContext
from sentinel_core.evidence import EvidenceLedger
from supervisor.phases.contracts import PhaseResult, PhaseStatus
from supervisor.phases.fetch import FetchPhase


# ---------------------------------------------------------------------------
# Fake supervisor surface
# ---------------------------------------------------------------------------

class _FakeSupervisor:
    """Minimal stand-in for SentinalAISupervisor that FetchPhase needs.

    Exposes only the attributes / methods FetchPhase touches:
      - _tls           (threading.local — same contract as the real one)
      - INVESTIGATION_DEADLINE_SECONDS (class constant)
      - _fetch_incident(id, receipts, budget, circuits)
      - _empty_result(id, reason)
    """

    INVESTIGATION_DEADLINE_SECONDS = 600

    def __init__(self, fetch_result=None, empty_reason="No incident data available"):
        self._tls = threading.local()
        self._fetch_calls: list[str] = []
        self._fetch_result = fetch_result  # what _fetch_incident returns
        self._empty_reason = empty_reason

    def _fetch_incident(self, incident_id, receipts, budget, circuits):
        # Echo what was passed so tests can assert
        self._fetch_calls.append(incident_id)
        return self._fetch_result

    def _empty_result(self, incident_id, reason, **kwargs):
        return {
            "incident_id": incident_id,
            "root_cause": reason,
            "confidence": 10,
            "evidence_timeline": [],
            "reasoning": f"Investigation could not proceed: {reason}",
        }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestSuccessfulFetch:
    def test_returns_phase_result(self):
        sup = _FakeSupervisor(fetch_result={
            "summary": "checkout 5xx spike",
            "affected_service": "checkout",
        })
        ctx = ContextBuilder.for_incident("INC1")
        result = FetchPhase(sup).execute(ctx)
        assert isinstance(result, PhaseResult)
        assert result.phase == "fetch"
        assert result.status == PhaseStatus.COMPLETED
        assert result.ok is True

    def test_normal_path_no_early_return(self):
        sup = _FakeSupervisor(fetch_result={
            "summary": "checkout 5xx spike",
            "affected_service": "checkout",
        })
        out = FetchPhase(sup).execute(ContextBuilder.for_incident("INC1")).output.result
        assert out["early_return"] is None
        assert out["incident"]["summary"] == "checkout 5xx spike"
        assert out["summary"] == "checkout 5xx spike"
        assert out["service"] == "checkout"

    def test_defaults_service_when_missing(self):
        sup = _FakeSupervisor(fetch_result={"summary": "x"})
        out = FetchPhase(sup).execute(ContextBuilder.for_incident("INC1")).output.result
        assert out["service"] == "unknown"

    def test_defaults_summary_when_missing(self):
        sup = _FakeSupervisor(fetch_result={"affected_service": "x"})
        out = FetchPhase(sup).execute(ContextBuilder.for_incident("INC1")).output.result
        assert out["summary"] == ""


# ---------------------------------------------------------------------------
# Per-investigation handle construction
# ---------------------------------------------------------------------------

class TestHandleConstruction:
    def test_receipts_constructed_with_case_id(self):
        sup = _FakeSupervisor(fetch_result={"summary": "x"})
        out = FetchPhase(sup).execute(ContextBuilder.for_incident("INC42")).output.result
        assert out["receipts"].case_id == "INC42"

    def test_budget_constructed_with_case_id(self):
        sup = _FakeSupervisor(fetch_result={"summary": "x"})
        out = FetchPhase(sup).execute(ContextBuilder.for_incident("INC42")).output.result
        assert out["budget"].case_id == "INC42"

    def test_circuits_constructed(self):
        from supervisor.guardrails import CircuitBreakerRegistry
        sup = _FakeSupervisor(fetch_result={"summary": "x"})
        out = FetchPhase(sup).execute(ContextBuilder.for_incident("INC1")).output.result
        assert isinstance(out["circuits"], CircuitBreakerRegistry)

    def test_call_graph_constructed_with_investigation_id(self):
        from supervisor.llm_call_graph import CallGraph
        sup = _FakeSupervisor(fetch_result={"summary": "x"})
        out = FetchPhase(sup).execute(ContextBuilder.for_incident("INC99")).output.result
        assert isinstance(out["call_graph"], CallGraph)
        # CallGraph uses the raw incident_id, matching investigate()'s prior behavior
        assert out["call_graph"].investigation_id == "INC99"


# ---------------------------------------------------------------------------
# TLS state reset (context propagation)
# ---------------------------------------------------------------------------

class TestTLSPropagation:
    def test_investigation_deadline_set(self):
        sup = _FakeSupervisor(fetch_result={"summary": "x"})
        FetchPhase(sup).execute(ContextBuilder.for_incident("INC1"))
        assert hasattr(sup._tls, "investigation_deadline")
        assert sup._tls.investigation_deadline > 0

    def test_current_investigation_id_set(self):
        sup = _FakeSupervisor(fetch_result={"summary": "x"})
        FetchPhase(sup).execute(ContextBuilder.for_incident("INC7"))
        assert sup._tls.current_investigation_id == "INC7"

    def test_current_phase_set_to_collect(self):
        sup = _FakeSupervisor(fetch_result={"summary": "x"})
        FetchPhase(sup).execute(ContextBuilder.for_incident("INC1"))
        assert sup._tls.current_phase == "collect"

    def test_itsm_and_devops_evidence_reset(self):
        sup = _FakeSupervisor(fetch_result={"summary": "x"})
        # Plant stale values from a prior investigation
        sup._tls.itsm_evidence = "stale"
        sup._tls.devops_evidence = "stale"
        FetchPhase(sup).execute(ContextBuilder.for_incident("INC1"))
        assert sup._tls.itsm_evidence is None
        assert sup._tls.devops_evidence is None

    def test_current_incident_cached_on_success(self):
        incident = {"summary": "x", "affected_service": "svc"}
        sup = _FakeSupervisor(fetch_result=incident)
        FetchPhase(sup).execute(ContextBuilder.for_incident("INC1"))
        assert sup._tls.current_incident is incident

    def test_current_incident_not_cached_when_empty(self):
        sup = _FakeSupervisor(fetch_result=None)
        FetchPhase(sup).execute(ContextBuilder.for_incident("INC1"))
        # TLS is reset to None at start; empty path doesn't overwrite
        assert sup._tls.current_incident is None


# ---------------------------------------------------------------------------
# Empty-incident path
# ---------------------------------------------------------------------------

class TestEmptyIncident:
    def test_returns_empty_result_when_fetch_returns_none(self):
        sup = _FakeSupervisor(fetch_result=None)
        out = FetchPhase(sup).execute(ContextBuilder.for_incident("INC404")).output.result
        early = out["early_return"]
        assert early is not None
        assert early["incident_id"] == "INC404"
        assert "No incident data available" in early["root_cause"]
        assert early["confidence"] == 10

    def test_empty_result_still_returns_handles(self):
        """Even on empty incident, handles must be exposed so caller can clean up."""
        sup = _FakeSupervisor(fetch_result=None)
        out = FetchPhase(sup).execute(ContextBuilder.for_incident("INC1")).output.result
        assert out["receipts"] is not None
        assert out["budget"] is not None
        assert out["circuits"] is not None

    def test_empty_result_status_still_completed(self):
        """An empty incident is a valid fetch outcome — not a FAILED phase."""
        sup = _FakeSupervisor(fetch_result=None)
        result = FetchPhase(sup).execute(ContextBuilder.for_incident("INC1"))
        assert result.status == PhaseStatus.COMPLETED


# ---------------------------------------------------------------------------
# Meta-query short-circuit
# ---------------------------------------------------------------------------

class TestMetaQueryShortCircuit:
    def test_meta_query_triggers_early_return(self):
        """Summary that looks like a question short-circuits the investigation."""
        # Use a real META_QUERY_PREFIX so is_meta_query() actually matches
        sup = _FakeSupervisor(fetch_result={
            "summary": "what is the runbook for payment failures",
            "affected_service": "svc",
        })
        out = FetchPhase(sup).execute(ContextBuilder.for_incident("INC-Q")).output.result
        early = out["early_return"]
        assert early is not None
        assert early["root_cause"] == "META_QUERY_NOT_INCIDENT"
        assert early["confidence"] == 0

    def test_real_incident_summary_does_not_meta_query(self):
        sup = _FakeSupervisor(fetch_result={
            "summary": "checkout returning 5xx errors in prod",
            "affected_service": "checkout",
        })
        out = FetchPhase(sup).execute(ContextBuilder.for_incident("INC1")).output.result
        assert out["early_return"] is None


# ---------------------------------------------------------------------------
# Worker delegation contract
# ---------------------------------------------------------------------------

class TestWorkerDelegation:
    def test_fetch_incident_called_with_handles(self):
        """FetchPhase must delegate to supervisor._fetch_incident with its own handles."""
        sup = _FakeSupervisor(fetch_result={"summary": "x"})
        FetchPhase(sup).execute(ContextBuilder.for_incident("INC50"))
        assert sup._fetch_calls == ["INC50"]


# ---------------------------------------------------------------------------
# Ledger parameter
# ---------------------------------------------------------------------------

class TestLedgerParameter:
    def test_ledger_argument_accepted(self):
        """FetchPhase signature accepts a ledger for symmetry with the contract."""
        sup = _FakeSupervisor(fetch_result={"summary": "x"})
        ledger = EvidenceLedger()
        result = FetchPhase(sup).execute(ContextBuilder.for_incident("INC1"), ledger=ledger)
        # FETCH does not currently emit ledger items (incident -> TLS, not evidence)
        assert result.ok is True
        assert len(ledger) == 0


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

class TestErrorPaths:
    def test_propagates_unexpected_exception_from_fetch(self):
        class _ExplodingSup(_FakeSupervisor):
            def _fetch_incident(self, *a, **kw):
                raise RuntimeError("upstream failure")

        sup = _ExplodingSup()
        with pytest.raises(RuntimeError, match="upstream failure"):
            FetchPhase(sup).execute(ContextBuilder.for_incident("INC1"))


# ---------------------------------------------------------------------------
# Integration smoke: agent.py wiring uses FetchPhase
# ---------------------------------------------------------------------------

class TestAgentWiring:
    def test_agent_imports_fetch_phase_at_call_site(self):
        import supervisor.agent as agent_mod
        src = open(agent_mod.__file__).read()
        # The supervisor's investigate() now imports FetchPhase lazily.
        assert "from supervisor.phases.fetch import FetchPhase" in src

    def test_old_inline_fetch_block_gone(self):
        """The original 'Step 1: Fetch incident' comment should be gone —
        that logic now lives in FetchPhase."""
        import supervisor.agent as agent_mod
        src = open(agent_mod.__file__).read()
        # Sentinel lines that USED to live in investigate() but are now in FetchPhase
        assert "logger.warning(\"No incident data for %s\", incident_id)" not in src
        assert "Meta-query detected for %s, skipping investigation" not in src

    def test_investigate_still_exists_and_signature_unchanged(self):
        from supervisor.agent import SentinalAISupervisor
        import inspect
        sig = inspect.signature(SentinalAISupervisor.investigate)
        params = list(sig.parameters.keys())
        # self, incident_id, replay — same as before
        assert params == ["self", "incident_id", "replay"]


# ---------------------------------------------------------------------------
# Import safety
# ---------------------------------------------------------------------------

class TestImportSafety:
    def test_fetch_module_does_not_import_agent_at_load(self):
        """supervisor.phases.fetch must not import supervisor.agent at module
        load time — would create a cycle since investigate() now imports it."""
        import supervisor.phases.fetch as fp
        src = open(fp.__file__).read()
        # The supervisor.agent import, if present, must be inside a function
        # body (lazy) — not a top-level `from supervisor.agent import ...`.
        for line in src.splitlines():
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            # Top-level import lines have no indentation
            if line == stripped and stripped.startswith(("from supervisor.agent", "import supervisor.agent")):
                pytest.fail(f"top-level import of supervisor.agent in fetch.py: {line!r}")
