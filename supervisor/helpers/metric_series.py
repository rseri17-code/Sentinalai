"""Normalize the two metric shapes the OSS validation gateway emits.

The gateway's Prometheus-to-Sysdig series is a list of points under
``metrics.metrics`` with ``baseline`` and ``pattern``. Its golden-signals
shape is ``signals.golden_signals``. Both become one internal series.
Any other body that reaches the metrics path is unparsed. It is not
empty and it is not zero.
"""

from __future__ import annotations

import re
from typing import Any

SERIES_FORMAT = "prometheus_sysdig_series"
GOLDEN_FORMAT = "golden_signals"

# Gateway functions that emit those shapes. The coverage test fails when
# a new emitter appears here without a normalizer.
GATEWAY_EMITTERS = {
    "shape_metrics": SERIES_FORMAT,
    "shape_golden_signals": GOLDEN_FORMAT,
}

_POOL_SUPPORT = re.compile(r"pool", re.I)
_POOL_MECHANISM = re.compile(r"exhaust|active|used|connections", re.I)


def classify_metric_payload(payload: Any) -> str:
    """``series``, ``golden``, ``empty``, ``unparsed``, or ``none``.

    ``none`` means this payload is not on the metrics path. ``empty`` is
    a recognized shape with no points, including the worker stubs
    ``{"signals": {}}`` and a series whose point list is empty.
    """
    if not isinstance(payload, dict):
        return "none"
    if _is_series_shape(payload):
        points = _series_points(payload)
        if not _numeric_points(points):
            return "empty"
        return "series"
    if _is_golden_shape(payload):
        golden = _golden_dict(payload)
        if not golden:
            return "empty"
        return "golden"
    if _reaches_metrics_path(payload):
        return "unparsed"
    return "none"


def normalize_metric_payload(payload: Any, *, ref: dict | None = None) -> list[dict]:
    """Normalized series for a recognized payload. Unparsed bodies yield []."""
    kind = classify_metric_payload(payload)
    if kind == "series":
        return _normalize_series(payload, ref or {})
    if kind == "golden":
        return _normalize_golden(payload, ref or {})
    return []


def unparsed_metric_signals(evidence: dict | None) -> list[dict]:
    rows = []
    for key, val in (evidence or {}).items():
        if str(key).startswith("_") or not isinstance(val, dict):
            continue
        if classify_metric_payload(val) == "unparsed":
            rows.append({"evidence_key": str(key), "reason": "unparsed_format"})
    return rows


def series_supports_pool(series: dict, owner: str, start, end, in_window) -> bool:
    """A flat in-window pool series for this owner supports pool exhaustion.

    Golden-signal summaries do not. A latency series does not.
    """
    if series.get("source_format") != SERIES_FORMAT:
        return False
    if not _names_match(series.get("service") or "", owner):
        return False
    metric = str(series.get("metric") or "")
    if not _POOL_SUPPORT.search(metric) or not _POOL_MECHANISM.search(metric):
        return False
    points = _aligned(series.get("points") or [], start, end, in_window)
    if not points:
        return False
    values = [value for _ts, value in points]
    return min(values) == max(values) and min(values) >= 1


def series_contradicts_pool(series: dict, owner: str, start, end, in_window) -> bool:
    """An in-window pool series whose latest point falls to half its peak."""
    if series.get("source_format") != SERIES_FORMAT:
        return False
    service = str(series.get("service") or "")
    if service and owner and not _names_match(service, owner):
        return False
    if not _POOL_SUPPORT.search(str(series.get("metric") or "")):
        return False
    points = _aligned(series.get("points") or [], start, end, in_window)
    if len(points) < 2:
        return False
    values = [value for _ts, value in points]
    peak = max(values)
    latest = values[-1]
    return peak > 0 and latest < peak and latest <= peak * 0.5


def _names_match(left: str, right: str) -> bool:
    from supervisor.helpers.service_match import service_names_match
    return service_names_match(left, right)


def _is_series_shape(payload: dict) -> bool:
    metrics = payload.get("metrics")
    if isinstance(metrics, dict) and isinstance(metrics.get("metrics"), list) and "baseline" in metrics:
        return True
    return isinstance(metrics, list) and "baseline" in payload


def _series_points(payload: dict) -> list:
    metrics = payload.get("metrics")
    if isinstance(metrics, dict) and isinstance(metrics.get("metrics"), list):
        return metrics["metrics"]
    if isinstance(metrics, list):
        return metrics
    return []


def _numeric_points(points: list) -> list[dict]:
    chosen = []
    for point in points:
        if not isinstance(point, dict):
            continue
        value = point.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            chosen.append(point)
    return chosen


def _is_golden_shape(payload: dict) -> bool:
    signals = payload.get("signals")
    if isinstance(signals, dict) and ("golden_signals" in signals or signals == {}):
        return True
    if payload.get("metrics") == {} and "signals" not in payload:
        return True
    golden = payload.get("golden_signals")
    return isinstance(golden, dict)


def _golden_service(payload: dict, signals: dict) -> str:
    raw_metrics = payload.get("metrics")
    metrics: dict = raw_metrics if isinstance(raw_metrics, dict) else {}
    for candidate in (signals.get("service"), payload.get("service"), metrics.get("service")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _golden_dict(payload: dict) -> dict:
    signals = payload.get("signals")
    if isinstance(signals, dict) and isinstance(signals.get("golden_signals"), dict):
        return signals["golden_signals"]
    golden = payload.get("golden_signals")
    if isinstance(golden, dict):
        return golden
    return {}


def _reaches_metrics_path(payload: dict) -> bool:
    if "metrics" in payload or "signals" in payload:
        return True
    data = payload.get("data")
    if isinstance(data, dict) and data.get("resultType"):
        return True
    return False


def _unit_for_metric(name: str) -> str:
    low = (name or "").lower()
    if "latency" in low or low.endswith("_ms") or low.endswith("ms"):
        return "ms"
    if "pct" in low or "percent" in low or "saturation" in low:
        return "percent"
    if "rps" in low or low.endswith("_rate") or "error_rate" in low:
        return "ratio"
    return "1"


def _normalize_series(payload: dict, ref: dict) -> list[dict]:
    grouped: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for point in _numeric_points(_series_points(payload)):
        service = str(point.get("service") or payload.get("service") or "")
        metric = str(point.get("metric") or point.get("name") or "")
        ts = str(point.get("timestamp") or point.get("_time") or point.get("ts") or "")
        grouped.setdefault((service, metric), []).append((ts, float(point["value"])))
    found = []
    for (service, metric), points in grouped.items():
        found.append({
            "service": service,
            "metric": metric,
            "unit": _unit_for_metric(metric),
            "points": points,
            "source_format": SERIES_FORMAT,
            "ref": dict(ref),
        })
    return found


def _normalize_golden(payload: dict, ref: dict) -> list[dict]:
    golden = _golden_dict(payload)
    raw_signals = payload.get("signals")
    signals: dict = raw_signals if isinstance(raw_signals, dict) else {}
    service = _golden_service(payload, signals)
    ts = str(
        (signals or {}).get("timestamp")
        or (signals or {}).get("anomaly_start")
        or payload.get("timestamp")
        or ""
    )
    specs = (
        (("latency", "p95"), "latency_p95", "ms"),
        (("latency", "p50"), "latency_p50", "ms"),
        (("latency", "p99"), "latency_p99", "ms"),
        (("latency", "baseline_p95"), "latency_baseline_p95", "ms"),
        (("errors", "rate"), "error_rate", "ratio"),
        (("traffic", "rps"), "request_rate", "rps"),
        (("saturation", "pct"), "saturation_pct", "percent"),
    )
    found = []
    for path, metric, unit in specs:
        cursor: Any = golden
        for part in path:
            if not isinstance(cursor, dict) or part not in cursor:
                cursor = None
                break
            cursor = cursor[part]
        if not isinstance(cursor, (int, float)) or isinstance(cursor, bool):
            continue
        point_ts = ts
        points = [(point_ts, float(cursor))] if point_ts else [("", float(cursor))]
        found.append({
            "service": service,
            "metric": metric,
            "unit": unit,
            "points": points,
            "source_format": GOLDEN_FORMAT,
            "ref": dict(ref),
        })
    return found


def _aligned(points, start, end, in_window) -> list[tuple[str, float]]:
    chosen = []
    for ts, value in points:
        if start is not None and end is not None and not in_window(str(ts or ""), start, end):
            continue
        chosen.append((str(ts or ""), float(value)))
    return chosen
