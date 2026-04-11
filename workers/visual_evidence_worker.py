"""Visual Evidence Worker — multi-modal evidence capture for SentinalAI.

Captures metric charts, dashboards, and topology screenshots from
observability platforms (Sysdig, Dynatrace, Grafana) and attaches them
to the investigation as visual evidence.

This closes the evidence gap for anomalies that are obvious visually
(a latency spike, a memory sawtooth, a pod restart storm) but hard
to express in structured JSON fields.

Output added to evidence["visual_evidence"]:
    {
        "charts": [
            {
                "title":     "payment-service latency p99 — last 2h",
                "source":    "sysdig",
                "metric":    "net.http.request.time",
                "url":       "https://app.sysdig.com/...",
                "image_b64": null,          # base64 PNG if fetched
                "time_range": {"from": "...", "to": "..."},
                "annotation": "spike at 14:02 UTC correlates with deploy v2.1.0",
            },
            ...
        ],
        "topology_snapshot": {
            "nodes": [...],
            "edges": [...],
        },
    }

Operations:
    get_metric_chart        — fetch a specific metric chart (URL or b64)
    get_dashboard_snapshot  — fetch full service dashboard
    get_topology_map        — get service dependency topology at incident time
    annotate_anomaly_window — mark the anomaly window on a chart

Phase placement:
    Phase 2 (evidence collection), parallel with other workers.
    Non-blocking — failure returns empty dict, investigation continues.

Configuration:
    VISUAL_EVIDENCE_ENABLED       — on/off (default: true)
    VISUAL_EVIDENCE_FETCH_IMAGES  — actually download b64 images (default: false,
                                    URLs only to avoid large payloads)
"""
from __future__ import annotations

import logging
import os
from typing import Any

from workers.base_worker import BaseWorker
from workers.mcp_client import McpGateway

logger = logging.getLogger("sentinalai.visual_evidence_worker")

VISUAL_ENABLED = os.environ.get("VISUAL_EVIDENCE_ENABLED", "true").lower() in ("1", "true", "yes")
FETCH_IMAGES = os.environ.get("VISUAL_EVIDENCE_FETCH_IMAGES", "false").lower() in ("1", "true", "yes")


class VisualEvidenceWorker(BaseWorker):
    """Worker that captures metric charts and dashboards as visual evidence."""

    worker_name = "visual_evidence_worker"

    def __init__(self, gateway: McpGateway | None = None):
        super().__init__()
        self._gateway = gateway or McpGateway.get_instance()
        self.register("get_metric_chart",        self._get_metric_chart)
        self.register("get_dashboard_snapshot",  self._get_dashboard_snapshot)
        self.register("get_topology_map",        self._get_topology_map)
        self.register("annotate_anomaly_window", self._annotate_anomaly_window)

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #

    def _get_metric_chart(self, params: dict) -> dict:
        """Fetch a URL (and optionally base64 image) for a specific metric chart.

        Params:
            service:      Service name (required)
            metric:       Metric name e.g. "net.http.request.time" (required)
            from_iso:     Start of time range (ISO-8601, required)
            to_iso:       End of time range (ISO-8601, required)
            source:       "sysdig" | "dynatrace" | "grafana" (default: "sysdig")
            fetch_image:  Return base64 PNG (default: VISUAL_EVIDENCE_FETCH_IMAGES env)

        Returns:
            {"chart": {"title", "url", "image_b64", "source", "metric",
                       "time_range", "annotation"}}
        """
        service = params.get("service", "")
        metric = params.get("metric", "")
        if not service or not metric:
            return {"error": "service and metric required"}

        source = params.get("source", "sysdig")
        fetch_image = params.get("fetch_image", FETCH_IMAGES)

        tool_name = f"{source}.get_metric_chart"
        return self._gateway.invoke(
            tool_name,
            "get_metric_chart",
            {
                "service":     service,
                "metric":      metric,
                "from_iso":    params.get("from_iso", ""),
                "to_iso":      params.get("to_iso", ""),
                "fetch_image": fetch_image,
            },
        )

    def _get_dashboard_snapshot(self, params: dict) -> dict:
        """Capture a full service dashboard snapshot at the incident time.

        Params:
            service:    Service name (required)
            at_iso:     Timestamp to anchor the dashboard to (required)
            source:     "sysdig" | "dynatrace" | "grafana" (default: "sysdig")
            width_px:   Dashboard image width (default: 1280)
            height_px:  Dashboard image height (default: 720)

        Returns:
            {"dashboard": {"url", "image_b64", "source", "charts": [...]}}
        """
        service = params.get("service", "")
        at_iso = params.get("at_iso", "")
        if not service:
            return {"error": "service required"}

        source = params.get("source", "sysdig")
        return self._gateway.invoke(
            f"{source}.get_dashboard_snapshot",
            "get_dashboard_snapshot",
            {
                "service":   service,
                "at_iso":    at_iso,
                "width_px":  params.get("width_px", 1280),
                "height_px": params.get("height_px", 720),
            },
        )

    def _get_topology_map(self, params: dict) -> dict:
        """Get service dependency topology at the incident time.

        Shows which services were calling which, with error rates on edges,
        so the agent can see blast radius visually.

        Params:
            service:    Root service to center topology on (required)
            at_iso:     Timestamp (required)
            depth:      Hop depth from root service (default: 2)
            source:     "dynatrace" | "sysdig" (default: "dynatrace")

        Returns:
            {"topology": {"nodes": [...], "edges": [...], "image_url": "..."}}
        """
        service = params.get("service", "")
        if not service:
            return {"error": "service required"}

        source = params.get("source", "dynatrace")
        return self._gateway.invoke(
            f"{source}.get_topology",
            "get_topology_map",
            {
                "service": service,
                "at_iso":  params.get("at_iso", ""),
                "depth":   params.get("depth", 2),
            },
        )

    def _annotate_anomaly_window(self, params: dict) -> dict:
        """Mark the anomaly window on an existing chart URL.

        Useful for highlighting the exact spike on a latency chart to make
        the visual evidence immediately readable.

        Params:
            chart_url:      Existing chart URL (required)
            anomaly_start:  ISO-8601 start of anomaly window (required)
            anomaly_end:    ISO-8601 end of anomaly window (required)
            label:          Annotation label e.g. "Deploy v2.1.0" (optional)
            source:         "sysdig" | "grafana" (default: "sysdig")

        Returns:
            {"annotated_url": "...", "annotation_id": "..."}
        """
        chart_url = params.get("chart_url", "")
        anomaly_start = params.get("anomaly_start", "")
        if not chart_url or not anomaly_start:
            return {"error": "chart_url and anomaly_start required"}

        source = params.get("source", "sysdig")
        return self._gateway.invoke(
            f"{source}.annotate_chart",
            "annotate_anomaly_window",
            {
                "chart_url":     chart_url,
                "anomaly_start": anomaly_start,
                "anomaly_end":   params.get("anomaly_end", ""),
                "label":         params.get("label", "incident window"),
            },
        )


# ---------------------------------------------------------------------------
# Module-level helper for agent integration
# ---------------------------------------------------------------------------

def collect_visual_evidence(
    service: str,
    incident_time: str,
    incident_type: str,
    gateway: McpGateway | None = None,
) -> dict[str, Any]:
    """Collect standard visual evidence bundle for an incident.

    Fetches the most relevant charts based on incident type:
      - latency charts for timeout/saturation
      - memory charts for oom_kill
      - error rate charts for error_spike
      - traffic charts for traffic_anomaly

    Returns a dict ready to be stored as evidence["visual_evidence"].
    Never raises — returns {} on failure.
    """
    if not VISUAL_ENABLED:
        return {}

    worker = VisualEvidenceWorker(gateway=gateway)
    charts: list[dict] = []

    # Map incident type to relevant metrics
    _TYPE_METRICS: dict[str, list[str]] = {
        "timeout":         ["net.http.request.time", "net.http.error.count"],
        "saturation":      ["net.http.request.time", "jvm.threads.count"],
        "oom_kill":        ["container.memory.used.percent", "jvm.memory.heap.used"],
        "error_spike":     ["net.http.error.count", "net.http.request.time"],
        "traffic_anomaly": ["net.http.request.count", "net.http.request.time"],
        "silent_failure":  ["net.http.request.count", "net.http.error.count"],
    }
    metrics = _TYPE_METRICS.get(incident_type, ["net.http.request.time", "net.http.error.count"])

    # Derive a 2-hour window around incident time
    from_iso, to_iso = _incident_window(incident_time, before_minutes=90, after_minutes=30)

    for metric in metrics:
        try:
            result = worker.execute("get_metric_chart", {
                "service":  service,
                "metric":   metric,
                "from_iso": from_iso,
                "to_iso":   to_iso,
                "source":   "sysdig",
            })
            if result and not result.get("error"):
                chart = result.get("chart", result)
                chart["metric"] = metric
                charts.append(chart)
        except Exception as exc:
            logger.debug("Chart fetch failed for %s/%s: %s", service, metric, exc)

    # Topology map
    topology = {}
    try:
        topo_result = worker.execute("get_topology_map", {
            "service": service,
            "at_iso":  incident_time,
            "source":  "dynatrace",
        })
        if topo_result and not topo_result.get("error"):
            topology = topo_result.get("topology", topo_result)
    except Exception as exc:
        logger.debug("Topology fetch failed for %s: %s", service, exc)

    if not charts and not topology:
        return {}

    return {"charts": charts, "topology_snapshot": topology}


def _incident_window(at_iso: str, before_minutes: int = 90, after_minutes: int = 30) -> tuple[str, str]:
    """Return (from_iso, to_iso) strings around an incident timestamp."""
    import datetime
    try:
        ts = datetime.datetime.fromisoformat(at_iso.replace("Z", "+00:00"))
        from_dt = ts - datetime.timedelta(minutes=before_minutes)
        to_dt = ts + datetime.timedelta(minutes=after_minutes)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return from_dt.strftime(fmt), to_dt.strftime(fmt)
    except Exception:
        return "", ""
