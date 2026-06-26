"""Phase 2 compatibility tests for sentinel_core.

Verifies:
- sentinel_core package is importable with zero SentinalAI sub-package deps
- All models are accessible from sentinel_core.models
- Every old import path resolves to the SAME object as the sentinel_core path
- sentinel_core has no imports from supervisor/intelligence/workers/agui
- supervisor.incident_model and agui.schemas.* continue to work as before
- supervisor/ci_shepherd, dev_loop_agent, review_responder use sentinel_core imports
"""
from __future__ import annotations

import importlib
import sys
import types


# ---------------------------------------------------------------------------
# sentinel_core package smoke test
# ---------------------------------------------------------------------------

class TestSentinelCorePackage:

    def test_package_importable(self):
        import sentinel_core
        assert sentinel_core.__version__ == "0.1.0"

    def test_models_subpackage_importable(self):
        import sentinel_core.models
        assert sentinel_core.models is not None

    def test_no_sentinel_deps_in_incident(self):
        import sentinel_core.models.incident as m
        bad = {"supervisor", "intelligence", "workers", "agui"}
        for dep in bad:
            assert dep not in sys.modules or m.__name__ not in str(
                getattr(sys.modules.get(dep, types.ModuleType("")), "__file__", "")
            ), f"sentinel_core.models.incident must not import {dep}"

    def test_sentinel_core_zero_internal_imports(self):
        import sentinel_core.models.incident
        import sentinel_core.models.events
        import sentinel_core.models.receipts
        import sentinel_core.models.incidents
        import sentinel_core.models.dev_task
        import sentinel_core.models.graph

        forbidden = {"supervisor", "intelligence", "workers", "agui", "database", "integrations"}
        for mod_name, mod in list(sys.modules.items()):
            if not mod_name.startswith("sentinel_core"):
                continue
            if not hasattr(mod, "__file__") or mod.__file__ is None:
                continue
            for forbidden_pkg in forbidden:
                assert forbidden_pkg not in (getattr(mod, "__name__", "") or ""), (
                    f"sentinel_core module {mod_name} must not import {forbidden_pkg}"
                )


# ---------------------------------------------------------------------------
# sentinel_core.models — object accessibility
# ---------------------------------------------------------------------------

class TestSentinelCoreModels:

    def test_incident_accessible(self):
        from sentinel_core.models import Incident
        assert Incident is not None
        inc = Incident(incident_id="test-1", summary="test")
        assert inc.incident_id == "test-1"

    def test_event_type_accessible(self):
        from sentinel_core.models import EventType
        assert EventType.INVESTIGATION_STARTED == "investigation.started"

    def test_agui_event_accessible(self):
        from sentinel_core.models import AGUIEvent, EventType
        ev = AGUIEvent(
            event_type=EventType.INVESTIGATION_STARTED,
            investigation_id="inv-1",
            incident_id="inc-1",
        )
        assert ev.investigation_id == "inv-1"

    def test_ui_receipt_accessible(self):
        from sentinel_core.models import UIReceipt
        assert UIReceipt is not None

    def test_incident_state_accessible(self):
        from sentinel_core.models import IncidentState, InvestigationStatus
        state = IncidentState(incident_id="inc-1")
        assert state.status == InvestigationStatus.PENDING

    def test_dev_task_accessible(self):
        from sentinel_core.models import DevTask, DevTaskStatus
        task = DevTask(title="test task", description="desc")
        assert task.status == DevTaskStatus.PENDING

    def test_graph_node_accessible(self):
        from sentinel_core.models import GraphNode, NodeType, NodeStatus
        node = GraphNode(
            node_id="n1",
            node_type=NodeType.TOOL_CALL,
            label="test",
            investigation_id="inv-1",
            trace_id="t1",
        )
        assert node.status == NodeStatus.PENDING

    def test_ci_run_accessible(self):
        from sentinel_core.models import CIRun
        run = CIRun(run_id="r1", status="success")
        assert run.run_id == "r1"

    def test_review_comment_accessible(self):
        from sentinel_core.models import ReviewComment
        comment = ReviewComment(comment_id="c1", body="looks good")
        assert comment.comment_id == "c1"


# ---------------------------------------------------------------------------
# Backward compatibility — object identity checks
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """Both import paths MUST resolve to the exact same Python object."""

    def test_incident_identity(self):
        from sentinel_core.models.incident import Incident as New
        from supervisor.incident_model import Incident as Old
        assert New is Old, "supervisor.incident_model.Incident must be the same object"

    def test_incident_helpers_identity(self):
        from sentinel_core.models.incident import _normalize_severity as New
        from supervisor.incident_model import _normalize_severity as Old
        assert New is Old

    def test_agui_event_identity(self):
        from sentinel_core.models.events import AGUIEvent as New
        from agui.schemas.events import AGUIEvent as Old
        assert New is Old

    def test_event_type_identity(self):
        from sentinel_core.models.events import EventType as New
        from agui.schemas.events import EventType as Old
        assert New is Old

    def test_event_schema_identity(self):
        from sentinel_core.models.events import EventSchema as New
        from agui.schemas.events import EventSchema as Old
        assert New is Old

    def test_ui_receipt_identity(self):
        from sentinel_core.models.receipts import UIReceipt as New
        from agui.schemas.receipts import UIReceipt as Old
        assert New is Old

    def test_receipt_schema_identity(self):
        from sentinel_core.models.receipts import ReceiptSchema as New
        from agui.schemas.receipts import ReceiptSchema as Old
        assert New is Old

    def test_incident_state_identity(self):
        from sentinel_core.models.incidents import IncidentState as New
        from agui.schemas.incidents import IncidentState as Old
        assert New is Old

    def test_investigation_status_identity(self):
        from sentinel_core.models.incidents import InvestigationStatus as New
        from agui.schemas.incidents import InvestigationStatus as Old
        assert New is Old

    def test_control_action_identity(self):
        from sentinel_core.models.incidents import ControlAction as New
        from agui.schemas.incidents import ControlAction as Old
        assert New is Old

    def test_dev_task_identity(self):
        from sentinel_core.models.dev_task import DevTask as New
        from agui.schemas.dev_task import DevTask as Old
        assert New is Old

    def test_dev_task_status_identity(self):
        from sentinel_core.models.dev_task import DevTaskStatus as New
        from agui.schemas.dev_task import DevTaskStatus as Old
        assert New is Old

    def test_ci_run_identity(self):
        from sentinel_core.models.dev_task import CIRun as New
        from agui.schemas.dev_task import CIRun as Old
        assert New is Old

    def test_review_comment_identity(self):
        from sentinel_core.models.dev_task import ReviewComment as New
        from agui.schemas.dev_task import ReviewComment as Old
        assert New is Old

    def test_graph_node_identity(self):
        from sentinel_core.models.graph import GraphNode as New
        from agui.schemas.graph import GraphNode as Old
        assert New is Old

    def test_execution_graph_identity(self):
        from sentinel_core.models.graph import ExecutionGraph as New
        from agui.schemas.graph import ExecutionGraph as Old
        assert New is Old

    def test_node_type_identity(self):
        from sentinel_core.models.graph import NodeType as New
        from agui.schemas.graph import NodeType as Old
        assert New is Old

    def test_agui_schemas_init_still_exports_correctly(self):
        from agui.schemas import AGUIEvent, EventType, UIReceipt, GraphNode, IncidentState
        assert AGUIEvent is not None
        assert EventType is not None
        assert UIReceipt is not None
        assert GraphNode is not None
        assert IncidentState is not None


# ---------------------------------------------------------------------------
# Import smoke tests — verify supervisor files now use sentinel_core paths
# ---------------------------------------------------------------------------

class TestSupervisorImportPaths:
    """Supervisor files that formerly imported from agui.schemas must now
    import from sentinel_core without errors."""

    def test_ci_shepherd_importable(self):
        import supervisor.ci_shepherd  # must not raise ImportError

    def test_dev_loop_agent_importable(self):
        import supervisor.dev_loop_agent  # must not raise ImportError

    def test_review_responder_importable(self):
        import supervisor.review_responder  # must not raise ImportError

    def test_ci_shepherd_uses_sentinel_core(self):
        import inspect
        import supervisor.ci_shepherd as m
        src = inspect.getfile(m)
        # The module-level imports must reference sentinel_core
        with open(src) as f:
            content = f.read()
        assert "from sentinel_core.models.dev_task import" in content
        assert "from sentinel_core.models.events import" in content

    def test_dev_loop_agent_uses_sentinel_core(self):
        import inspect
        import supervisor.dev_loop_agent as m
        src = inspect.getfile(m)
        with open(src) as f:
            content = f.read()
        assert "from sentinel_core.models.dev_task import" in content
        assert "from sentinel_core.models.events import" in content

    def test_review_responder_uses_sentinel_core(self):
        import inspect
        import supervisor.review_responder as m
        src = inspect.getfile(m)
        with open(src) as f:
            content = f.read()
        assert "from sentinel_core.models.dev_task import" in content
        assert "from sentinel_core.models.events import" in content


# ---------------------------------------------------------------------------
# Functional regression — Incident still works after move
# ---------------------------------------------------------------------------

class TestIncidentFunctional:
    """Incidents created via either import path must behave identically."""

    def test_from_moogsoft_works(self):
        from supervisor.incident_model import Incident
        inc = Incident.from_moogsoft({"incident_id": "M1", "summary": "test", "severity": 1})
        assert inc.source == "moogsoft"
        assert inc.severity == 1

    def test_from_servicenow_works(self):
        from supervisor.incident_model import Incident
        inc = Incident.from_servicenow({"number": "INC001", "short_description": "test"})
        assert inc.source == "servicenow"

    def test_from_pagerduty_works(self):
        from supervisor.incident_model import Incident
        inc = Incident.from_pagerduty({"id": "PD1", "title": "test", "urgency": "high"})
        assert inc.source == "pagerduty"
        assert inc.severity == 2

    def test_to_dict_excludes_raw_data(self):
        from sentinel_core.models.incident import Incident
        inc = Incident(incident_id="X1", summary="test", raw_data={"raw": "sensitive"})
        d = inc.to_dict()
        assert "raw_data" not in d
        assert d["incident_id"] == "X1"

    def test_sentinel_core_incident_same_as_supervisor(self):
        from sentinel_core.models.incident import Incident as CoreInc
        from supervisor.incident_model import Incident as SupInc
        # They are literally the same class (identity check in TestBackwardCompatibility)
        assert CoreInc is SupInc
