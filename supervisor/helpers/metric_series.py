"""Normalize the two metric shapes the OSS validation gateway emits.

The gateway's Prometheus-to-Sysdig series is a list of points under
``metrics.metrics`` with ``baseline`` and ``pattern``. Its golden-signals
shape is ``signals.golden_signals``. Both become one internal series.
Any other body that reaches the metrics path is unparsed. It is not
empty and it is not zero.
"""

from __future__ import annotations

import json
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


def unwrap_tool_payload(payload: dict) -> dict:
    """Open one MCP tools/call envelope the validation gateway returns.

    The gateway's ``tools/call`` result is ``content[].text`` holding the
    tool JSON. Receipt stamps on the wrapper are kept. A second envelope
    inside that JSON is not opened. Text that is not JSON is unchanged.
    """
    if not isinstance(payload, dict) or not _is_tool_envelope(payload):
        return payload
    text = _envelope_text(payload)
    if not text:
        return payload
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return payload
    if not isinstance(parsed, dict):
        return payload
    merged = dict(parsed)
    for key, value in payload.items():
        if str(key).startswith("_"):
            merged[key] = value
    return merged


def classify_metric_payload(payload: Any) -> str:
    """``series``, ``golden``, ``empty``, ``unparsed``, or ``none``.

    ``none`` means this payload is not on the metrics path. ``empty`` is
    a recognized shape with no points, including the worker stubs
    ``{"signals": {}}`` and a series whose point list is empty.
    A tools/call envelope is classified by the JSON inside it. An
    envelope that is not JSON, a series whose points are not numbers,
    and the gateway's ``unknown_metric`` shell are ``unparsed``.
    """
    if not isinstance(payload, dict):
        return "none"
    if _is_tool_envelope(payload):
        opened = unwrap_tool_payload(payload)
        if opened is payload:
            return "unparsed"
        payload = opened
    if _unknown_metric(payload):
        return "unparsed"
    if _is_series_shape(payload):
        points = _series_points(payload)
        numeric = _numeric_points(points)
        if points and not numeric:
            return "unparsed"
        if not numeric:
            return "empty"
        return "series"
    if _is_golden_shape(payload):
        golden = _golden_dict(payload)
        if not golden:
            return "empty"
        if not _golden_numeric(golden) and _golden_unreadable(golden):
            return "unparsed"
        return "golden"
    if _reaches_metrics_path(payload):
        return "unparsed"
    return "none"


def normalize_metric_payload(payload: Any, *, ref: dict | None = None) -> list[dict]:
    """Normalized series for a recognized payload. Unparsed bodies yield []."""
    if isinstance(payload, dict):
        payload = unwrap_tool_payload(payload)
    kind = classify_metric_payload(payload)
    if kind == "series":
        return _normalize_series(payload, ref or {})
    if kind == "golden":
        return _normalize_golden(payload, ref or {})
    return []


def unparsed_metric_signals(evidence: dict | None) -> list[dict]:
    """Metric bodies that match neither gateway shape.

    Each row names the signal, the query that returned the body, and
    ``unparsed_format``. The body was not read.
    """
    from supervisor.receipt import engine_query_id

    rows = []
    for key, val in (evidence or {}).items():
        if str(key).startswith("_") or not isinstance(val, dict):
            continue
        if classify_metric_payload(val) != "unparsed":
            continue
        rows.append({
            "evidence_key": str(key),
            "signal": _unparsed_signal_name(val, str(key)),
            "query_id": engine_query_id(val),
            "reason": "unparsed_format",
        })
    return rows


def _unparsed_signal_name(payload: dict, evidence_key: str) -> str:
    opened = unwrap_tool_payload(payload)
    if opened is not payload:
        return _unparsed_signal_name(opened, evidence_key)
    if _unknown_metric(payload):
        named = payload.get("metric")
        if isinstance(named, str) and named.strip():
            return named.strip()
    for key in ("signal", "name", "metric"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    metrics = payload.get("metrics")
    if isinstance(metrics, list):
        for point in metrics:
            if not isinstance(point, dict):
                continue
            for key in ("name", "metric", "signal"):
                val = point.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    if isinstance(metrics, dict):
        for key in ("name", "metric", "signal"):
            val = metrics.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    signals = payload.get("signals")
    if isinstance(signals, dict):
        for key in signals:
            if key == "golden_signals":
                continue
            if isinstance(key, str) and key.strip():
                return key.strip()
    filt = payload.get("_filter")
    if isinstance(filt, str) and filt.strip() and " " not in filt.strip():
        return filt.strip()
    return evidence_key


def _golden_or_cpu(metric: str) -> bool:
    """Golden-signal saturation is CPU. It is not pool usage."""
    return bool(re.search(r"cpu|saturation|latency|error_rate|golden", metric or "", re.I))


def series_supports_pool(series: dict, owner: str, start, end, in_window) -> bool:
    """A flat in-window pool series for this owner supports pool exhaustion.

    Golden-signal summaries do not. A latency series does not. A series
    whose points stay at or below half of a limit carried on the series
    does not: that series contradicts exhaustion.
    """
    if series.get("source_format") != SERIES_FORMAT:
        return False
    if not _names_match(series.get("service") or "", owner):
        return False
    metric = str(series.get("metric") or "")
    if _golden_or_cpu(metric):
        return False
    if not _POOL_SUPPORT.search(metric) or not _POOL_MECHANISM.search(metric):
        return False
    points = _aligned(series.get("points") or [], start, end, in_window)
    if not points:
        return False
    values = [value for _ts, value in points]
    if min(values) != max(values) or min(values) < 1:
        return False
    limit = series.get("limit")
    if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0:
        if max(values) <= float(limit) * 0.5:
            return False
    return True


def series_contradicts_pool(series: dict, owner: str, start, end, in_window) -> bool:
    """An in-window pool series whose latest point falls to half its peak.

    Golden-signal saturation is CPU and does not contradict a pool cause.
    """
    if series.get("source_format") != SERIES_FORMAT:
        return False
    service = str(series.get("service") or "")
    if service and owner and not _names_match(service, owner):
        return False
    metric = str(series.get("metric") or "")
    if _golden_or_cpu(metric):
        return False
    if not _POOL_SUPPORT.search(metric):
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


def _is_tool_envelope(payload: dict) -> bool:
    content = payload.get("content")
    if not isinstance(content, list) or not content:
        return False
    return any(isinstance(part, dict) and isinstance(part.get("text"), str) for part in content)


def _envelope_text(payload: dict) -> str:
    parts = []
    for part in payload.get("content") or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "\n".join(parts)


def _unknown_metric(payload: dict) -> bool:
    err = payload.get("error")
    return isinstance(err, str) and "unknown_metric" in err


def _golden_numeric(golden: dict) -> bool:
    for value in golden.values():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if isinstance(value, dict) and _golden_numeric(value):
            return True
    return False


def _golden_unreadable(golden: dict) -> bool:
    """True when the golden object holds a value that is not a number."""
    for value in golden.values():
        if isinstance(value, dict):
            if _golden_unreadable(value):
                return True
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            continue
        if isinstance(value, str):
            continue
        return True
    return False


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
