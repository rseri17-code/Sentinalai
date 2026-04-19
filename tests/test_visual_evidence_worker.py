"""Tests for workers.visual_evidence_worker."""
from __future__ import annotations

import os
import pytest
from unittest.mock import MagicMock

os.environ.setdefault("VISUAL_EVIDENCE_ENABLED", "true")
os.environ.setdefault("VISUAL_EVIDENCE_FETCH_IMAGES", "false")

from workers.visual_evidence_worker import (
    VisualEvidenceWorker,
    collect_visual_evidence,
    _incident_window,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CHART_RESPONSE = {
    "chart": {
        "title": "payment-service net.http.request.time — last 2h",
        "source": "sysdig",
        "metric": "net.http.request.time",
        "url": "https://app.sysdig.com/charts/...",
        "image_b64": None,
        "time_range": {"from": "2024-01-15T12:00:00Z", "to": "2024-01-15T14:00:00Z"},
        "annotation": "spike at 14:02 UTC",
    }
}

TOPOLOGY_RESPONSE = {
    "topology": {
        "nodes": [
            {"service": "payment-service", "type": "service"},
            {"service": "auth-service", "type": "service"},
        ],
        "edges": [
            {"from": "client", "to": "payment-service", "calls_per_min": 847},
        ],
        "image_url": "https://dynatrace.example.com/topology/payment-service",
    }
}

DASHBOARD_RESPONSE = {
    "dashboard": {
        "url": "https://app.sysdig.com/dashboards/payment-service",
        "image_b64": None,
        "source": "sysdig",
        "charts": [
            {"metric": "net.http.request.time", "title": "Request Latency"},
        ],
    }
}

ANNOTATED_RESPONSE = {
    "annotated_url": "https://app.sysdig.com/charts/...?annotation=1",
    "annotation_id": "ann-001",
}


@pytest.fixture
def mock_gateway():
    gw = MagicMock()
    gw.invoke.return_value = CHART_RESPONSE
    return gw


@pytest.fixture
def worker(mock_gateway):
    return VisualEvidenceWorker(gateway=mock_gateway)


# ---------------------------------------------------------------------------
# _incident_window helper
# ---------------------------------------------------------------------------

class TestIncidentWindow:

    def test_returns_iso_strings(self):
        from_iso, to_iso = _incident_window("2024-01-15T14:00:00Z")
        assert from_iso.endswith("Z")
        assert to_iso.endswith("Z")

    def test_before_90_after_30(self):
        import datetime
        from_iso, to_iso = _incident_window("2024-01-15T14:00:00Z", before_minutes=90, after_minutes=30)
        # from_iso should be 90 min before
        from_dt = datetime.datetime.fromisoformat(from_iso.replace("Z", "+00:00"))
        to_dt = datetime.datetime.fromisoformat(to_iso.replace("Z", "+00:00"))
        ts = datetime.datetime(2024, 1, 15, 14, 0, 0, tzinfo=datetime.timezone.utc)
        assert from_dt == ts - datetime.timedelta(minutes=90)
        assert to_dt == ts + datetime.timedelta(minutes=30)

    def test_invalid_iso_returns_empty_strings(self):
        from_iso, to_iso = _incident_window("not-a-date")
        assert from_iso == ""
        assert to_iso == ""

    def test_z_suffix_handled(self):
        from_iso, to_iso = _incident_window("2024-06-01T12:00:00Z")
        assert from_iso != ""
        assert to_iso != ""


# ---------------------------------------------------------------------------
# VisualEvidenceWorker.get_metric_chart
# ---------------------------------------------------------------------------

class TestGetMetricChart:

    def test_requires_service(self, worker):
        result = worker.execute("get_metric_chart", {"metric": "net.http.request.time"})
        assert "error" in result

    def test_requires_metric(self, worker):
        result = worker.execute("get_metric_chart", {"service": "payment-service"})
        assert "error" in result

    def test_invokes_gateway(self, worker, mock_gateway):
        mock_gateway.invoke.return_value = CHART_RESPONSE
        result = worker.execute("get_metric_chart", {
            "service": "payment-service",
            "metric": "net.http.request.time",
            "from_iso": "2024-01-15T12:00:00Z",
            "to_iso":   "2024-01-15T14:00:00Z",
        })
        assert mock_gateway.invoke.called
        assert not result.get("error")

    def test_uses_source_param(self, worker, mock_gateway):
        worker.execute("get_metric_chart", {
            "service": "payment-service",
            "metric": "net.http.request.time",
            "source": "grafana",
        })
        call_args = mock_gateway.invoke.call_args
        assert "grafana.get_metric_chart" in call_args[0][0]

    def test_default_source_sysdig(self, worker, mock_gateway):
        worker.execute("get_metric_chart", {
            "service": "payment-service",
            "metric": "net.http.request.time",
        })
        call_args = mock_gateway.invoke.call_args
        assert "sysdig.get_metric_chart" in call_args[0][0]

    def test_passes_fetch_image_flag(self, worker, mock_gateway):
        worker.execute("get_metric_chart", {
            "service": "payment-service",
            "metric": "net.http.request.time",
            "fetch_image": True,
        })
        _, kwargs = mock_gateway.invoke.call_args
        # params are in positional arg index 2
        invoked_params = mock_gateway.invoke.call_args[0][2]
        assert invoked_params["fetch_image"] is True


# ---------------------------------------------------------------------------
# VisualEvidenceWorker.get_dashboard_snapshot
# ---------------------------------------------------------------------------

class TestGetDashboardSnapshot:

    def test_requires_service(self, worker):
        result = worker.execute("get_dashboard_snapshot", {})
        assert "error" in result

    def test_invokes_gateway(self, worker, mock_gateway):
        mock_gateway.invoke.return_value = DASHBOARD_RESPONSE
        worker.execute("get_dashboard_snapshot", {
            "service": "payment-service",
            "at_iso": "2024-01-15T14:02:00Z",
        })
        assert mock_gateway.invoke.called

    def test_default_dimensions(self, worker, mock_gateway):
        mock_gateway.invoke.return_value = DASHBOARD_RESPONSE
        worker.execute("get_dashboard_snapshot", {"service": "payment-service"})
        params = mock_gateway.invoke.call_args[0][2]
        assert params["width_px"] == 1280
        assert params["height_px"] == 720


# ---------------------------------------------------------------------------
# VisualEvidenceWorker.get_topology_map
# ---------------------------------------------------------------------------

class TestGetTopologyMap:

    def test_requires_service(self, worker):
        result = worker.execute("get_topology_map", {})
        assert "error" in result

    def test_invokes_gateway(self, worker, mock_gateway):
        mock_gateway.invoke.return_value = TOPOLOGY_RESPONSE
        worker.execute("get_topology_map", {
            "service": "payment-service",
            "at_iso": "2024-01-15T14:00:00Z",
        })
        assert mock_gateway.invoke.called

    def test_default_source_dynatrace(self, worker, mock_gateway):
        mock_gateway.invoke.return_value = TOPOLOGY_RESPONSE
        worker.execute("get_topology_map", {"service": "payment-service"})
        tool_name = mock_gateway.invoke.call_args[0][0]
        assert "dynatrace" in tool_name

    def test_custom_depth(self, worker, mock_gateway):
        mock_gateway.invoke.return_value = TOPOLOGY_RESPONSE
        worker.execute("get_topology_map", {"service": "payment-service", "depth": 3})
        params = mock_gateway.invoke.call_args[0][2]
        assert params["depth"] == 3


# ---------------------------------------------------------------------------
# VisualEvidenceWorker.annotate_anomaly_window
# ---------------------------------------------------------------------------

class TestAnnotateAnomalyWindow:

    def test_requires_chart_url(self, worker):
        result = worker.execute("annotate_anomaly_window", {
            "anomaly_start": "2024-01-15T14:00:00Z"
        })
        assert "error" in result

    def test_requires_anomaly_start(self, worker):
        result = worker.execute("annotate_anomaly_window", {
            "chart_url": "https://app.sysdig.com/charts/..."
        })
        assert "error" in result

    def test_invokes_gateway(self, worker, mock_gateway):
        mock_gateway.invoke.return_value = ANNOTATED_RESPONSE
        worker.execute("annotate_anomaly_window", {
            "chart_url": "https://app.sysdig.com/charts/...",
            "anomaly_start": "2024-01-15T14:00:00Z",
            "anomaly_end": "2024-01-15T14:30:00Z",
            "label": "Deploy v2.1.0",
        })
        assert mock_gateway.invoke.called

    def test_default_label(self, worker, mock_gateway):
        mock_gateway.invoke.return_value = ANNOTATED_RESPONSE
        worker.execute("annotate_anomaly_window", {
            "chart_url": "https://...",
            "anomaly_start": "2024-01-15T14:00:00Z",
        })
        params = mock_gateway.invoke.call_args[0][2]
        assert params["label"] == "incident window"


# ---------------------------------------------------------------------------
# collect_visual_evidence — module-level helper
# ---------------------------------------------------------------------------

class TestCollectVisualEvidence:

    def _make_gateway(self, chart_resp=None, topo_resp=None):
        gw = MagicMock()
        def _invoke(tool, action, params):
            if "topology" in tool or "topology" in action:
                return topo_resp or TOPOLOGY_RESPONSE
            return chart_resp or CHART_RESPONSE
        gw.invoke.side_effect = _invoke
        return gw

    def test_returns_charts_and_topology(self):
        gw = self._make_gateway()
        result = collect_visual_evidence(
            "payment-service", "2024-01-15T14:00:00Z", "timeout", gateway=gw
        )
        assert "charts" in result
        assert "topology_snapshot" in result

    def test_timeout_type_fetches_latency_and_error_metrics(self):
        fetched_metrics = []
        gw = MagicMock()
        def _invoke(tool, action, params):
            if "get_metric_chart" in tool:
                fetched_metrics.append(params.get("metric", ""))
                return CHART_RESPONSE
            return TOPOLOGY_RESPONSE
        gw.invoke.side_effect = _invoke
        collect_visual_evidence("svc", "2024-01-15T14:00:00Z", "timeout", gateway=gw)
        assert "net.http.request.time" in fetched_metrics
        assert "net.http.error.count" in fetched_metrics

    def test_oom_kill_type_fetches_memory_metrics(self):
        fetched_metrics = []
        gw = MagicMock()
        def _invoke(tool, action, params):
            if "get_metric_chart" in tool:
                fetched_metrics.append(params.get("metric", ""))
                return CHART_RESPONSE
            return TOPOLOGY_RESPONSE
        gw.invoke.side_effect = _invoke
        collect_visual_evidence("svc", "2024-01-15T14:00:00Z", "oom_kill", gateway=gw)
        assert "container.memory.used.percent" in fetched_metrics

    def test_unknown_incident_type_uses_defaults(self):
        fetched_metrics = []
        gw = MagicMock()
        def _invoke(tool, action, params):
            if "get_metric_chart" in tool:
                fetched_metrics.append(params.get("metric", ""))
                return CHART_RESPONSE
            return TOPOLOGY_RESPONSE
        gw.invoke.side_effect = _invoke
        collect_visual_evidence("svc", "2024-01-15T14:00:00Z", "unknown_type", gateway=gw)
        assert "net.http.request.time" in fetched_metrics

    def test_returns_empty_on_total_failure(self):
        gw = MagicMock()
        gw.invoke.side_effect = RuntimeError("gateway down")
        result = collect_visual_evidence("svc", "2024-01-15T14:00:00Z", "timeout", gateway=gw)
        assert result == {}

    def test_returns_empty_when_disabled(self, monkeypatch):
        import workers.visual_evidence_worker as mod
        monkeypatch.setattr(mod, "VISUAL_ENABLED", False)
        gw = MagicMock()
        result = collect_visual_evidence("svc", "2024-01-15T14:00:00Z", "timeout", gateway=gw)
        assert result == {}
        gw.invoke.assert_not_called()

    def test_chart_error_response_excluded(self):
        gw = MagicMock()
        def _invoke(tool, action, params):
            if "get_metric_chart" in tool:
                return {"error": "metric not found"}
            return TOPOLOGY_RESPONSE
        gw.invoke.side_effect = _invoke
        result = collect_visual_evidence("svc", "2024-01-15T14:00:00Z", "timeout", gateway=gw)
        # topology is present but no charts
        assert result.get("charts", []) == []
        assert result.get("topology_snapshot")

    def test_partial_failure_still_returns_available(self):
        """If chart succeeds but topology fails, we still get charts."""
        call_count = [0]
        gw = MagicMock()
        def _invoke(tool, action, params):
            call_count[0] += 1
            if "topology" in tool:
                raise RuntimeError("topology down")
            return CHART_RESPONSE
        gw.invoke.side_effect = _invoke
        result = collect_visual_evidence("svc", "2024-01-15T14:00:00Z", "error_spike", gateway=gw)
        assert "charts" in result
        assert len(result["charts"]) > 0

    def test_all_known_incident_types(self):
        types = ["timeout", "saturation", "oom_kill", "error_spike", "traffic_anomaly", "silent_failure"]
        for itype in types:
            gw = MagicMock()
            gw.invoke.return_value = CHART_RESPONSE
            collect_visual_evidence("svc", "2024-01-15T14:00:00Z", itype, gateway=gw)
            # Should not raise, and should attempt chart fetches
            assert gw.invoke.call_count >= 1
            gw.invoke.reset_mock()


# ---------------------------------------------------------------------------
# Coverage gap-fill tests
# ---------------------------------------------------------------------------

class TestCollectVisualEvidenceExceptionPaths:
    """Cover lines 262-263 and 275-276: exception handlers in collect_visual_evidence.

    BaseWorker.execute() catches Exception and returns an error dict, so the only way
    to trigger the outer exception handlers is to patch VisualEvidenceWorker.execute
    to raise directly (bypassing the base class catch).
    """

    def test_chart_execute_raises_exception_caught(self, monkeypatch):
        """Cover lines 262-263: chart fetch raises → caught, charts stay empty."""
        from unittest.mock import patch, MagicMock
        gw = MagicMock()

        call_count = [0]

        def mock_execute(action, params=None):
            call_count[0] += 1
            if action == "get_metric_chart":
                raise RuntimeError("chart gateway exploded")
            # topology returns empty (no topology either)
            return {}

        with patch(
            "workers.visual_evidence_worker.VisualEvidenceWorker.execute",
            side_effect=mock_execute,
        ):
            result = collect_visual_evidence("svc", "2024-01-15T14:00:00Z", "timeout", gateway=gw)

        # No charts, no topology → empty result
        assert result == {}

    def test_topology_execute_raises_exception_caught(self, monkeypatch):
        """Cover lines 275-276: topology fetch raises → caught, topology stays empty."""
        from unittest.mock import patch, MagicMock
        gw = MagicMock()

        def mock_execute(action, params=None):
            if action == "get_topology_map":
                raise RuntimeError("topology gateway exploded")
            # charts succeed but return error dict (so charts list stays empty too)
            return {"error": "no chart"}

        with patch(
            "workers.visual_evidence_worker.VisualEvidenceWorker.execute",
            side_effect=mock_execute,
        ):
            result = collect_visual_evidence("svc", "2024-01-15T14:00:00Z", "timeout", gateway=gw)

        assert result == {}

    def test_both_execute_raise_returns_empty(self):
        """Cover both 262-263 and 275-276 in a single call."""
        from unittest.mock import patch, MagicMock
        gw = MagicMock()

        def mock_execute(action, params=None):
            raise RuntimeError("everything is broken")

        with patch(
            "workers.visual_evidence_worker.VisualEvidenceWorker.execute",
            side_effect=mock_execute,
        ):
            result = collect_visual_evidence("svc", "2024-01-15T14:00:00Z", "timeout", gateway=gw)

        assert result == {}
