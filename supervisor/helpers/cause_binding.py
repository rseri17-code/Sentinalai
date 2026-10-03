"""Re-score a hypothesis from this incident's own cited records.

Historical and similar-incident matches may propose a hypothesis. They never
count as support. A clause such as leak, cascade, or after-change is kept
only when a cited raw record inside the alignment window shows that clause.
With no direct aligned ref, the cause is UNKNOWN and its confidence is
below 60.
"""
from __future__ import annotations

import re
from typing import Any

from supervisor.helpers.placeholders import is_placeholder
from supervisor.helpers.timeout_evidence import (
    ALIGNMENT_WINDOW_MINUTES,
    DOWNSTREAM_UNKNOWN,
    _extract_downstream,
    _in_window,
    _incident_bounds,
    _is_derived_record,
    _is_pool,
    _is_slow_query,
    _is_timeout_text,
    _parse_ts,
    _pool_owner_statement,
    _raw_text,
    _record_downstream,
    _ref,
    dedupe_views,
    is_unsaturated_pool,
    iter_pool_readings,
    score_raw_support,
    successful_observation,
    tool_search_error,
    tool_search_errors,
    unsaturated_pool_conflict,
)

# A raw symptom record exists, but it does not support the proposed cause.
SYMPTOM_ONLY = 34
# The cited refs resolve to nothing in this incident.
MISSING_CAUSE = 12

_HISTORICAL_REF = re.compile(
    r"past_experience|suggested_root|kg_similar|historical|knowledge_worker|similar_incident",
    re.I,
)

# Evidence keys that are other incidents, not this one.
# Words that name a causal link. They are kept only by a clause above,
# never just because the proposal contains them.
_TOKEN_SKIP = frozenset({
    "memory", "usage", "latency", "slow", "cpu", "exhaustion", "exhausted",
    "failure", "failures", "resolution", "server", "connection", "pool",
    "change", "data", "pipeline", "drop", "index", "thread", "saturation",
    "config", "errors", "error", "spike", "service", "query", "queries",
    "cache", "stale", "intermittent", "deployment", "observed", "unknown",
    "oomkill", "oomkilled", "increased", "resource", "network",
    "connectivity", "throughput", "degraded", "generic", "recorded",
})

_GLUE = frozenset({
    "after", "causing", "caused", "cause", "introduced", "from", "with",
    "and", "the", "this", "that", "into", "until", "for", "to", "by",
    "affecting", "during", "via", "than", "then", "also", "only",
})

_HISTORICAL_KEYS = frozenset({
    "_past_experiences",
    "_suggested_root_causes",
    "_kg_similar_incidents",
    "historical_context",
    "_tool_recommendations",
})


def bind_hypothesis(
    hypothesis: Any,
    *,
    incident_type: str,
    service: str,
    incident: dict | None,
    evidence: dict | None,
    logs: list | None = None,
    signals: dict | None = None,
    metrics: dict | None = None,
    events: list | None = None,
    changes: list | None = None,
) -> dict[str, Any]:
    """Return an evidence-bound assessment for one hypothesis.

    The shape matches the timeout decision so callers can publish it the
    same way: statement, category, cause_confidence, cause_refs, symptom.
    """
    incident = incident or {}
    evidence = evidence or {}
    proposed = str(getattr(hypothesis, "root_cause", "") or "")
    refs = [str(r) for r in (getattr(hypothesis, "evidence_refs", None) or [])]
    name = str(getattr(hypothesis, "name", "") or "")

    if name == "historical_pattern" or (refs and all(_is_historical_ref(r) for r in refs)):
        cited: list[dict] = []
    else:
        cited = _resolve_refs(
            refs,
            incident=incident,
            evidence=evidence,
            logs=logs or [],
            signals=signals or {},
            metrics=metrics or {},
            events=events or [],
            changes=changes or [],
        )

    # The proposal is only a menu. Windowed records are what a symptom
    # cites, and what fills a cause when the proposal itself has no support.
    windowed = _windowed_views(
        incident=incident,
        evidence=evidence,
        logs=logs or [],
        signals=signals or {},
        metrics=metrics or {},
        events=events or [],
        changes=changes or [],
    )
    symptom = _symptom_for(windowed, incident_type)

    # A derived label may suggest the hypothesis. It does not support it.
    raw_cited = [
        v for v in cited if not _is_derived_record(v.get("record") or {})
    ]
    # A derived label does not support a clause. The raw series it was built
    # from does, when that series was retrieved.
    clause_views = list(raw_cited) + _raw_behind_derived(cited, windowed)
    series = _cycle_series(windowed)
    kept, unknowns, category = _kept_clauses(proposed, clause_views, service)
    kept, unknowns = _apply_pattern(proposed, kept, unknowns, series)
    support = _support_views(windowed, kept, series, service)
    cause_refs = []
    for view in support:
        signal = _signal_for(view, kept) or (view.get("kind") or "record")
        ref = _ref(view, signal, service or "")
        if ref.get("evidence_class") == "derived":
            continue
        cause_refs.append(ref)

    derived_only = bool(cited) and not raw_cited and not cause_refs
    contributions: list[dict] = []
    failed_searches = tool_search_errors(evidence)
    searches_all_failed = bool(failed_searches) and not successful_observation(evidence)
    if kept and cause_refs:
        statement = _render(service, kept)
        if (
            category == "connection_pool_exhaustion"
            and not _views_name_downstream(support)
            and DOWNSTREAM_UNKNOWN not in unknowns
        ):
            unknowns.append(DOWNSTREAM_UNKNOWN)
        confidence, contributions = score_raw_support(cause_refs)
    else:
        if kept and not cause_refs and not searches_all_failed:
            unknowns = list(unknowns) + [f"cited refs do not locate: {', '.join(kept)}"]
        if searches_all_failed:
            for row in failed_searches:
                line = f"search did not happen: {row['tool']}: {row['error']}"
                if line not in unknowns:
                    unknowns.append(line)
        statement = f"{incident_type} observed; cause UNKNOWN"
        category = "unknown"
        if derived_only:
            confidence = 0
        else:
            confidence = SYMPTOM_ONLY if raw_cited else MISSING_CAUSE
        cause_refs = []

    if category == "unknown":
        confidence = min(confidence, 59)

    if not symptom["evidence_refs"] and raw_cited:
        symptom = {
            "statement": f"{incident_type} observed",
            "confidence": 70,
            "evidence_refs": [_ref(raw_cited[0], "symptom_observed", service or "")],
        }
    proposal: dict[str, Any] = {
        "hypothesis_name": name or category,
        "statement": statement,
        "category": category,
        "cause_confidence": confidence,
        "cause_refs": cause_refs,
        "contradictions": [],
        "unknowns": unknowns,
        "symptom": symptom,
        "contributions": contributions,
    }
    proposal = _with_provenance(proposal, name)
    _mention_service(proposal, service)
    # A supported slow query, pool record, or exception is the cause even
    # when the proposal already scored at or above 60 from another clause.
    # A derived label must not be what keeps that proposal in place.
    logs = [v for v in windowed if v.get("kind") == "log"]
    scanned = _decide_from_views(windowed, service=service, incident_type=incident_type)
    if scanned is None:
        return proposal
    scanned_raw = [
        r for r in (scanned.get("cause_refs") or [])
        if r.get("evidence_class") != "derived"
    ]
    prefer = bool(scanned.get("contradictions")) or (
        scanned.get("category") in {
            "exception", "slow_queries", "connection_pool_exhaustion",
        }
        and bool(scanned_raw)
    )
    if not prefer and proposal["cause_confidence"] >= 60:
        return proposal
    if scanned.get("category") == "connection_pool_exhaustion":
        scanned = _merge_pattern(scanned, proposed, windowed)
    if symptom["evidence_refs"]:
        scanned["symptom"] = symptom
    _kept, proposal_unknowns, _category = _kept_clauses(proposed, windowed, service)
    _kept, proposal_unknowns = _apply_pattern(proposed, _kept, proposal_unknowns, series)
    merged = list(scanned.get("unknowns") or [])
    for item in proposal_unknowns:
        if item not in merged:
            merged.append(item)
    scanned["unknowns"] = merged
    scanned = _with_provenance(scanned, name)
    return _mention_service(scanned, service)


def narrow_statement(
    proposed: str,
    records: list[dict],
    *,
    service: str,
    incident_type: str,
) -> str:
    """Statement an analyzer may publish for records it already holds.

    ``proposed`` is the claim menu. Only clauses those records show are kept.
    """
    views = []
    for rec in records or []:
        if isinstance(rec, dict):
            views.append({"record": rec, "timestamp": _record_ts(rec), "sequence_order": None, "tool": "", "locator": None})
    kept, _unknowns, _category = _kept_clauses(proposed, views, service)
    if not kept:
        return f"{incident_type} observed; cause UNKNOWN"
    return _render(service, kept)


def _is_historical_ref(ref: str) -> bool:
    return bool(_HISTORICAL_REF.search(ref or ""))


def _alignment_bounds(incident: dict):
    """Incident clock used for the window.

    A fabricated ``created_at`` (the incident model fills an empty clock with
    wall-clock now) is not an incident time. ``_source_clock`` is set by
    fetch from the raw payload; an empty value means the payload had no clock.
    """
    if "_source_clock" in incident:
        clock = incident.get("_source_clock") or ""
        start = _parse_ts(clock) if clock else None
        end = _parse_ts(incident.get("end_time") or incident.get("resolved_at") or "")
        if start and not end:
            end = start
        return start, end
    return _incident_bounds(incident)


def _resolve_refs(
    refs: list[str],
    *,
    incident: dict,
    evidence: dict,
    logs: list,
    signals: dict,
    metrics: dict,
    events: list,
    changes: list,
) -> list[dict]:
    if not refs or all(_is_historical_ref(r) for r in refs):
        return []
    start, end = _alignment_bounds(incident)
    views = _collect_views(evidence, logs, signals, metrics, events, changes)
    if start is not None:
        views = [v for v in views if _in_window(v["timestamp"], start, end)]
    matched: list[dict] = []
    seen: set[int] = set()
    for ref in refs:
        if _is_historical_ref(ref):
            continue
        kind, _, token = ref.partition(":")
        kind = kind.strip().lower()
        token = token.strip()
        for view in views:
            if id(view) in seen:
                continue
            if _view_matches(view, kind, token):
                seen.add(id(view))
                matched.append(view)
    return matched


def _collect_views(evidence, logs, signals, metrics, events, changes) -> list[dict]:
    views: list[dict] = []
    found_logs = False
    for key, val in (evidence or {}).items():
        if key in _HISTORICAL_KEYS or str(key).startswith("_"):
            continue
        if not isinstance(val, dict):
            continue
        if tool_search_error(val) is not None:
            continue
        seq = val.get("_receipt_sequence_order")
        tool = str(val.get("_receipt_tool") or "")
        locator_key = key
        results = _log_results(val)
        if results is not None:
            found_logs = True
            for i, entry in enumerate(results):
                if isinstance(entry, dict):
                    views.append(_view(entry, "log", seq, tool, {"evidence_key": locator_key, "path": ["logs", "results", i]}))
        for i, entry in enumerate(val.get("events") or []):
            if isinstance(entry, dict):
                views.append(_view(entry, "event", seq, tool, {"evidence_key": locator_key, "path": ["events", i]}))
        for i, entry in enumerate(val.get("changes") or []):
            if isinstance(entry, dict):
                views.append(_view(entry, "change", seq, tool, {"evidence_key": locator_key, "path": ["changes", i]}))
        for i, entry in enumerate(val.get("change_records") or []):
            if isinstance(entry, dict):
                views.append(_view(entry, "change", seq, tool, {"evidence_key": locator_key, "path": ["change_records", i]}))
        metric_blob = val.get("metrics")
        if isinstance(metric_blob, dict):
            for i, entry in enumerate(metric_blob.get("metrics") or []):
                if isinstance(entry, dict):
                    views.append(_view(entry, "metric", seq, tool, {"evidence_key": locator_key, "path": ["metrics", "metrics", i]}))
        elif isinstance(metric_blob, list):
            for i, entry in enumerate(metric_blob):
                if isinstance(entry, dict):
                    views.append(_view(entry, "metric", seq, tool, {"evidence_key": locator_key, "path": ["metrics", i]}))
        sig = val.get("signals")
        if isinstance(sig, dict) and sig.get("golden_signals"):
            ts = str(sig.get("anomaly_start") or sig.get("timestamp") or "")
            views.append({
                "record": sig,
                "kind": "signal",
                "timestamp": ts,
                "sequence_order": seq if isinstance(seq, int) else None,
                "tool": tool,
                "locator": {"evidence_key": locator_key, "path": ["signals"]},
            })
    if not found_logs:
        for i, entry in enumerate(logs or []):
            if isinstance(entry, dict):
                views.append(_view(entry, "log", None, "", {"path": ["logs", i]}))
    if not any(v["kind"] == "event" for v in views):
        for i, entry in enumerate(events or []):
            if isinstance(entry, dict):
                views.append(_view(entry, "event", None, "", {"path": ["events", i]}))
    if not any(v["kind"] == "change" for v in views):
        for i, entry in enumerate(changes or []):
            if isinstance(entry, dict):
                views.append(_view(entry, "change", None, "", {"path": ["changes", i]}))
    if not any(v["kind"] == "metric" for v in views):
        for i, entry in enumerate((metrics or {}).get("metrics") or []):
            if isinstance(entry, dict):
                views.append(_view(entry, "metric", None, "", {"path": ["metrics", i]}))
    if not any(v["kind"] == "signal" for v in views) and (signals or {}).get("golden_signals"):
        views.append({
            "record": signals,
            "kind": "signal",
            "timestamp": str((signals or {}).get("anomaly_start") or ""),
            "sequence_order": None,
            "tool": "",
            "locator": {"path": ["signals"]},
        })
    # Pool gauges from Prometheus series and from signals.db_connection_pool.
    # The same numbers are one reading whichever shape the worker used.
    for reading in iter_pool_readings(evidence):
        views.append(reading)
    return views


def _log_results(val: dict) -> list | None:
    logs_data = val.get("logs", None)
    if isinstance(logs_data, dict) and isinstance(logs_data.get("results"), list):
        return logs_data["results"]
    if isinstance(logs_data, list):
        return logs_data
    if isinstance(val.get("results"), list) and "logs" not in val and "metrics" not in val:
        return None
    return None


def _view(record: dict, kind: str, seq: Any, tool: str, locator: dict) -> dict:
    return {
        "record": record,
        "kind": kind,
        "timestamp": _record_ts(record),
        "sequence_order": seq if isinstance(seq, int) else record.get("_receipt_sequence_order"),
        "tool": tool or str(record.get("_receipt_tool") or ""),
        "locator": locator,
    }


def _record_ts(record: dict) -> str:
    for key in ("_time", "timestamp", "ts", "time", "scheduled_start", "actual_start", "start_time", "start_date"):
        val = record.get(key)
        if isinstance(val, str) and val.strip() and not is_placeholder(val):
            return val
    return ""


def _view_matches(view: dict, kind: str, token: str) -> bool:
    record = view["record"]
    view_kind = view.get("kind") or ""
    token_l = token.lower()
    kind_l = kind.lower()
    if kind_l in {"logs", "log"} and view_kind != "log":
        return False
    if kind_l in {"changes", "change"} and view_kind != "change":
        return False
    if kind_l in {"events", "event"} and view_kind != "event":
        return False
    if kind_l in {"metrics", "metric"} and view_kind != "metric":
        return False
    if kind_l in {"golden_signals", "signals", "signal"} and view_kind != "signal":
        return False
    if token_l in {"pool_exhaustion", "pool"}:
        return _is_connection_pool(record) or (
            "pool" in _blob(record) and "thread pool" not in _blob(record)
        )
    if token_l in {"slow_query", "slow_queries"}:
        return _is_slow_query(record)
    if token_l in {"gradual_increase", "memory"}:
        return view_kind == "metric" or "oom" in _blob(record) or "memory" in _blob(record)
    if token_l in {"oomkill", "oom"}:
        return "oom" in _blob(record)
    if token_l in {"thread_pool", "thread"}:
        return "thread" in _blob(record)
    if token_l in {"cascade_chain", "cascade"}:
        return "cascad" in _blob(record)
    if token_l in {"dns_failure", "dns"}:
        return bool(re.search(r"\bdns\b|resolve hostname|name resolution", _blob(record)))
    if token_l in {"pipeline_failure", "pipeline"}:
        return "pipeline" in _blob(record)
    if token_l in {"stale_cache", "stale"}:
        return "stale" in _blob(record)
    if token_l in {"error_rate", "errors"}:
        return _error_rate(record) is not None
    if token_l in {"latency", "cpu_saturation", "cpu"}:
        return view_kind == "signal" or token_l.split("_")[0] in _blob(record)
    if token_l in {"deployment", "config_change", "database_migration", "maintenance", "config"}:
        return view_kind == "change" and _change_matches(record, token_l)
    if token and token_l in _blob(record):
        return True
    return False


def _change_matches(record: dict, token: str) -> bool:
    blob = _change_blob(record)
    if token in {"deployment", "deploy"}:
        return "deploy" in blob or "release" in blob or str(record.get("change_type", "")).lower() in {"deployment", "deploy", "release"}
    if token in {"config_change", "config"}:
        return "config" in blob or "deploy" in blob or "change" in blob
    if token in {"database_migration", "maintenance"}:
        return token.split("_")[-1] in blob or "migrat" in blob or "index" in blob or "maint" in blob or "change" in blob
    return True


def _is_connection_pool(record: dict) -> bool:
    """Connection-pool exhaustion. A thread pool is a different claim."""
    text = _raw_text(record).lower()
    if "thread pool" in text and "connection pool" not in text and "hikari" not in text:
        return False
    return _is_pool(record)


def _blob(record: dict) -> str:
    if not isinstance(record, dict):
        return ""
    parts = [_raw_text(record)]
    for key in ("name", "type", "event_type", "change_type", "exception", "error_type", "anomaly_type", "service"):
        val = record.get(key)
        if isinstance(val, str) and not is_placeholder(val):
            parts.append(val)
    gs = record.get("golden_signals")
    if isinstance(gs, dict):
        parts.append(str(gs)[:500])
    return "\n".join(parts).lower()


def _change_blob(record: dict) -> str:
    parts = []
    for key in ("change_type", "type", "description", "short_description", "title", "message"):
        val = record.get(key)
        if isinstance(val, str) and not is_placeholder(val):
            parts.append(val)
    return " ".join(parts).lower()


def _error_rate(record: dict) -> float | None:
    gs = record.get("golden_signals") if isinstance(record, dict) else None
    if not isinstance(gs, dict):
        return None
    rate = (gs.get("errors") or {}).get("rate")
    if isinstance(rate, (int, float)) and not isinstance(rate, bool):
        return float(rate)
    return None


def _kept_clauses(proposed: str, views: list[dict], service: str) -> tuple[list[str], list[str], str]:
    """Clauses of ``proposed`` that the cited records actually show."""
    text = proposed or ""
    low = text.lower()
    records = [v["record"] for v in views]
    blobs = [_blob(r) for r in records]
    change_blobs = [_change_blob(r) for r in records if _looks_like_change(r)]
    unknowns: list[str] = []
    kept: list[str] = []
    category = "unknown"

    def any_blob(pattern: str) -> bool:
        return any(re.search(pattern, b, re.I) for b in blobs)

    def add(phrase: str, cat: str) -> None:
        nonlocal category
        if phrase not in kept:
            kept.append(phrase)
        if category == "unknown":
            category = cat

    if re.search(r"pool", low) and not re.search(r"thread pool", low):
        pool_records = [
            r for r in records
            if _is_connection_pool(r) or (
                "pool" in _blob(r) and "thread pool" not in _blob(r)
            )
        ]
        if pool_records:
            if _record_downstream(pool_records[0]):
                phrase, _owner = _pool_owner_statement(pool_records[0])
            else:
                pool_svc = str(pool_records[0].get("service") or "")
                phrase = "connection pool exhausted"
                if pool_svc:
                    phrase = f"connection pool exhausted on {pool_svc}"
            add(phrase, "connection_pool_exhaustion")
        else:
            unknowns.append("whether a connection pool is exhausted")

    if re.search(r"\bleak\b", low):
        if any_blob(r"\bleak\b"):
            add("leak", "leak")
        else:
            unknowns.append("whether this is a leak")

    if re.search(r"slow quer", low):
        if any(_is_slow_query(r) for r in records):
            add("slow queries", "slow_queries")
        else:
            unknowns.append("whether queries are slow")

    if re.search(r"rebalanc", low):
        if any_blob(r"rebalanc"):
            add("rebalancing", "rebalancing")
        else:
            unknowns.append("whether a rebalance occurred")

    if re.search(r"cascad", low):
        if any_blob(r"cascad"):
            add("cascade", "cascade")
        else:
            unknowns.append("whether the failure cascaded")

    if re.search(r"nullpointer", low):
        if any_blob(r"nullpointer"):
            add("NullPointerException", "exception")
        else:
            unknowns.append("whether this incident logged a NullPointerException")

    if re.search(r"\boom\b|oomkill", low):
        if any_blob(r"oom"):
            add("OOMKill", "oomkill")
        else:
            unknowns.append("whether an OOMKill occurred")

    if re.search(r"memory", low) and re.search(r"leak|increas|saturat|oom", low):
        if _memory_increased(records) or any_blob(r"memory"):
            if "leak" not in low or any_blob(r"\bleak\b"):
                add("memory usage increased", "memory_growth")
            elif not any_blob(r"\bleak\b"):
                unknowns.append("whether memory growth is a leak")
                if _memory_increased(records) or any_blob(r"oom"):
                    add("memory usage increased", "memory_growth")

    if re.search(r"\bcpu\b", low):
        if _cpu_high(records) or any_blob(r"cpu"):
            add("cpu exhaustion", "cpu_exhaustion")
        else:
            unknowns.append("whether cpu is exhausted")

    if re.search(r"thread pool", low):
        if any_blob(r"thread pool"):
            add("thread pool saturation", "thread_pool")
        else:
            unknowns.append("whether the thread pool is saturated")

    if re.search(r"\bdns\b", low):
        if any_blob(r"\bdns\b|resolve hostname|name resolution"):
            add("dns resolution failure", "dns")
        else:
            unknowns.append("whether dns resolution failed")

    if re.search(r"index", low):
        if any(re.search(r"index", b, re.I) for b in change_blobs) or any_blob(r"index"):
            add("index change", "change")
        else:
            unknowns.append("whether an index change occurred")

    if re.search(r"deploy|introduced|after config|after .*change|maintenance", low):
        if change_blobs and _change_supports(low, change_blobs, records):
            version = _version(change_blobs)
            joined = " ".join(change_blobs)
            if version or re.search(r"deploy|introduced", low):
                phrase = f"deployment {version}".strip()
            elif "config" in joined:
                phrase = "config change"
            elif "maint" in joined:
                phrase = "maintenance"
            elif "index" in joined:
                phrase = "index change"
            else:
                phrase = "change recorded"
            # "introduced" is a claim about why. It stays only when a record
            # from before the deploy shows the error was absent.
            if re.search(r"\bintroduced\b", low) and _error_absent_before_deploy(views):
                add("introduced", "change")
            elif re.search(r"\bintroduced\b|caused", low):
                unknowns.append("whether the deploy introduced the error")
            add(phrase, "change")
        elif re.search(r"deploy|introduced|after ", low):
            unknowns.append("whether a change in this incident preceded the symptom")

    if re.search(r"connection failure|connection refused", low):
        if any_blob(r"connection (failure|refused|error)|refused"):
            target = _connection_target(records)
            add(f"{target} connection failure".strip() if target else "connection failure", "connection_failure")
        else:
            unknowns.append("whether a connection failed")

    if re.search(r"pipeline", low):
        if any_blob(r"pipeline"):
            add("data pipeline failure", "pipeline")
        else:
            unknowns.append("whether a data pipeline failed")

    if re.search(r"stale cache|stale", low):
        if any_blob(r"\bstale\b") and any_blob(r"\bcache\b"):
            add("stale cache", "stale_cache")
        elif any_blob(r"\bstale\b"):
            add("stale data", "stale_cache")
        else:
            unknowns.append("whether a cache is stale")

    # The pattern word is applied later, from the raw series only.
    # A label such as anomaly_type or a payload pattern field does not keep it.

    # A proposed exception or error token that the logs actually contain.
    # A class that only restates the timeout or error symptom is not a cause.
    if not kept:
        for token in _error_tokens(text):
            if _is_symptom_exception(token):
                continue
            if any(token.lower() in b for b in blobs):
                add(token, "exception")
                break

    # Observed tokens the proposal names and a cited record also shows.
    # Causal glue (causing, after, introduced) is not copied from the proposal
    # unless a clause above already kept it.
    if blobs or change_blobs:
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text):
            low_tok = token.lower()
            if low_tok in _GLUE or low_tok in _TOKEN_SKIP:
                continue
            if _is_symptom_exception(token) or _unlabeled_number(token):
                continue
            if any(low_tok in b for b in blobs) or any(low_tok in b for b in change_blobs):
                add(token, category if category != "unknown" else "observed")

    if service and kept and service.lower() not in " ".join(kept).lower():
        kept.append(service)
    return kept, unknowns, category


def _looks_like_change(record: dict) -> bool:
    return any(k in record for k in ("change_type", "scheduled_start", "short_description"))


def _memory_increased(records: list[dict]) -> bool:
    points = []
    for rec in records:
        val = rec.get("value")
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            name = str(rec.get("name") or "")
            if not name or "mem" in name.lower():
                points.append(((_record_ts(rec)), float(val)))
    if len(points) < 2:
        return False
    points.sort()
    return points[-1][1] > points[0][1]


def _cpu_high(records: list[dict]) -> bool:
    for rec in records:
        gs = rec.get("golden_signals") if isinstance(rec, dict) else None
        if isinstance(gs, dict):
            cpu = (gs.get("saturation") or {}).get("cpu")
            if isinstance(cpu, (int, float)) and not isinstance(cpu, bool) and float(cpu) > 90:
                return True
        if "cpu" in _blob(rec) and re.search(r"\b(9\d|100)\b", _blob(rec)):
            return True
    return False


def _change_supports(proposed_low: str, change_blobs: list[str], records: list[dict]) -> bool:
    if not change_blobs:
        return False
    # "introduced" / "after" need the change to precede some other cited record.
    if re.search(r"introduced|after ", proposed_low):
        change_times = []
        other_times = []
        for rec in records:
            ts = _parse_ts(_record_ts(rec))
            if ts is None:
                continue
            if _looks_like_change(rec):
                change_times.append(ts)
            else:
                other_times.append(ts)
        if change_times and other_times:
            return min(change_times) <= max(other_times)
        # A change record is present but ordering cannot be checked.
        return False
    return True


def _version(change_blobs: list[str]) -> str:
    for blob in change_blobs:
        match = re.search(r"v?\d+\.\d+\.\d+", blob, re.I)
        if match:
            token = match.group(0)
            return token if token.lower().startswith("v") else token
    return ""


def _connection_target(records: list[dict]) -> str:
    for rec in records:
        blob = _raw_text(rec)
        match = re.search(r"\b([a-z0-9_-]+)\b connection", blob, re.I)
        if match:
            return match.group(1)
    return ""


def _windowed_views(
    *,
    incident: dict,
    evidence: dict,
    logs: list,
    signals: dict,
    metrics: dict,
    events: list,
    changes: list,
) -> list[dict]:
    views = _collect_views(evidence, logs, signals, metrics, events, changes)
    start, end = _alignment_bounds(incident)
    if start is not None:
        views = [v for v in views if _in_window(v["timestamp"], start, end)]
    return views


def _symptom_for(views: list[dict], incident_type: str) -> dict:
    """Cite a returned timeout or error record, whichever the logs contain."""
    refs = []
    saw_timeout = False
    saw_error = False
    for view in views:
        if view.get("kind") != "log":
            continue
        text = _raw_text(view["record"])
        if not saw_timeout and _is_timeout_text(text):
            refs.append(_ref(view, "timeout_observed", ""))
            saw_timeout = True
        elif not saw_error and _is_error_record(view):
            refs.append(_ref(view, "error_observed", ""))
            saw_error = True
        if saw_timeout and saw_error:
            break
    if saw_timeout:
        statement = "timeout observed"
    elif saw_error:
        statement = "error observed"
    else:
        statement = f"{incident_type} observed"
    return {
        "statement": statement,
        "confidence": 70 if refs else MISSING_CAUSE,
        "evidence_refs": refs,
    }


def _is_error_record(view: dict) -> bool:
    raw_record = view.get("record")
    record: dict = raw_record if isinstance(raw_record, dict) else {}
    level = str(record.get("level") or record.get("severity") or "")
    if level.upper() in {"ERROR", "FATAL", "CRITICAL"}:
        return True
    text = _raw_text(record)
    if text.upper().startswith("ERROR"):
        return True
    exc = record.get("exception")
    return isinstance(exc, str) and bool(exc.strip()) and not is_placeholder(exc)


def _decide_from_views(
    views: list[dict],
    *,
    service: str,
    incident_type: str,
) -> dict | None:
    """A specific cause the records support, or a conflict. None when they don't.

    Latency elevation by itself is not a cause. 'The deploy introduced it'
    is not a cause unless a pre-deploy record shows the error was absent.
    """
    logs = [v for v in views if v.get("kind") == "log"]
    pool = [v for v in logs if _is_connection_pool(v["record"])]
    slow = [v for v in logs if _is_slow_query(v["record"])]
    if pool and slow:
        return _conflict(pool[0], slow[0], incident_type)
    if pool:
        return _pool_cause(pool[0], views, service, incident_type)
    if slow:
        return _slow_cause(slow[0], service, views)
    exc = _exception_hit(logs)
    if exc is not None:
        return _exception_cause(exc, views, service)
    conn = _first_text(logs, r"connection (failure|refused|error)|connection refused")
    if conn is not None:
        target = _connection_target([conn["record"]])
        phrase = f"{target} connection failure".strip() if target else "connection failure"
        return _direct(conn, phrase, "connection_failure", "connection_failure", service)
    dns = _first_text(logs, r"\bdns\b|resolve hostname|name resolution")
    if dns is not None:
        return _direct(dns, "dns resolution failure", "dns", "dns", service)
    oom = _first_text(logs, r"\boom\b|oomkill")
    if oom is not None:
        return _direct(oom, "OOMKill", "oomkill", "oomkill", service, extra="memory usage increased" if _memory_increased([v["record"] for v in views]) or _blob(oom["record"]).find("memory") >= 0 else "")
    pipe = _first_text(logs, r"pipeline")
    if pipe is not None:
        return _direct(pipe, "data pipeline failure", "pipeline", "pipeline", service)
    stale = _first_text(logs, r"\bstale\b")
    if stale is not None and "cache" in _blob(stale["record"]):
        return _direct(stale, "stale cache", "stale_cache", "stale_cache", service)
    threads = _first_text(logs, r"thread pool")
    if threads is not None:
        record = threads["record"]
        field = _record_downstream(record)
        caller = _citation_service(record)
        if field and caller and caller != field:
            phrase = f"{caller}'s thread pool to {field} saturated"
        elif field:
            phrase = f"thread pool for {field} saturated"
        else:
            phrase = "thread pool saturation"
        return _direct(threads, phrase, "thread_pool", "thread_pool", service)
    return None


def _conflict(pool: dict, slow: dict, incident_type: str) -> dict:
    pool_ref = _ref(pool, "connection_pool_exhausted", "")
    slow_ref = _ref(slow, "slow_query", "")
    return {
        "hypothesis_name": "cause_conflict",
        "statement": f"{incident_type} observed; cause UNKNOWN",
        "category": "unknown",
        "cause_confidence": 24,
        "cause_refs": [],
        "contradictions": [
            {"statement": "connection pool exhaustion", "evidence_refs": [pool_ref]},
            {"statement": "slow queries", "evidence_refs": [slow_ref]},
        ],
        "unknowns": [
            "which mechanism is immediate: connection pool exhaustion or slow queries"
        ],
    }


def _pool_cause(view: dict, views: list[dict], service: str, incident_type: str = "timeout") -> dict:
    unsaturated = [
        item for item in views
        if item.get("pool_reading") and is_unsaturated_pool(item.get("record") or {})
    ]
    if unsaturated:
        return unsaturated_pool_conflict(view, unsaturated, incident_type)
    record = view["record"]
    own = _citation_service(record)
    pool_views = [
        v for v in views
        if v.get("kind") == "log" and _is_connection_pool(v["record"])
        and not _is_derived_record(v["record"])
    ] or [view]
    # A downstream field on a cited pool record is the owner. Naming
    # only the caller is an overclaim. Text from another line is used
    # only when this record has no downstream field.
    field_record = next(
        (v["record"] for v in pool_views if _record_downstream(v["record"])),
        None,
    )
    named_by_text = ""
    if field_record is not None:
        statement, named = _pool_owner_statement(field_record)
    else:
        downstream = ""
        for other in views:
            if other.get("kind") != "log":
                continue
            if _is_timeout_text(_raw_text(other["record"])):
                downstream = _extract_downstream(_raw_text(other["record"]))
                if downstream:
                    break
        if downstream and (not own or downstream == own or downstream in _raw_text(record)):
            statement, named = _pool_owner_statement(record, downstream)
            named_by_text = downstream
        elif own:
            statement, named = f"connection pool exhausted on {own}", own
        else:
            statement, named = "connection pool exhausted", ""
    unknowns = []
    if named:
        unknowns.append(f"why {named} refuses connections")
    if field_record is None and not named_by_text and not _views_name_downstream(pool_views):
        unknowns.append(DOWNSTREAM_UNKNOWN)
    pool_views = dedupe_views(pool_views)
    refs = [_ref(v, "connection_pool_exhausted", "") for v in pool_views]
    for metric in dedupe_views([
        item for item in views
        if item.get("kind") == "metric"
        and not item.get("pool_reading")
        and _pool_metric(item["record"])
    ]):
        refs.append(_ref(metric, "connection_pool_exhausted", ""))
    return _direct(
        view, statement, "connection_pool_exhaustion", "connection_pool_exhausted",
        service, unknowns=unknowns, refs=refs,
    )


def _slow_cause(view: dict, service: str, views: list[dict] | None = None) -> dict:
    record = view["record"]
    # v1.7: the owner is the record's downstream field when that field is set.
    target = _record_downstream(record)
    if not target:
        backend = record.get("backend")
        if isinstance(backend, str) and backend.strip() and not is_placeholder(backend):
            target = backend.strip()
    if not target:
        match = re.search(
            r"slow quer(?:y|ies)(?::| on)\s+([A-Za-z0-9_.-]+)",
            _raw_text(record),
            re.I,
        )
        if match and match.group(1).lower() not in {"response", "took", "the"}:
            target = match.group(1)
        else:
            named = re.search(r"\b([A-Za-z][A-Za-z0-9_.-]*)\s+quer(?:y|ies)\b", _raw_text(record))
            if named and named.group(1).lower() not in {"slow", "the", "a"}:
                target = named.group(1)
    own = _citation_service(record)
    if not target:
        target = own or service
    statement = f"slow queries on {target}" if target else "slow queries"
    if own and own not in statement:
        statement = f"{statement} in {own}"
    slow_views = dedupe_views([
        v for v in (views or [view])
        if _is_slow_query(v["record"]) and not _is_derived_record(v["record"])
    ]) or [view]
    refs = [_ref(v, "slow_query", "") for v in slow_views]
    return _direct(view, statement, "slow_queries", "slow_query", service, refs=refs)


def _is_symptom_exception(name: str) -> bool:
    """True when the class only restates a timeout or error symptom.

    TimeoutException and SocketTimeoutException name the symptom. A bare
    Error or Exception does too. IllegalStateException does not.
    """
    if not name:
        return False
    if re.search(r"timeout", name, re.I):
        return True
    stem = re.sub(r"(?:Exception|Error)$", "", name)
    return stem.lower() in {"", "runtime", "remote", "generic", "unchecked", "wrapped"}


def _unlabeled_number(token: str) -> bool:
    """A numeric fragment that is not a version token such as 4.8.2 or v3.1.0."""
    if not token or not re.search(r"\d", token):
        return False
    return re.fullmatch(r"v?\d+\.\d+(?:\.\d+)?", token, re.I) is None


def _exception_hit(logs: list[dict]) -> dict | None:
    pattern = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Exception|Error))\b")
    for view in logs:
        text = _raw_text(view["record"])
        for match in pattern.finditer(text):
            name = match.group(1)
            if _is_symptom_exception(name):
                continue
            return {"view": view, "name": name, "text": text}
    return None


def _exception_cause(hit: dict, views: list[dict], service: str) -> dict:
    view = hit["view"]
    exc = hit["name"]
    own = _citation_service(view["record"]) or service
    version = _version_in_text(hit["text"])
    deploy = _deploy_view(views)
    if not version and deploy is not None:
        version = _version([_change_blob(deploy["record"]) + " " + _raw_text(deploy["record"])])
    statement = f"{exc} in {own}"
    if version:
        statement = f"{exc} in {own} {version}"
    refs = [_ref(view, exc, "")]
    unknowns: list[str] = []
    deploys = _deploy_views(views)
    deploy = deploys[0] if deploys else None
    if deploy is not None:
        when = _record_ts(deploy["record"])
        if when:
            statement = f"{statement}, deployed at {when}"
        for item in dedupe_views(deploys):
            refs.append(_ref(item, "deployment", ""))
        deploy_ts = _parse_ts(when)
        if not _error_absent_before_deploy(views, deploy_ts):
            unknowns.append("whether the deploy introduced the error")
    return _direct(
        view, statement, "exception", exc, service,
        unknowns=unknowns, refs=refs,
    )


def _is_deploy_view(view: dict) -> bool:
    raw_record = view.get("record")
    record: dict = raw_record if isinstance(raw_record, dict) else {}
    if view.get("kind") not in {"change", "event", "log"} and not _looks_like_change(record):
        return False
    if _is_derived_record(record):
        return False
    blob = (_change_blob(record) + " " + _raw_text(record)).lower()
    change_type = str(record.get("change_type") or record.get("type") or "").lower()
    return change_type == "deployment" or "deploy" in blob


def _deploy_views(views: list[dict]) -> list[dict]:
    return [view for view in views if _is_deploy_view(view)]


def _deploy_view(views: list[dict]) -> dict | None:
    found = _deploy_views(views)
    return found[0] if found else None


def _version_in_text(text: str) -> str:
    match = re.search(r"\bv?\d+\.\d+(?:\.\d+)?\b", text or "", re.I)
    if not match:
        return ""
    token = match.group(0)
    return token if token.lower().startswith("v") else token


def _error_absent_before_deploy(views: list[dict], deploy_ts=None) -> bool:
    """True when a record before the deploy says the error was absent."""
    if deploy_ts is None:
        # Called from the proposal path without a parsed time: look for any
        # pre-change absence line. Without a deploy clock this stays false.
        times = []
        for view in views:
            if _looks_like_change(view.get("record") or {}):
                parsed = _parse_ts(view.get("timestamp") or "")
                if parsed is not None:
                    times.append(parsed)
        deploy_ts = min(times) if times else None
    if deploy_ts is None:
        return False
    for view in views:
        parsed = _parse_ts(view.get("timestamp") or "")
        if parsed is None or parsed >= deploy_ts:
            continue
        text = _raw_text(view.get("record") or {}).lower()
        if re.search(
            r"no errors|errors absent|error rate\s*[:=]?\s*0(?:\.0+)?\b|0 errors",
            text,
        ):
            return True
    return False


def _first_text(logs: list[dict], pattern: str) -> dict | None:
    for view in logs:
        if re.search(pattern, _blob(view["record"]), re.I):
            return view
    return None


def _citation_service(record: dict) -> str:
    raw = record.get("service") if isinstance(record, dict) else ""
    if isinstance(raw, str) and raw.strip() and not is_placeholder(raw):
        return raw.strip()
    return ""


def _direct(
    view: dict,
    statement: str,
    category: str,
    signal: str,
    service: str,
    *,
    extra: str = "",
    unknowns: list[str] | None = None,
    refs: list[dict] | None = None,
) -> dict:
    if extra and extra not in statement:
        statement = f"{statement}; {extra}"
    own = _citation_service(view["record"])
    if own and own not in statement and category not in {"exception", "slow_queries", "connection_pool_exhaustion"}:
        statement = f"{statement} in {own}"
    elif service and service not in statement and not own and category not in {"exception", "slow_queries", "connection_pool_exhaustion"}:
        statement = f"{statement} in {service}"
    used = refs if refs is not None else [_ref(view, signal, "")]
    raw = [ref for ref in used if ref.get("evidence_class") != "derived"]
    if not raw:
        return {
            "hypothesis_name": category,
            "statement": f"{service or 'incident'} observed; cause UNKNOWN",
            "category": "unknown",
            "cause_confidence": 0,
            "cause_refs": [],
            "contradictions": [],
            "unknowns": list(unknowns or []),
            "contributions": [],
        }
    score, contribs = score_raw_support(raw)
    return {
        "hypothesis_name": category,
        "statement": statement,
        "category": category,
        "cause_confidence": score,
        "cause_refs": raw,
        "contradictions": [],
        "unknowns": list(unknowns or []),
        "contributions": contribs,
    }


def _mention_service(decision: dict, service: str) -> dict:
    """The affected service stays in the reasoning when the cause cannot name it."""
    if not service:
        return decision
    reasoning = str(decision.get("reasoning") or "")
    if service.lower() not in reasoning.lower():
        decision["reasoning"] = f"For {service}. {reasoning}".strip()
    return decision


def _with_provenance(decision: dict, hypothesis_name: str) -> dict:
    confidence = int(decision["cause_confidence"])
    symptom = decision.get("symptom") or {
        "statement": "observed",
        "confidence": MISSING_CAUSE,
        "evidence_refs": [],
    }
    refs = decision.get("cause_refs") or []
    if decision.get("contradictions"):
        base = 40
        contributions = []
        for group in decision["contradictions"]:
            for ref in group.get("evidence_refs") or []:
                contributions.append({
                    "kind": "contradiction",
                    "source": f"seq={ref.get('sequence_order')}:{ref.get('signal')}",
                    "delta": -8,
                    "relevance": "direct",
                    "strength": "contradiction",
                    "signal": ref.get("signal") or "",
                    "sequence_order": ref.get("sequence_order"),
                    "evidence_class": ref.get("evidence_class") or "",
                })
        # 40 + (-8) * n, then the published score is already capped.
        raw = base + sum(c["delta"] for c in contributions)
        if confidence != raw:
            contributions.append({
                "kind": "cap",
                "source": "unknown_or_contradiction",
                "delta": confidence - raw,
                "relevance": "cap",
                "strength": "none",
                "signal": "",
            })
    elif decision.get("contributions"):
        base = 0
        contributions = list(decision["contributions"])
    else:
        base = 0
        ref = refs[0] if refs else {}
        contributions = [{
            "kind": "support" if refs else "missing",
            "source": (
                f"seq={ref.get('sequence_order')}:{ref.get('signal')}"
                if refs else "no_direct_ref"
            ),
            "delta": confidence,
            "relevance": "direct" if refs else "none",
            "strength": "direct" if refs else "none",
            "signal": ref.get("signal") if refs else "",
            "sequence_order": ref.get("sequence_order") if refs else None,
            "evidence_class": ref.get("evidence_class") or "",
        }]
    unknowns = list(decision.get("unknowns") or [])
    reasoning = (
        f"Re-scored {hypothesis_name or decision.get('hypothesis_name') or 'hypothesis'} "
        f"from in-window records. The cause statement is: {decision['statement']}."
    )
    if unknowns:
        reasoning += " Not established: " + "; ".join(unknowns) + "."
    decision["reasoning"] = reasoning
    decision["provenance"] = {
        "model": "cited_evidence_v1",
        "alignment_window_minutes": ALIGNMENT_WINDOW_MINUTES,
        "base": float(base),
        "contributions": contributions,
        "final_confidence": confidence,
        "symptom_base": 0.0,
        "symptom_contributions": [],
        "symptom_confidence": int(symptom.get("confidence") or 0),
    }
    decision["symptom"] = symptom
    return decision


def _gateway_truncation(payload: dict) -> dict | None:
    """Dict that carries the gateway's truncation fields, if it sent them.

    The fields sit on the tool result. A logs or metrics object may carry
    them too. A ``limit`` equal to the result count is not a truncation
    report, and this function does not invent one.
    """
    candidates = [payload]
    for key in ("logs", "metrics"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for src in candidates:
        if "truncated" in src:
            return src
    return None


def _gateway_ts(src: dict, key: str) -> str:
    val = src.get(key)
    if isinstance(val, str) and val.strip():
        return val.strip()
    return ""


def _truncation_entry(evidence_key: str, payload: dict) -> dict:
    """One payload's truncation note.

    ``truncated`` true or false is the gateway's report, with ``oldest_ts``
    and ``newest_ts`` when the payload includes them. A missing flag is
    ``truncation_unknown``. The returned rows are not treated as complete.
    """
    src = _gateway_truncation(payload)
    if src is None or not isinstance(src.get("truncated"), bool):
        return {"evidence_key": evidence_key, "truncation_unknown": True}
    return {
        "evidence_key": evidence_key,
        "truncation_unknown": False,
        "truncated": src["truncated"],
        "oldest_ts": _gateway_ts(src, "oldest_ts"),
        "newest_ts": _gateway_ts(src, "newest_ts"),
    }


def unchecked_coverage(incident: dict | None, evidence: dict | None, run_started: str = "") -> dict:
    """What this run did not see.

    Requested window versus the windows the receipts say were searched,
    whether the gateway said the result was truncated, and any requested
    span after the run started. These are notes on the cause. They are
    not replay fields.
    """
    start, end = _alignment_bounds(incident or {})
    requested = None
    if start is not None and end is not None:
        from datetime import timedelta
        width = timedelta(minutes=ALIGNMENT_WINDOW_MINUTES)
        requested = {
            "start": (start - width).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": (end + width).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    searched = []
    truncations = []
    count_gaps = []
    tool_errors = tool_search_errors(evidence)
    failed_keys = {row["evidence_key"] for row in tool_errors}
    for key, val in (evidence or {}).items():
        if not isinstance(val, dict) or str(key).startswith("_"):
            continue
        if str(key) in failed_keys:
            continue
        tws = str(val.get("_receipt_time_window_start") or "")
        twe = str(val.get("_receipt_time_window_end") or "")
        if tws or twe:
            searched.append({"evidence_key": key, "start": tws, "end": twe})
        truncations.append(_truncation_entry(str(key), val))
        results = _log_results(val) or []
        logs_obj = val.get("logs") if isinstance(val.get("logs"), dict) else {}
        reported = logs_obj.get("count") if isinstance(logs_obj, dict) else None
        if isinstance(reported, int) and reported != len(results):
            count_gaps.append({
                "evidence_key": key,
                "reported_count": reported,
                "records_returned": len(results),
            })
    after = None
    if run_started and requested:
        run_dt = _parse_ts(run_started)
        req_end = _parse_ts(requested["end"])
        req_start = _parse_ts(requested["start"])
        if run_dt and req_end and req_start and run_dt < req_end:
            span_start = run_dt if run_dt > req_start else req_start
            after = {
                "run_started": run_started,
                "unchecked_start": span_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "unchecked_end": requested["end"],
            }
    return {
        "requested_window": requested,
        "searched_windows": searched,
        "searched_window_reported": bool(searched),
        "truncations": truncations,
        "truncation_unknown": any(row.get("truncation_unknown") for row in truncations),
        "reported_count_disagrees": count_gaps,
        "after_run_start": after,
        "tool_errors": tool_errors,
    }


def build_evidence_snapshot(evidence: dict | None) -> dict:
    """Bool presence plus a content hash of every record consulted.

    ``present`` stays truthy for callers that only ask whether a key was
    filled. ``records`` is what an outside reviewer hashes.
    """
    import hashlib
    import json

    snap: dict[str, Any] = {}
    for key, val in (evidence or {}).items():
        if str(key).startswith("_"):
            continue
        if isinstance(val, dict) and tool_search_error(val) is not None:
            continue
        records = []
        if isinstance(val, dict):
            results = _log_results(val) or []
            blobs = [("logs", results)]
            for label in ("events", "changes", "change_records"):
                entries = val.get(label)
                if isinstance(entries, list):
                    blobs.append((label, entries))
            metrics = val.get("metrics")
            if isinstance(metrics, dict) and isinstance(metrics.get("metrics"), list):
                blobs.append(("metrics", metrics["metrics"]))
            elif isinstance(metrics, list):
                blobs.append(("metrics", metrics))
            index = 0
            for label, entries in blobs:
                for rec in entries:
                    if not isinstance(rec, dict):
                        continue
                    raw = json.dumps(
                        rec, sort_keys=True, default=str, separators=(",", ":"),
                    ).encode()
                    records.append({
                        "index": index,
                        "path": label,
                        "content_hash": hashlib.sha256(raw).hexdigest(),
                        "service": str(rec.get("service") or ""),
                        "timestamp": _record_ts(rec),
                    })
                    index += 1
        # Absent keys stay false so callers that test truthiness still
        # skip them. A filled key carries the record hashes.
        snap[key] = {"present": True, "records": records} if val else False
    return snap


_PATTERN_WORDS = ("intermittent", "recurring", "sawtooth", "flapping")


def _pattern_words(text: str) -> list[str]:
    found = []
    for word in _PATTERN_WORDS:
        if re.search(rf"\b{word}\b", text or "", re.I) and word not in found:
            found.append(word)
    return found


def _apply_pattern(proposed, kept, unknowns, series):
    """Keep a pattern word only when the raw series shows repeated cycles."""
    words = _pattern_words(proposed)
    if not words:
        return kept, unknowns
    kept = [phrase for phrase in kept if phrase.lower() not in _PATTERN_WORDS]
    unknowns = list(unknowns)
    if series:
        if "intermittent" not in [phrase.lower() for phrase in kept]:
            kept.append("intermittent")
    else:
        for word in words:
            if not any(word in item for item in unknowns):
                unknowns.append(word)
    return kept, unknowns


def _cycle_series(views: list[dict]) -> list[dict]:
    """In-window metric points that change direction at least twice.

    The payload ``pattern`` field is not consulted. Fewer than four points,
    or a series that does not reverse, is not a cycle.
    """
    groups: dict[str, list[dict]] = {}
    for view in dedupe_views(views):
        if view.get("kind") != "metric":
            continue
        record = view.get("record") or {}
        if _is_derived_record(record):
            continue
        value = record.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        name = str(record.get("name") or record.get("metric") or "value")
        groups.setdefault(name, []).append(view)
    for group in groups.values():
        ordered = sorted(group, key=lambda item: item.get("timestamp") or "")
        if len(ordered) < 4:
            continue
        values = [float(item["record"]["value"]) for item in ordered]
        changes = 0
        for index in range(2, len(values)):
            delta = (values[index] - values[index - 1]) * (values[index - 1] - values[index - 2])
            if delta < 0:
                changes += 1
        if changes >= 2:
            return ordered
    return []


def _pool_metric(record: dict) -> bool:
    if not isinstance(record, dict) or _is_derived_record(record):
        return False
    value = record.get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    name = str(record.get("name") or record.get("metric") or "")
    return bool(re.search(r"pool|connection", name, re.I))


def _views_name_downstream(views: list[dict]) -> bool:
    for view in views:
        record = view.get("record") or {}
        if _record_downstream(record):
            return True
        if _extract_downstream(_raw_text(record)):
            return True
    return False


def _raw_behind_derived(cited: list[dict], windowed: list[dict]) -> list[dict]:
    """Raw points retrieved behind a derived label the hypothesis cited."""
    blobs = []
    for view in cited:
        record = view.get("record") or {}
        if _is_derived_record(record):
            blobs.append(_blob(record))
    if not blobs:
        return []
    blob = "\n".join(blobs)
    wanted: list[dict] = []
    for view in windowed:
        if _is_derived_record(view.get("record") or {}):
            continue
        record = view.get("record") or {}
        name = str(record.get("name") or record.get("metric") or "").lower()
        if view.get("kind") != "metric":
            continue
        if re.search(r"cpu|saturation", blob) and "cpu" in name:
            wanted.append(view)
        elif re.search(r"mem|oom", blob) and "mem" in name:
            wanted.append(view)
        elif re.search(r"pool|sawtooth|intermittent", blob) and re.search(r"pool|connection", name):
            wanted.append(view)
    return wanted


def _support_views(windowed, kept, series, service: str = "") -> list[dict]:
    """Raw in-window views that show a kept clause. Derived labels are skipped."""
    chosen: list[dict] = []
    seen: set[int] = set()

    def add(view: dict) -> None:
        if id(view) in seen:
            return
        record = view.get("record") or {}
        if _is_derived_record(record):
            return
        seen.add(id(view))
        chosen.append(view)

    service_name = (service or "").lower()
    for phrase in kept:
        low = phrase.lower().strip()
        if not low or low == service_name:
            continue
        if "connection pool" in low:
            for view in windowed:
                record = view.get("record") or {}
                if view.get("kind") == "log" and _is_connection_pool(record):
                    add(view)
                elif view.get("kind") == "metric" and _pool_metric(record):
                    add(view)
        elif low == "intermittent":
            for view in series:
                add(view)
        elif "slow quer" in low:
            for view in windowed:
                if _is_slow_query((view.get("record") or {})):
                    add(view)
        elif "oom" in low:
            for view in windowed:
                if view.get("kind") in {"log", "event"} and re.search(
                    r"oom", _raw_text(view.get("record") or {}), re.I,
                ):
                    add(view)
        elif "memory" in low:
            for view in windowed:
                record = view.get("record") or {}
                name = str(record.get("name") or "")
                if view.get("kind") == "metric" and "mem" in name.lower() and not _is_derived_record(record):
                    add(view)
        elif "thread pool" in low:
            for view in windowed:
                if re.search(r"thread pool", _raw_text(view.get("record") or {}), re.I):
                    add(view)
        elif "cpu" in low:
            for view in windowed:
                record = view.get("record") or {}
                name = str(record.get("name") or "")
                text = _raw_text(record)
                if view.get("kind") == "metric" and "cpu" in name.lower():
                    add(view)
                elif re.search(r"\bcpu\b", text, re.I):
                    add(view)
        elif "dns" in low:
            for view in windowed:
                if re.search(r"\bdns\b|resolve hostname|name resolution", _raw_text(view.get("record") or {}), re.I):
                    add(view)
        elif "pipeline" in low:
            for view in windowed:
                if re.search(r"pipeline", _raw_text(view.get("record") or {}), re.I):
                    add(view)
        elif "stale" in low:
            for view in windowed:
                if re.search(r"\bstale\b", _raw_text(view.get("record") or {}), re.I):
                    add(view)
        elif "connection" in low:
            for view in windowed:
                text = _raw_text(view.get("record") or {})
                if re.search(r"connection (failure|refused|error)|connection refused", text, re.I):
                    add(view)
                elif re.search(r"\bredis\b|\bpostgres\b|\bdatabase\b|\belasticsearch\b", text, re.I) and re.search(
                    r"refused|unavailable|unreachable|failure", text, re.I,
                ):
                    add(view)
        elif any(token in low for token in ("deploy", "config", "maintenance", "index", "change")):
            for view in windowed:
                record = view.get("record") or {}
                if view.get("kind") == "change" or _looks_like_change(record):
                    add(view)
        elif "rebalanc" in low:
            for view in windowed:
                if re.search(r"rebalanc", _raw_text(view.get("record") or {}), re.I):
                    add(view)
        else:
            token = low.split()[0]
            if len(token) < 4:
                continue
            for view in windowed:
                if token in _raw_text(view.get("record") or {}).lower():
                    add(view)
    return dedupe_views(chosen)


def _merge_pattern(decision: dict, proposed: str, windowed: list[dict]) -> dict:
    """Attach a pattern word only together with the raw series that shows it."""
    series = _cycle_series(windowed)
    words = _pattern_words(proposed)
    if not words:
        return decision
    statement = str(decision.get("statement") or "")
    unknowns = list(decision.get("unknowns") or [])
    if series:
        if "intermittent" not in statement.lower():
            statement = f"{statement}; intermittent"
        refs = list(decision.get("cause_refs") or [])
        existing = [ref.get("locator") for ref in refs]
        for view in series:
            ref = _ref(view, "pool_series", "")
            if ref.get("evidence_class") == "derived":
                continue
            if ref.get("locator") in existing:
                continue
            refs.append(ref)
            existing.append(ref.get("locator"))
        score, contribs = score_raw_support(refs)
        decision["statement"] = statement
        decision["cause_refs"] = refs
        decision["cause_confidence"] = score
        decision["contributions"] = contribs
    else:
        for word in _PATTERN_WORDS:
            statement = re.sub(rf";\s*{word}\b", "", statement, flags=re.I)
            statement = re.sub(rf"\b{word}\b", "", statement, flags=re.I)
        statement = re.sub(r"\s{2,}", " ", statement).strip(" ;")
        decision["statement"] = statement
        for word in words:
            if not any(word in item for item in unknowns):
                unknowns.append(word)
        decision["unknowns"] = unknowns
    return decision


def _error_tokens(proposed: str) -> list[str]:
    return re.findall(r"\b[A-Z][A-Za-z0-9]+(?:Exception|Error)\b", proposed or "")


def _signal_for(view: dict, kept: list[str]) -> str:
    blob = _blob(view["record"]) + " " + _change_blob(view["record"])
    for phrase in kept:
        token = phrase.split()[0].lower()
        if token and token in blob:
            return phrase.split()[0].lower().replace(" ", "_")
        if token == "connection" and "pool" in blob:
            return "connection_pool_exhausted"
        if token == "nullpointerexception" and "nullpointer" in blob:
            return "NullPointerException"
    if view.get("kind") == "signal":
        return "golden_signals"
    return view.get("kind") or "record"


def _render(service: str, kept: list[str]) -> str:
    body = "; ".join(p for p in kept if p and p != service)
    if service and service not in body:
        return f"{body} in {service}" if body else service
    return body or service
