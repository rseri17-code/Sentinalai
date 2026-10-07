"""Evidence-bound timeout cause selection.

A timeout cause is chosen only from raw records that name the alerted
downstream and sit inside the alignment window. Source summary, note, and
annotation fields are never evidence and are never cited.

Cause statements (AC2):
  * ``<caller>'s connection pool to <downstream> exhausted`` when the
    cited pool record has a ``downstream`` field
  * ``connection pool for <ds> exhausted`` when a cited record names one
  * ``connection pool exhausted on <service>`` when none does
  * ``slow queries on <ds>`` when a raw query-level record says so
  * ``<ds> latency elevated; cause UNKNOWN`` from a raw metric point
  * ``timeout observed; cause UNKNOWN`` when evidence is missing or conflicts

A derived record (anomaly flag, pattern, summary, note, verdict) adds 0
to cause confidence. Only raw metric points, log lines, and events score.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from supervisor.helpers.placeholders import is_placeholder
from supervisor.receipt import engine_query_id

# Records must fall in [incident_start - W, incident_end + W].
ALIGNMENT_WINDOW_MINUTES = 15

# Service latency at or above this multiple of baseline is elevated.
# It never proves slow queries.
LATENCY_ELEVATION_FACTOR = 10

# Cause-confidence weights. UNKNOWN and contradicted causes never reach 60.
# One direct in-window raw record is three parts (20 + 22 + 20 = 62).
# Each further agreeing raw record adds EXTRA_RAW. A derived record and
# a record outside the alignment window add 0 and are not scored.
DIRECT_SUPPORT_DELTA = 62
RAW_IN_WINDOW = 20
RAW_OBSERVATION = 22
RAW_MECHANISM = 20
EXTRA_RAW = 8
RAW_CAP = 90

DOWNSTREAM_UNKNOWN = "which downstream this resource connects to"
LATENCY_UNKNOWN_DELTA = 34
CONFLICT_BASE = 40
CONFLICT_EACH = -8
MISSING_CAUSE = 12

SYMPTOM_TIMEOUT_DELTA = 70
SYMPTOM_LATENCY_DELTA = 15

_NON_EVIDENCE_FIELDS = frozenset({
    "summary", "note", "notes", "annotation", "annotations",
    "description", "root_cause_hint", "hint", "comment", "comments",
})

# Allow list. "pool" counts when nothing is in front of it, when the
# word in front is connection, db, database, jdbc, hikari, pgx, or
# r2dbc, or when the token is a DB pool class: HikariPool, JdbcPool,
# QueuePool, AsyncAdaptedQueuePool. max/maximum in front counts only
# when the word before that is empty, punctuation with a space after
# it, a log level, an allowed pool word, or a function word. A hyphen,
# underscore, or period glued to max joins the previous word, so that
# word is the slot. Any other word does not.
_ALLOWED_POOL_PREFIX = frozenset({
    "connection", "db", "database", "jdbc", "hikari", "pgx", "r2dbc",
})
# Closed list. A noun in this slot blocks "max pool" / "maximum pool".
_MAX_POOL_FUNCTION_WORDS = frozenset({
    "and", "or", "the", "a", "an", "was", "is", "of", "when",
    "because", "that", "but", "so", "then", "its", "has", "had", "been",
})
# A severity token is not a pool-kind word. "ERROR pool.exhausted"
# is still a bare pool.
_LOG_LEVEL = frozenset({
    "error", "err", "warn", "warning", "info", "debug",
    "fatal", "critical", "trace",
})
_POOL_CLASS = frozenset({
    "hikaripool", "jdbcpool", "queuepool", "asyncadaptedqueuepool",
})
_POOL_MENTION = re.compile(
    r"[A-Za-z0-9]+[\s._-]*pool\b|\bpool\b",
    re.I,
)
_POOL_NAME = (
    r"(?:"
    r"\b(?:connection|database|r2dbc|hikari|jdbc|pgx|db)[\s._-]*pool\b"
    r"|\basyncadaptedqueuepool\b"
    r"|\bqueuepool\b"
    r"|\bpool\b"
    r")"
)

_POOL_PATTERNS = (
    re.compile(rf"{_POOL_NAME}[\s._-]*exhaust", re.I),
    re.compile(
        r"connection\s+pool.{0,80}(exhaust|not available|unavailable|timed?\s*out|timeout|full|overflow|at capacity|waiting|limit)",
        re.I,
    ),
    # Pool overflow, named on either side of the word.
    re.compile(rf"{_POOL_NAME}.{{0,60}}\boverflow\b", re.I),
    re.compile(rf"\boverflow\b.{{0,60}}{_POOL_NAME}", re.I),
    # "limit … reached" only when a pool is named. Other words (overflow,
    # a size) may sit between limit and reached.
    re.compile(
        rf"{_POOL_NAME}.{{0,80}}\blimit\b.{{0,80}}\breached\b",
        re.I,
    ),
    re.compile(
        rf"\blimit\b.{{0,80}}\breached\b.{{0,80}}{_POOL_NAME}",
        re.I,
    ),
    re.compile(r"unable to acquire connection", re.I),
    re.compile(r"connection is not available", re.I),
    re.compile(r"hikari\s*pool.{0,60}(not available|exhaust|timed?\s*out|timeout)", re.I),
    re.compile(r"(pool|hikari).{0,40}wait queue", re.I),
    re.compile(r"wait queue.{0,40}(pool|connection)", re.I),
    re.compile(r"remaining connection slots", re.I),
    re.compile(r"too many clients already", re.I),
    re.compile(r"cannot get (a )?connection", re.I),
    # At most two words may sit between size and reached/exceeded, and
    # each must be was, is, has, have, had, or been. "was reached" and
    # "has been exceeded" still match. A negation does not.
    re.compile(
        r"max(?:imum)? pool size(?:\s+(?:was|is|has|have|had|been)){0,2}\s+(?:reached|exceeded)",
        re.I,
    ),
    re.compile(r"connection pool.{0,40}(held|waiting)", re.I),
    # Whole word only. "saturation 35%" and "unsaturated" are readings.
    # A negation anywhere between pool and saturated rejects the line,
    # including cannot, can not, and a curly apostrophe. A false value
    # immediately after the word rejects it too.
    re.compile(
        "connection\\s+pool"
        "(?:(?!\\b(?:not|never|without|cannot)\\b|can\\s+not|no\\s+longer|n['\u2019]t).){0,80}?"
        "\\bsaturated\\b"
        "(?!\\s*[=:]\\s*(?:no|false|0)\\b)",
        re.I,
    ),
)

_SLOW_QUERY_PATTERNS = (
    re.compile(r"slow[\s_-]*quer", re.I),
    re.compile(r"quer(?:y|ies)\s+slow", re.I),
    re.compile(r"long[\s-]*running[\s-]*quer", re.I),
    re.compile(
        r"quer(?:y|ies).{0,40}(?:took|duration|lasted|exceeded)\s*[:=]?\s*\d+",
        re.I,
    ),
    re.compile(r"\b(?:select|insert|update|delete)\b.{0,120}\btook\s+\d+", re.I),
    re.compile(r"query[_\s-]*duration", re.I),
)

# PostgreSQL logs a slow statement as milliseconds, then the SQL.
# log_min_duration_statement: "duration: <ms> ms  statement: <sql>"
# extended-query execute: "duration: <ms> ms  execute <name>: <sql>"
# The same millisecond-then-statement shape is what that server emits.
# A duration under the raw query_duration_ms bar is not a slow statement.
_DB_DURATION_STATEMENT = re.compile(
    r"\bduration:\s*(\d+(?:\.\d+)?)\s*ms\b.{0,80}?\b(?:statement|execute)\b",
    re.I,
)

_DOWNSTREAM_PATTERNS = (
    re.compile(r"timeout.*?:\s*(\S+?)(?::\d+)?(?:\s|$)", re.I),
    re.compile(r"waiting for connection:\s*(\S+)", re.I),
    re.compile(r"upstream\s+(\S+?)\s+not responding", re.I),
)

_QUERY_DURATION_FIELDS = ("query_duration_ms", "query_time_ms", "query_duration")
_QUERY_COUNT_FIELDS = ("slow_query_count", "slow_queries")


def _dependency_field(record: dict) -> str:
    for key in ("target", "downstream", "downstream_service"):
        val = _record_field(record, key)
        if val:
            return val
    return ""


def _view_in_bounds(view: dict, start, end) -> bool:
    if start is None or end is None:
        return True
    return _in_window(view.get("timestamp") or "", start, end)


def _is_separate_timeout(view: dict) -> bool:
    """A timeout line that is not itself the pool record."""
    record = view.get("record") or {}
    if not isinstance(record, dict) or view.get("pool_reading"):
        return False
    if _is_derived_record(record) or _is_pool(record):
        return False
    kind = view.get("kind")
    if kind not in (None, "", "log"):
        return False
    return _is_timeout_text(_raw_text(record))


def _cited_structured_dependency(view: dict) -> str:
    """Target or downstream on a timeout or error record from this incident."""
    record = view.get("record") or {}
    if not isinstance(record, dict) or view.get("pool_reading"):
        return ""
    if _is_derived_record(record) or _is_pool(record):
        return ""
    kind = view.get("kind")
    if kind not in (None, "", "log"):
        return ""
    level = str(record.get("level") or record.get("severity") or "")
    text = _raw_text(record)
    is_error = level.upper() in {"ERROR", "FATAL", "CRITICAL"} or text.upper().startswith("ERROR")
    if not _is_timeout_text(text) and not is_error:
        return ""
    return _dependency_field(record)


def _span_dependency(views: list[dict], alerted: str) -> str:
    from supervisor.helpers.service_match import service_names_match

    for view in views:
        record = view.get("record") or {}
        if not isinstance(record, dict) or _is_derived_record(record):
            continue
        kind = str(view.get("kind") or record.get("span_kind") or record.get("kind") or "")
        if kind.lower() not in {"span", "trace"} and not record.get("span_id"):
            continue
        dest = (
            _record_field(record, "to")
            or _record_field(record, "target")
            or _record_field(record, "downstream")
        )
        if not dest:
            continue
        parent = _record_field(record, "from") or _record_field(record, "service")
        if alerted and parent and not service_names_match(parent, alerted):
            continue
        if alerted and service_names_match(dest, alerted):
            continue
        return dest
    return ""


def _incident_structured_dependency(incident: dict | None) -> str:
    if not isinstance(incident, dict):
        return ""
    for key in ("downstream", "downstream_service"):
        val = incident.get(key)
        if isinstance(val, str) and val.strip() and not is_placeholder(val):
            return val.strip()
    return ""


def establish_failing_dependency(
    views: list[dict],
    incident: dict | None,
    alerted: str,
    start=None,
    end=None,
) -> tuple[str, str]:
    """D_fail and how it was established.

    ``structured`` is a cited record's target or downstream field.
    ``span`` is a trace field. ``incident`` is the alert's structured
    downstream field. ``text`` is a name pulled from a timeout line's
    message and does not establish a pool dependency. ``missing`` means
    a separate timeout exists and none of the fields named a dependency.
    ``""`` means there is no separate timeout. Alert title, description,
    and summary are not read.
    """
    considered = [view for view in views if _view_in_bounds(view, start, end)]
    for view in considered:
        found = _cited_structured_dependency(view)
        if found:
            return found, "structured"
    span = _span_dependency(considered, alerted)
    if span:
        return span, "span"
    named = _incident_structured_dependency(incident)
    if named:
        return named, "incident"
    for view in considered:
        if not _is_separate_timeout(view):
            continue
        extracted = _extract_downstream(_raw_text(view.get("record") or {}))
        if extracted:
            return extracted, "text"
    if any(_is_separate_timeout(view) for view in considered):
        return "", "missing"
    return "", ""


def split_pools_for_dependency(
    pool_views: list[dict],
    d_fail: str,
    alerted: str,
) -> tuple[list[dict], list[dict]]:
    """Pools whose own downstream matches D_fail, and the others.

    A pool with no downstream does not match. A downstream that names a
    different service is an observation, not a cause ref.
    """
    from supervisor.helpers.service_match import service_names_match

    matched: list[dict] = []
    observations: list[dict] = []
    for view in pool_views:
        record = view.get("record") or {}
        d_pool = _dependency_field(record)
        if not d_pool:
            continue
        if not service_names_match(d_pool, d_fail):
            observations.append({
                "statement": f"pool to {d_pool} exhausted",
                "evidence_refs": [_ref(view, "pool_observation", "")],
            })
            continue
        own = ""
        raw = record.get("service")
        if isinstance(raw, str) and raw.strip() and not is_placeholder(raw):
            own = raw.strip()
        if own and alerted and not service_names_match(own, alerted) and not service_names_match(own, d_fail):
            continue
        matched.append(view)
    return matched, observations


def _normalized_series(evidence: dict | None) -> list[dict]:
    from supervisor.helpers.metric_series import normalize_metric_payload

    found = []
    for key, val in (evidence or {}).items():
        if str(key).startswith("_") or not isinstance(val, dict):
            continue
        ref = {
            "sequence_order": val.get("_receipt_sequence_order") if isinstance(val.get("_receipt_sequence_order"), int) else None,
            "tool": str(val.get("_receipt_tool") or ""),
            "locator": {"evidence_key": str(key), "path": ["metrics"]},
            "service": "",
            "timestamp": "",
            "signal": "metric_series",
            "evidence_class": "raw",
        }
        qid = engine_query_id(val)
        if qid:
            ref["query_id"] = qid
        for series in normalize_metric_payload(val, ref=ref):
            series_ref = dict(series.get("ref") or {})
            series_ref["service"] = series.get("service") or ""
            points = series.get("points") or []
            series_ref["timestamp"] = points[0][0] if points else ""
            series["ref"] = series_ref
            found.append(series)
    return found


def _contradicting_pool_series(evidence, owner: str, start, end) -> dict | None:
    from supervisor.helpers.metric_series import series_contradicts_pool

    for series in _normalized_series(evidence):
        if series_contradicts_pool(series, owner, start, end, _in_window):
            return series["ref"]
    return None


def _supporting_pool_series(evidence, owner: str, start, end) -> dict | None:
    from supervisor.helpers.metric_series import series_supports_pool

    for series in _normalized_series(evidence):
        if series_supports_pool(series, owner, start, end, _in_window):
            return series
    return None


def decide_timeout(
    *,
    service: str,
    logs: list[dict],
    signals: dict,
    metrics: dict,
    incident: dict | None,
    evidence: dict | None,
) -> dict[str, Any]:
    """Return the single evidence-bound timeout decision.

    ``cause_confidence`` is an int in 0..100. UNKNOWN causes are below 60.
    A bound cause cites at least one in-window raw record. Each further
    agreeing raw record adds to that score. A derived record adds 0.
    """
    incident = incident or {}
    evidence = evidence or {}
    start, end = _incident_bounds(incident)
    alerted = service or str(incident.get("affected_service") or "")

    log_views = list(_iter_logs(evidence, logs))
    latency_views = list(_iter_latency(evidence, signals, metrics))

    ds, timeout_views = _downstream(log_views, start, end)
    named_downstream = bool(ds)
    d_fail, d_source = establish_failing_dependency(
        log_views, incident, alerted, start, end,
    )
    pool_observations: list[dict] = []
    # A separate timeout that names nothing does not fall back to the
    # alerted service. A pool line never supplies that name itself.
    if d_source == "missing":
        ds = ""
        named_downstream = False
    elif d_source in {"structured", "span", "incident", "text"}:
        ds = d_fail
        named_downstream = True
    elif not ds:
        ds = alerted
    in_window_pools = [
        v for v in log_views
        if _in_window(v["timestamp"], start, end) and _is_pool(v["record"])
    ]
    # A pool line does not establish the failing dependency, and neither
    # does message text. A cited record's target or downstream field does,
    # or the alert's structured downstream field. The pool's own dependency
    # field has to match that name exactly.
    cited_dependency = d_source in {"structured", "span", "incident"}
    if cited_dependency:
        pool_views, pool_observations = split_pools_for_dependency(
            in_window_pools, d_fail, alerted,
        )
    else:
        pool_views = []
        _unused, pool_observations = split_pools_for_dependency(
            in_window_pools, "", alerted,
        )
    pools_without_dependency = bool(in_window_pools) and not pool_views
    slow_views = [
        v for v in log_views
        if _in_window(v["timestamp"], start, end)
        and _is_slow_query(v["record"])
        and _concerns(v, ds, alerted, timeout_names_ds=named_downstream)
    ]
    latency_views = [
        v for v in latency_views
        if _in_window(v["timestamp"], start, end) and _latency_elevated(v)
    ]
    # A golden-signals summary is the detector's conclusion. Cause support
    # uses the raw metric points behind it, and only those.
    raw_latency = dedupe_views([
        v for v in latency_views if not _is_derived_record(v["record"])
    ])
    pool_views = dedupe_views(pool_views)
    slow_views = dedupe_views(slow_views)
    timeout_views = [
        v for v in timeout_views if _in_window(v["timestamp"], start, end)
    ]
    if not timeout_views and ds:
        timeout_views = [
            v for v in log_views
            if _in_window(v["timestamp"], start, end)
            and _is_timeout_text(_raw_text(v["record"]))
            and _concerns(v, ds, alerted, timeout_names_ds=False)
        ]

    symptom_refs = [_ref(v, "timeout_observed", ds or alerted) for v in timeout_views[:1]]
    symptom_contribs: list[dict] = []
    symptom_score = 0
    if timeout_views:
        symptom_contribs.append(_contrib(
            "support", symptom_refs[0], SYMPTOM_TIMEOUT_DELTA, "direct", "direct",
        ))
        symptom_score += SYMPTOM_TIMEOUT_DELTA
    if latency_views:
        lat_ref = _ref(latency_views[0], "latency_elevated", ds or alerted)
        if not symptom_refs:
            symptom_refs.append(lat_ref)
        symptom_contribs.append(_contrib(
            "support", lat_ref, SYMPTOM_LATENCY_DELTA, "indirect", "indirect",
        ))
        symptom_score += SYMPTOM_LATENCY_DELTA
    symptom_score = _clamp(symptom_score)

    unavailable = _unavailable(evidence)
    contradictions: list[dict] = []
    unknowns: list[str] = []
    cause_refs: list[dict] = []
    contributions: list[dict] = []
    base = 0

    unsaturated = contradicting_pool_readings(evidence, start, end)
    if pool_views and unsaturated:
        # A pool-exhaustion candidate plus a gauge that shows the pool
        # is not saturated is an unresolved contradiction. The log does
        # not outrank that reading.
        conflict = unsaturated_pool_conflict(pool_views[0], unsaturated, "timeout")
        contradictions = conflict["contradictions"]
        unknowns.extend(conflict["unknowns"])
        base = conflict["base"]
        contributions = conflict["contributions"]
        statement = conflict["statement"]
        category = conflict["category"]
        name = conflict["hypothesis_name"]
        cause_refs = []
    elif pool_views and slow_views:
        # Both mechanisms are directly evidenced. Neither is the unique
        # immediate cause, so the narrowest claim is UNKNOWN.
        pool_ref = _ref(pool_views[0], "connection_pool_exhausted", ds)
        slow_ref = _ref(slow_views[0], "slow_query", ds)
        contradictions = [
            {"statement": "connection pool exhaustion", "evidence_refs": [pool_ref]},
            {"statement": "slow queries", "evidence_refs": [slow_ref]},
        ]
        unknowns.append(
            "which mechanism is immediate: connection pool exhaustion or slow queries"
        )
        base = CONFLICT_BASE
        contributions = [
            _contrib("contradiction", pool_ref, CONFLICT_EACH, "direct", "contradiction"),
            _contrib("contradiction", slow_ref, CONFLICT_EACH, "direct", "contradiction"),
        ]
        statement = "timeout observed; cause UNKNOWN"
        category = "unknown"
        name = "timeout_conflict"
        cause_refs = []
    elif pool_views and (ds or _record_downstream(pool_views[0]["record"])):
        series_ref = _contradicting_pool_series(evidence, ds or alerted, start, end)
        if series_ref is not None:
            pool_ref = _ref(pool_views[0], "connection_pool_exhausted", ds)
            contradictions = [
                {"statement": "connection pool exhaustion", "evidence_refs": [pool_ref]},
                {"statement": "normalized metric series contradicts the pool record", "evidence_refs": [series_ref]},
            ]
            unknowns.append("an aligned metric series contradicts the pool record")
            base = CONFLICT_BASE
            contributions = [
                _contrib("contradiction", pool_ref, CONFLICT_EACH, "direct", "contradiction"),
                _contrib("contradiction", series_ref, CONFLICT_EACH, "direct", "contradiction"),
            ]
            statement = "timeout observed; cause UNKNOWN"
            category = "unknown"
            name = "metric_series_contradiction"
            cause_refs = []
        else:
            raw_pools = [
                v for v in pool_views if not _is_derived_record(v["record"])
            ]
            pool_record = raw_pools[0]["record"]
            cause_refs = [
                _ref(v, "connection_pool_exhausted", ds) for v in raw_pools
            ]
            _score, contributions = score_raw_support(cause_refs)
            # The owner is a cited record's downstream field when one is set.
            # A timeout line that names a downstream still counts. When no
            # cited record names one, the cause names the service.
            field_record = next(
                (v["record"] for v in raw_pools if _record_downstream(v["record"])),
                None,
            )
            if field_record is not None:
                statement, named = _pool_owner_statement(field_record)
            elif named_downstream and ds:
                statement = f"connection pool for {ds} exhausted"
                named = ds
            else:
                statement, named = _pool_owner_statement(pool_record)
                unknowns.append(DOWNSTREAM_UNKNOWN)
            category = "connection_pool_exhaustion"
            name = "connection_pool_exhaustion"
            if named:
                unknowns.append(f"why {named} refuses connections")
    elif slow_views and ds:
        raw_slow = [
            v for v in slow_views if not _is_derived_record(v["record"])
        ]
        cause_refs = [_ref(v, "slow_query", ds) for v in raw_slow]
        _score, contributions = score_raw_support(cause_refs)
        statement = f"slow queries on {ds}"
        category = "slow_queries"
        name = "slow_queries"
        unknowns.append(f"why queries on {ds} are slow")
    elif raw_latency and ds:
        cause_refs = [
            _ref(v, "latency_elevated", ds) for v in raw_latency
        ]
        _score, contributions = score_latency_unknown(cause_refs)
        statement = f"{ds} latency elevated; cause UNKNOWN"
        category = "unknown"
        name = "latency_elevated_unknown"
        unknowns.append(f"why {ds} latency is elevated")
    elif pools_without_dependency:
        base = MISSING_CAUSE
        statement = "timeout observed; cause UNKNOWN"
        category = "unknown"
        name = "pool_dependency_mismatch"
        unknowns.append("failing dependency not identified")
    else:
        required_owner = d_fail if d_source in {"structured", "span", "incident"} else (ds or alerted)
        supported = None if d_source in {"missing", "text"} else _supporting_pool_series(
            evidence, required_owner, start, end,
        )
        if supported is not None:
            ref = dict(supported["ref"])
            ref["signal"] = "connection_pool_exhausted"
            ref["service"] = supported.get("service") or required_owner
            cause_refs = [ref]
            _score, contributions = score_raw_support(cause_refs)
            named = supported.get("service") or required_owner
            statement = f"connection pool exhausted on {named}"
            category = "connection_pool_exhaustion"
            name = "connection_pool_exhaustion"
            unknowns.append(f"why {named} refuses connections")
            unknowns.append(DOWNSTREAM_UNKNOWN)
        else:
            base = MISSING_CAUSE
            statement = "timeout observed; cause UNKNOWN"
            category = "unknown"
            name = "timeout_unknown"
            failed = tool_search_errors(evidence)
            if failed and not successful_observation(evidence):
                # Every search that could have bound a cause errored. Absence
                # of records is not a finding.
                for row in failed:
                    unknowns.append(
                        f"search did not happen: {row['tool']}: {row['error']}"
                    )
            else:
                if d_source == "missing":
                    unknowns.append("failing dependency not identified")
                elif not ds:
                    unknowns.append("downstream not named by an in-window raw record")
                else:
                    unknowns.append(f"no in-window mechanism record for {ds}")
                failed_keys = {row["evidence_key"] for row in failed}
                for src in unavailable:
                    if src in failed_keys:
                        continue
                    unknowns.append(f"unavailable: {src}")
                for row in failed:
                    unknowns.append(
                        f"search did not happen: {row['tool']}: {row['error']}"
                    )

    cause_score = _clamp(base + sum(c["delta"] for c in contributions))
    if category == "unknown" or contradictions:
        cause_score = min(cause_score, 59)
    if cause_score >= 60 and not _direct_support(contributions):
        cause_score = 59
    if contradictions and cause_score >= 60:
        cause_score = 59

    # Reconcile a cap so base + contributions still equals the final score.
    raw = base + sum(c["delta"] for c in contributions)
    if cause_score != _clamp(raw):
        contributions = list(contributions) + [{
            "kind": "cap",
            "source": "unknown_or_contradiction",
            "delta": cause_score - raw,
            "relevance": "cap",
            "strength": "none",
            "signal": "",
        }]

    symptom = {
        "statement": "timeout observed",
        "confidence": symptom_score,
        "evidence_refs": symptom_refs,
    }
    reasoning = _reasoning(
        statement, ds, alerted, category,
        bool(pool_views), bool(slow_views), bool(latency_views),
    )
    provenance = {
        "model": "cited_evidence_v1",
        "alignment_window_minutes": ALIGNMENT_WINDOW_MINUTES,
        "base": float(base),
        "contributions": contributions,
        "final_confidence": cause_score,
        "symptom_base": 0.0,
        "symptom_contributions": symptom_contribs,
        "symptom_confidence": symptom_score,
    }
    decision = {
        "hypothesis_name": name,
        "statement": statement,
        "category": category,
        "cause_confidence": cause_score,
        "cause_refs": cause_refs,
        "contradictions": contradictions,
        "unknowns": unknowns,
        "symptom": symptom,
        "reasoning": reasoning,
        "provenance": provenance,
        "downstream": ds,
    }
    if pool_observations:
        decision["observations"] = pool_observations
    from supervisor.helpers.cause_binding import require_query_tie
    return require_query_tie(decision, evidence)


def citations_for_bound_result(result: dict) -> list[dict] | None:
    """Citations drawn from the refs that chose the winner.

    Returns None when this result was not re-scored from cited refs, so the
    legacy keyword citer stays in place.
    """
    if not result.get("_evidence_bound_cause"):
        return None
    cause_raw = result.get("cause")
    symptom_raw = result.get("symptom")
    cause: dict = cause_raw if isinstance(cause_raw, dict) else {}
    symptom: dict = symptom_raw if isinstance(symptom_raw, dict) else {}
    items: list[tuple[str, dict]] = []
    for ref in cause.get("evidence_refs") or []:
        if isinstance(ref, dict):
            items.append((str(cause.get("statement") or ""), ref))
    for group in cause.get("contradictions") or []:
        if not isinstance(group, dict):
            continue
        claim = f"conflicting evidence: {group.get('statement', '')}"
        for ref in group.get("evidence_refs") or []:
            if isinstance(ref, dict):
                items.append((claim, ref))
    for ref in symptom.get("evidence_refs") or []:
        if isinstance(ref, dict):
            items.append((str(symptom.get("statement") or ""), ref))
    citations = []
    for i, (claim, ref) in enumerate(items, 1):
        tool = str(ref.get("tool") or "evidence")
        citations.append({
            "claim": claim,
            "source": tool,
            "evidence": str(ref.get("signal") or ""),
            "timestamp": str(ref.get("timestamp") or ""),
            "confidence": 1.0,
            "citation_id": f"{tool}:{i}",
            "sequence_order": ref.get("sequence_order"),
            "locator": ref.get("locator"),
            "service": ref.get("service"),
            "signal": ref.get("signal"),
        })
    return citations


def attach_cited_outputs(result: dict, evidence: dict, receipts: Any) -> None:
    """Copy cited worker results onto those receipts' output.

    ``_evidence_snapshot`` stays a bool-per-key map. ``RECEIPT_CAPTURE_OUTPUT``
    stays off for uncited calls. A cited ref still resolves: its locator path
    walks the receipt output, which is the worker result.
    """
    if not result.get("_evidence_bound_cause") or not isinstance(evidence, dict):
        return
    wanted: set[int] = set()
    cause_raw = result.get("cause")
    symptom_raw = result.get("symptom")
    cause: dict = cause_raw if isinstance(cause_raw, dict) else {}
    symptom: dict = symptom_raw if isinstance(symptom_raw, dict) else {}
    blobs = [cause.get("evidence_refs") or [], symptom.get("evidence_refs") or []]
    for group in cause.get("contradictions") or []:
        if isinstance(group, dict):
            blobs.append(group.get("evidence_refs") or [])
    for refs in blobs:
        for ref in refs:
            if isinstance(ref, dict) and isinstance(ref.get("sequence_order"), int):
                wanted.add(ref["sequence_order"])
    if not wanted:
        return
    by_seq: dict[int, dict] = {}
    for val in evidence.values():
        if isinstance(val, dict) and isinstance(val.get("_receipt_sequence_order"), int):
            by_seq[val["_receipt_sequence_order"]] = val
    items = getattr(receipts, "receipts", None)
    if items is None:
        return
    for rec in items:
        seq = getattr(rec, "sequence_order", None)
        if not isinstance(seq, int):
            continue
        blob = by_seq.get(seq)
        if blob is None or seq not in wanted:
            continue
        if getattr(rec, "output", None) is None:
            rec.output = blob


def resolve_evidence_ref(
    ref: dict,
    evidence: dict,
    receipts: list[dict] | None = None,
) -> dict | None:
    """Return the raw record named by ``ref``, or None if it does not resolve.

    The record is taken from the evidence blob stamped with the receipt
    ``sequence_order`` (the worker result). When that blob was also stored
    on the receipt output, either source matches.
    """
    if not isinstance(ref, dict):
        return None
    locator = ref.get("locator")
    if not isinstance(locator, dict):
        return None
    path = locator.get("path")
    if not isinstance(path, list) or not path:
        return None
    seq = ref.get("sequence_order")
    key = locator.get("evidence_key")
    blob = None
    if isinstance(evidence, dict) and key in evidence and isinstance(evidence[key], dict):
        candidate = evidence[key]
        if candidate.get("_receipt_sequence_order") == seq:
            blob = candidate
    if blob is None and receipts:
        for rec in receipts:
            if not isinstance(rec, dict):
                continue
            if rec.get("sequence_order") == seq and isinstance(rec.get("output"), dict):
                blob = rec["output"]
                break
    if blob is None:
        return None
    node: Any = blob
    for part in path:
        if isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node):
            node = node[part]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    if not isinstance(node, dict):
        return None
    return node


def ref_in_window(ref: dict, incident: dict) -> bool:
    start, end = _incident_bounds(incident or {})
    return _in_window(str(ref.get("timestamp") or ""), start, end)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _contrib(
    kind: str,
    ref: dict,
    delta: int,
    relevance: str,
    strength: str,
    *,
    part: str = "",
) -> dict:
    source = f"seq={ref.get('sequence_order')}:{ref.get('signal')}"
    if part:
        source = f"{source}:{part}"
    return {
        "kind": kind,
        "source": source,
        "part": part,
        "delta": delta,
        "relevance": relevance,
        "strength": strength,
        "signal": ref.get("signal") or "",
        "sequence_order": ref.get("sequence_order"),
        "evidence_class": ref.get("evidence_class") or "",
    }


def _is_derived_record(record: dict) -> bool:
    """A tool's conclusion, not an observation.

    Anomaly flags, ``anomaly_type``, ``pattern``, vendor problem records,
    summaries, notes, and verdicts are derived. A log line, a metric point
    with a value, or a change event is raw even when a sibling field on
    the payload is a label.
    """
    if not isinstance(record, dict):
        return True
    if _raw_text(record).strip():
        return False
    value = record.get("value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return False
    if record.get("change_type") or record.get("scheduled_start"):
        return False
    conclusion = (
        "anomaly_type", "anomaly_detected", "pattern", "alert_pattern",
        "summary", "note", "notes", "annotation", "annotations",
        "verdict", "problem", "problem_id", "root_cause_hint", "hint",
        "comment", "comments",
    )
    for key in conclusion:
        val = record.get(key)
        if isinstance(val, str) and val.strip() and not is_placeholder(val):
            return True
        if key == "anomaly_detected" and val is True:
            return True
    # A golden-signals latency summary is the detector's conclusion.
    if "p95" in record and "value" not in record:
        return True
    return False


def _evidence_class(record: dict) -> str:
    return "derived" if _is_derived_record(record) else "raw"


def score_raw_support(refs: list[dict]) -> tuple[int, list[dict]]:
    """Confidence from in-window raw refs only.

    The first raw ref contributes ``RAW_IN_WINDOW + RAW_OBSERVATION +
    RAW_MECHANISM``. Each further agreeing raw ref contributes
    ``EXTRA_RAW``. Derived refs add 0. The sum is capped at ``RAW_CAP``.
    """
    raw_refs = [ref for ref in refs if ref.get("evidence_class") != "derived"]
    contributions: list[dict] = []
    if not raw_refs:
        return 0, contributions
    first = raw_refs[0]
    for part, delta in (
        ("in_window", RAW_IN_WINDOW),
        ("raw_observation", RAW_OBSERVATION),
        ("direct_mechanism", RAW_MECHANISM),
    ):
        contributions.append(_contrib("support", first, delta, "direct", "direct", part=part))
    for ref in raw_refs[1:]:
        contributions.append(_contrib(
            "support", ref, EXTRA_RAW, "direct", "direct", part="additional_raw",
        ))
    total = sum(item["delta"] for item in contributions)
    if total > RAW_CAP:
        contributions.append({
            "kind": "cap",
            "source": "raw_cap",
            "part": "cap",
            "delta": RAW_CAP - total,
            "relevance": "cap",
            "strength": "none",
            "signal": "",
            "sequence_order": None,
            "evidence_class": "",
        })
        total = RAW_CAP
    return total, contributions


def score_latency_unknown(refs: list[dict]) -> tuple[int, list[dict]]:
    """Elevated latency is not a mechanism.

    The first raw point contributes ``LATENCY_UNKNOWN_DELTA``. Each further
    raw point contributes ``EXTRA_RAW``. Derived points add 0. The sum stays
    below 60, so the cause does not bind.
    """
    raw_refs = [ref for ref in refs if ref.get("evidence_class") != "derived"]
    if not raw_refs:
        return 0, []
    contributions = [_contrib(
        "support", raw_refs[0], LATENCY_UNKNOWN_DELTA, "indirect", "indirect",
        part="latency_elevated",
    )]
    for ref in raw_refs[1:]:
        contributions.append(_contrib(
            "support", ref, EXTRA_RAW, "indirect", "indirect", part="additional_raw",
        ))
    total = sum(item["delta"] for item in contributions)
    ceiling = 59
    if total > ceiling:
        contributions.append({
            "kind": "cap",
            "source": "unknown_cap",
            "part": "cap",
            "delta": ceiling - total,
            "relevance": "cap",
            "strength": "none",
            "signal": "",
            "sequence_order": None,
            "evidence_class": "",
        })
        total = ceiling
    return total, contributions


def dedupe_views(views: list[dict]) -> list[dict]:
    """One copy of each observation.

    Two searches that return the same line are one record. A later line,
    or another metric point, is a different record.
    """
    seen: set[tuple] = set()
    chosen: list[dict] = []
    for view in views:
        raw = view.get("record")
        record: dict = raw if isinstance(raw, dict) else {}
        value = record.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            value = ""
        key = (
            str(view.get("timestamp") or ""),
            _raw_text(record),
            str(record.get("service") or ""),
            str(record.get("name") or record.get("metric") or ""),
            value,
            str(record.get("change_type") or record.get("type") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        chosen.append(view)
    return chosen


def _direct_support(contributions: list[dict]) -> bool:
    return any(
        c.get("kind") == "support" and c.get("strength") == "direct" and c.get("delta", 0) > 0
        for c in contributions
    )


def _ref(view: dict, signal: str, service: str) -> dict:
    """Cite the record's own service, or a downstream that same record names.

    ``service`` is not copied onto the ref. A different record's downstream
    (the timeout line naming payment-db) must not relabel this one.
    """
    raw_record = view.get("record")
    record: dict = raw_record if isinstance(raw_record, dict) else {}
    own = ""
    raw = record.get("service")
    if isinstance(raw, str) and raw.strip() and not is_placeholder(raw):
        own = raw.strip()
    else:
        own = _extract_downstream(_raw_text(record))
    ref = {
        "sequence_order": view.get("sequence_order"),
        "tool": view.get("tool") or "",
        "locator": view.get("locator"),
        "service": own,
        "timestamp": view.get("timestamp") or "",
        "signal": signal,
        "evidence_class": _evidence_class(record),
    }
    qid = view.get("query_id")
    if isinstance(qid, str) and qid:
        ref["query_id"] = qid
    return ref


def _reasoning(
    statement: str,
    ds: str,
    alerted: str,
    category: str,
    pool: bool,
    slow: bool,
    latency: bool,
) -> str:
    bits = [
        (
            f"Timeline review of in-window raw records for "
            f"{alerted or 'the alerted service'} waiting on {ds or 'the downstream'}."
        ),
        f"The cause statement is: {statement}.",
    ]
    if category == "connection_pool_exhaustion":
        bits.append(
            "A raw connection-pool exhaustion record precedes the timeout. "
            "Why the pool exhausted is not established by that record."
        )
    elif category == "slow_queries":
        bits.append(
            "A raw query-level record names slow queries. "
            "Database latency alone was not used."
        )
    elif pool and slow:
        bits.append(
            "Raw records support both connection pool exhaustion and slow queries, "
            "so the immediate cause cannot be distinguished and stays UNKNOWN."
        )
    elif latency and category == "unknown":
        bits.append(
            "Latency is elevated against its baseline before the timeout, "
            "but no connection-pool or slow-query record identifies the cause."
        )
    else:
        bits.append(
            "No aligned raw record identifies a mechanism, so the cause stays UNKNOWN."
        )
    return " ".join(bits)


def _incident_bounds(incident: dict) -> tuple[datetime | None, datetime | None]:
    start = _parse_ts(
        incident.get("start_time")
        or incident.get("created_at")
        or incident.get("timestamp")
        or incident.get("startsAt")
        or ""
    )
    end = _parse_ts(
        incident.get("end_time")
        or incident.get("resolved_at")
        or incident.get("endsAt")
        or ""
    )
    if start and not end:
        end = start
    return start, end


def _parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _in_window(ts: str, start: datetime | None, end: datetime | None) -> bool:
    if start is None or end is None:
        return False
    parsed = _parse_ts(ts)
    if parsed is None:
        return False
    w = timedelta(minutes=ALIGNMENT_WINDOW_MINUTES)
    return (start - w) <= parsed <= (end + w)


def _raw_text(record: dict) -> str:
    """Raw line text only. Summary, note, and annotation fields are not evidence."""
    parts = []
    for key in ("message", "_raw", "exception"):
        if key in _NON_EVIDENCE_FIELDS:
            continue
        val = record.get(key)
        if isinstance(val, str) and not is_placeholder(val):
            parts.append(val)
    return "\n".join(parts)


def _max_pool_prefix_allowed(before: str) -> bool:
    """True when the word before max/maximum may introduce a pool.

    The slot is the single word before "max" or "maximum". Empty, the
    start of a clause, and punctuation with a space after it are
    allowed. A hyphen, underscore, or period glued to max joins the
    word before it, and that word is the slot. A noun is not.
    """
    if before and before[-1] in "-_.":
        found = re.search(r"[A-Za-z0-9]+$", before[:-1])
        if not found:
            return False
        return _max_pool_lookback_word(found.group(0))
    trimmed = before.rstrip(" \t\r\n")
    if not trimmed:
        return True
    if not trimmed[-1].isalnum():
        return True
    found = re.search(r"[A-Za-z0-9]+$", trimmed)
    if not found:
        return True
    return _max_pool_lookback_word(found.group(0))


def _max_pool_lookback_word(word: str) -> bool:
    token = word.lower()
    return (
        token in _LOG_LEVEL
        or token in _ALLOWED_POOL_PREFIX
        or token in _MAX_POOL_FUNCTION_WORDS
    )


def _pool_mention_allowed(token: str, before: str = "") -> bool:
    """True when this pool token is on the connection-pool allow list."""
    separated = re.search(r"[\s._-]", token) is not None
    compact = re.sub(r"[\s._-]+", "", token).lower()
    if not separated and compact in _POOL_CLASS:
        return True
    prefix = re.match(r"^(.*?)[\s._-]*pool$", token, re.I)
    word = (prefix.group(1) if prefix else "").lower()
    if word in {"max", "maximum"}:
        return _max_pool_prefix_allowed(before)
    if word in _ALLOWED_POOL_PREFIX or word == "":
        return True
    # "ERROR pool.exhausted": the severity is not the pool's kind.
    return separated and word in _LOG_LEVEL


def _listed_pool_text(text: str) -> str:
    """Blank every pool token that is not on the allow list."""
    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        if _pool_mention_allowed(token, text[:match.start()]):
            return token
        return " " * len(token)

    return _POOL_MENTION.sub(repl, text)


def _is_pool(record: dict) -> bool:
    text = _raw_text(record)
    if text and any(p.search(_listed_pool_text(text)) for p in _POOL_PATTERNS):
        return True
    # Numeric pool gauges on the record itself (not a summary string).
    active = _num(record, "active", "active_connections", "pool_active", "db_pool_active")
    limit = _num(record, "max", "pool_max", "pool_size", "max_connections", "db_pool_max")
    waiting = _num(record, "pending", "waiting", "waiters", "pending_requests", "db_pool_pending")
    if active is not None and limit is not None and limit > 0 and active >= limit and (waiting or 0) > 0:
        return True
    return False


def _is_slow_query(record: dict) -> bool:
    text = _raw_text(record)
    if text and any(p.search(text) for p in _SLOW_QUERY_PATTERNS):
        return True
    if text:
        match = _DB_DURATION_STATEMENT.search(text)
        if match and float(match.group(1)) >= 1000:
            return True
    for key in _QUERY_DURATION_FIELDS:
        val = record.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 1000:
            return True
    for key in _QUERY_COUNT_FIELDS:
        val = record.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            return True
    return False


def _num(record: dict, *keys: str) -> float | None:
    for key in keys:
        val = record.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
    return None


def _record_field(record: dict, key: str) -> str:
    if not isinstance(record, dict):
        return ""
    raw = record.get(key)
    if isinstance(raw, str) and raw.strip() and not is_placeholder(raw):
        return raw.strip()
    return ""


def _record_downstream(record: dict) -> str:
    return _record_field(record, "downstream")


def _pool_owner_statement(record: dict, fallback_downstream: str = "") -> tuple[str, str]:
    """Statement for a pool record, and the component that owns the pool.

    The owner is the record's ``downstream`` field when that field is set,
    otherwise ``fallback_downstream``, otherwise the record's ``service``.
    A set ``downstream`` is named even when the caller is named too. The
    caller is never the only component in that case.
    """
    caller = _record_field(record, "service")
    field = _record_downstream(record)
    if field:
        if caller and caller != field:
            return f"{caller}'s connection pool to {field} exhausted", field
        return f"connection pool for {field} exhausted", field
    if fallback_downstream:
        return f"connection pool for {fallback_downstream} exhausted", fallback_downstream
    if caller:
        return f"connection pool exhausted on {caller}", caller
    return "connection pool exhausted", ""


def _is_timeout_text(text: str) -> bool:
    lowered = (text or "").lower()
    return "timeout" in lowered or "timed out" in lowered


def _extract_downstream(text: str) -> str:
    if not text:
        return ""
    for pattern in _DOWNSTREAM_PATTERNS:
        match = pattern.search(text)
        if match:
            token = match.group(1).strip().rstrip(".,;\"'")
            if token and token.lower() not in {"timeout", "connection", "upstream"}:
                return token
    return ""


def _token_in(token: str, text: str) -> bool:
    if not token or not text:
        return False
    return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", text, re.I) is not None


def _downstream(log_views: list[dict], start, end) -> tuple[str, list[dict]]:
    named: list[tuple[str, dict]] = []
    for view in log_views:
        if not _in_window(view["timestamp"], start, end):
            continue
        text = _raw_text(view["record"])
        if not _is_timeout_text(text):
            continue
        ds = _extract_downstream(text)
        # A downstream field on the log record is raw data, not a summary.
        field = view["record"].get("downstream")
        if isinstance(field, str) and field.strip():
            ds = ds or field.strip()
        if ds:
            named.append((ds, view))
    if not named:
        return "", []
    # Stable: first in-window timeout that names a downstream.
    ds = named[0][0]
    views = [v for name, v in named if name == ds]
    return ds, views


def _concerns(view: dict, ds: str, alerted: str, timeout_names_ds: bool) -> bool:
    if not ds:
        return False
    record = view["record"]
    text = _raw_text(record)
    if _token_in(ds, text):
        return True
    if str(record.get("service") or "") == ds:
        return True
    field = record.get("downstream")
    if isinstance(field, str) and field == ds and (not text or _token_in(field, text) or not text):
        return True
    svc = str(record.get("service") or "")
    if timeout_names_ds and svc in {alerted, ds, ""}:
        other = _extract_downstream(text)
        if other and other != ds:
            return False
        # Pool/query line on the alerted service, tied to ds by the timeout line.
        if _is_pool(record) or _is_slow_query(record):
            return True
    return False


def _latency_elevated(view: dict) -> bool:
    record = view["record"]
    p95 = record.get("p95")
    baseline = record.get("baseline_p95")
    if isinstance(p95, (int, float)) and isinstance(baseline, (int, float)) and baseline > 0:
        return float(p95) > float(baseline) * LATENCY_ELEVATION_FACTOR
    value = record.get("value")
    base = view.get("baseline")
    if isinstance(value, (int, float)) and isinstance(base, (int, float)) and base > 0:
        return float(value) > float(base) * LATENCY_ELEVATION_FACTOR
    return False


def _iter_logs(evidence: dict, fallback: list[dict]) -> list[dict]:
    found = False
    views: list[dict] = []
    for key, val in evidence.items():
        if str(key).startswith("_") or not isinstance(val, dict):
            continue
        if tool_search_error(val) is not None:
            continue
        results, prefix = _log_list(val)
        if results is None:
            continue
        found = True
        seq = val.get("_receipt_sequence_order")
        tool = str(val.get("_receipt_tool") or "")
        query_id = engine_query_id(val)
        for i, entry in enumerate(results):
            if not isinstance(entry, dict):
                continue
            ts = entry.get("_time") or entry.get("timestamp") or entry.get("ts") or ""
            view = {
                "record": entry,
                "timestamp": str(ts),
                "sequence_order": seq if isinstance(seq, int) else entry.get("_receipt_sequence_order"),
                "tool": tool or str(entry.get("_receipt_tool") or ""),
                "locator": {"evidence_key": key, "path": prefix + [i]},
            }
            if query_id:
                view["query_id"] = query_id
            views.append(view)
    if found:
        return views
    for i, entry in enumerate(fallback or []):
        if not isinstance(entry, dict):
            continue
        ts = entry.get("_time") or entry.get("timestamp") or entry.get("ts") or ""
        views.append({
            "record": entry,
            "timestamp": str(ts),
            "sequence_order": entry.get("_receipt_sequence_order"),
            "tool": str(entry.get("_receipt_tool") or ""),
            "locator": entry.get("_locator") or {"evidence_key": "logs", "path": ["results", i]},
        })
    return views


def _log_list(val: dict) -> tuple[list | None, list]:
    logs = val.get("logs")
    if isinstance(logs, dict) and isinstance(logs.get("results"), list):
        return logs["results"], ["logs", "results"]
    if isinstance(logs, list):
        return logs, ["logs"]
    results = val.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        if "message" in results[0] or "_raw" in results[0]:
            return results, ["results"]
    return None, []


def _iter_latency(evidence: dict, signals: dict, metrics: dict) -> list[dict]:
    views: list[dict] = []
    saw_signal = False
    saw_metric = False
    for key, val in evidence.items():
        if str(key).startswith("_") or not isinstance(val, dict):
            continue
        if tool_search_error(val) is not None:
            continue
        seq = val.get("_receipt_sequence_order")
        tool = str(val.get("_receipt_tool") or "")
        sig = val.get("signals")
        if isinstance(sig, dict) and isinstance(sig.get("golden_signals"), dict):
            latency = sig["golden_signals"].get("latency")
            if isinstance(latency, dict) and not is_placeholder(latency):
                saw_signal = True
                ts = str(sig.get("anomaly_start") or sig.get("timestamp") or "")
                record = dict(latency)
                view = {
                    "record": record,
                    "timestamp": ts,
                    "sequence_order": seq if isinstance(seq, int) else None,
                    "tool": tool,
                    "locator": {"evidence_key": key, "path": ["signals", "golden_signals", "latency"]},
                    "baseline": latency.get("baseline_p95"),
                }
                if engine_query_id(val):
                    view["query_id"] = engine_query_id(val)
                views.append(view)
        met = val.get("metrics")
        if isinstance(met, dict) and isinstance(met.get("metrics"), list):
            baseline = met.get("baseline")
            if is_placeholder(baseline):
                baseline = None
            for i, point in enumerate(met["metrics"]):
                if not isinstance(point, dict) or is_placeholder(point.get("value")):
                    continue
                name = str(point.get("name") or point.get("metric") or "")
                if is_placeholder(name):
                    name = ""
                if name and not re.search(r"latency|response_time|duration", name, re.I):
                    continue
                saw_metric = True
                view = {
                    "record": point,
                    "timestamp": str(point.get("timestamp") or point.get("_time") or ""),
                    "sequence_order": seq if isinstance(seq, int) else None,
                    "tool": tool,
                    "locator": {"evidence_key": key, "path": ["metrics", "metrics", i]},
                    "baseline": baseline,
                }
                if engine_query_id(val):
                    view["query_id"] = engine_query_id(val)
                views.append(view)
    if saw_signal or saw_metric:
        # Keep the raw points. A golden-signals summary may sit beside them;
        # callers that score a cause skip that summary.
        return views
    # Fallback when the caller passed already-extracted structures.
    gs = (signals or {}).get("golden_signals") or {}
    latency = gs.get("latency") if isinstance(gs, dict) else None
    if isinstance(latency, dict):
        views.append({
            "record": dict(latency),
            "timestamp": str((signals or {}).get("anomaly_start") or ""),
            "sequence_order": (signals or {}).get("_receipt_sequence_order"),
            "tool": str((signals or {}).get("_receipt_tool") or ""),
            "locator": {"evidence_key": "signals", "path": ["golden_signals", "latency"]},
            "baseline": latency.get("baseline_p95"),
        })
    return views[:1]


def _fmt_gauge(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _gauge(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _object_gauges(obj: dict) -> tuple[float | None, float | None, float | None]:
    """active, idle, max from one pool object. Field names vary by worker."""
    active = None
    for key in ("active", "active_connections", "pool_active", "db_pool_active"):
        active = _gauge(obj.get(key))
        if active is not None:
            break
    idle = None
    for key in ("idle", "idle_connections", "pool_idle", "db_pool_idle"):
        idle = _gauge(obj.get(key))
        if idle is not None:
            break
    limit = None
    for key in (
        "max", "pool_max", "pool_size", "max_connections",
        "max_pool_size", "db_pool_max",
    ):
        limit = _gauge(obj.get(key))
        if limit is not None:
            break
    return active, idle, limit


def _series_role(name: str) -> str | None:
    """active, idle, or max for a pool or connection gauge series."""
    low = (name or "").lower().replace("-", "_")
    if not re.search(r"pool|connection", low):
        return None
    if "idle" in low:
        return "idle"
    if re.search(
        r"(^|_)(max|limit|capacity)(_|$)|pool_size|max_pool|pool_max",
        low,
    ):
        return "max"
    if re.search(r"active|in_use|busy|used", low):
        return "active"
    return None


def _text_field(obj: dict, *keys: str) -> str:
    for key in keys:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def is_unsaturated_pool(record: dict) -> bool:
    """True when the gauge shows the pool is not saturated.

    Active is well below max, and many connections are idle. A series
    that only reports active, including one that later hits the max,
    is not this reading.
    """
    if not isinstance(record, dict):
        return False
    active = _gauge(record.get("active"))
    idle = _gauge(record.get("idle"))
    limit = _gauge(record.get("max"))
    if active is None or idle is None or limit is None or limit <= 0:
        return False
    return active <= limit * 0.5 and idle >= active and idle >= limit * 0.25 and idle > 0


def pool_reading_text(record: dict) -> str:
    return (
        f"pool not saturated: active {_fmt_gauge(float(record['active']))}, "
        f"idle {_fmt_gauge(float(record['idle']))}, "
        f"max {_fmt_gauge(float(record['max']))}"
    )


def _pool_reading_view(
    active: float,
    idle: float,
    limit: float,
    *,
    ts: str,
    seq: Any,
    tool: str,
    locator: dict,
    service: str,
) -> dict:
    record: dict[str, Any] = {
        "name": "db_connection_pool",
        "value": active,
        "active": active,
        "idle": idle,
        "max": limit,
        "timestamp": ts,
    }
    if service:
        record["service"] = service
    return {
        "record": record,
        "kind": "metric",
        "pool_reading": True,
        "timestamp": ts,
        "sequence_order": seq if isinstance(seq, int) else None,
        "tool": tool,
        "locator": locator,
    }


def _emit_pool_object(
    obj: dict,
    *,
    ts: str,
    seq: Any,
    tool: str,
    locator: dict,
    service: str,
) -> dict | None:
    active, idle, limit = _object_gauges(obj)
    if active is None or idle is None or limit is None:
        return None
    when = _text_field(obj, "timestamp", "_time", "ts") or ts
    return _pool_reading_view(
        active, idle, limit,
        ts=when, seq=seq, tool=tool, locator=locator, service=service,
    )


def _pool_objects(val: dict, key: str, seq: Any, tool: str, service: str) -> list[dict]:
    """db_connection_pool nested on the payload, on signals, or on golden signals."""
    found: list[dict] = []
    parents: list[tuple[dict, list]] = [(val, [])]
    signals = val.get("signals")
    if isinstance(signals, dict):
        parents.append((signals, ["signals"]))
        golden = signals.get("golden_signals")
        if isinstance(golden, dict):
            parents.append((golden, ["signals", "golden_signals"]))
    golden = val.get("golden_signals")
    if isinstance(golden, dict):
        parents.append((golden, ["golden_signals"]))
    for container, prefix in parents:
        parent_ts = _text_field(container, "anomaly_start", "timestamp", "_time")
        if not parent_ts:
            parent_ts = _text_field(val, "anomaly_start", "timestamp", "_time")
        for name in ("db_connection_pool", "connection_pool"):
            obj = container.get(name)
            if not isinstance(obj, dict):
                continue
            view = _emit_pool_object(
                obj,
                ts=parent_ts,
                seq=seq,
                tool=tool,
                locator={"evidence_key": key, "path": prefix + [name]},
                service=service or _text_field(container, "service") or _text_field(val, "service"),
            )
            if view is not None:
                found.append(view)
    return found


def _absorb_point(buckets: dict[str, dict], point: dict, path: list, fallback_max: float | None) -> None:
    if not isinstance(point, dict):
        return
    active, idle, limit = _object_gauges(point)
    if active is not None and idle is not None and (limit is not None or fallback_max is not None):
        ts = _text_field(point, "timestamp", "_time", "ts")
        slot = buckets.setdefault(ts, {})
        slot["active"] = active
        slot["idle"] = idle
        slot["max"] = limit if limit is not None else fallback_max
        slot.setdefault("path", path)
        return
    role = _series_role(str(point.get("name") or point.get("metric") or ""))
    value = _gauge(point.get("value"))
    if role is None or value is None:
        return
    ts = _text_field(point, "timestamp", "_time", "ts")
    slot = buckets.setdefault(ts, {})
    slot[role] = value
    if role == "active":
        slot.setdefault("path", path)


def _readings_from_buckets(
    buckets: dict[str, dict],
    *,
    fallback_max: float | None,
    seq: Any,
    tool: str,
    key: str,
    service: str,
) -> list[dict]:
    found = []
    for ts, slot in buckets.items():
        active = slot.get("active")
        idle = slot.get("idle")
        limit = slot.get("max")
        if limit is None:
            limit = fallback_max
        if active is None or idle is None or limit is None:
            continue
        found.append(_pool_reading_view(
            float(active), float(idle), float(limit),
            ts=ts,
            seq=seq,
            tool=tool,
            locator={"evidence_key": key, "path": slot.get("path") or ["metrics"]},
            service=service,
        ))
    return found


def _pool_series(val: dict, key: str, seq: Any, tool: str, service: str) -> list[dict]:
    """Flat Prometheus points, or a dict of named series with values."""
    blob = val.get("metrics")
    fallback = _gauge(val.get("pool_max"))
    buckets: dict[str, dict] = {}
    if isinstance(blob, list):
        for index, point in enumerate(blob):
            _absorb_point(buckets, point, ["metrics", index], fallback)
    elif isinstance(blob, dict):
        if fallback is None:
            fallback = _gauge(blob.get("pool_max"))
        nested = blob.get("metrics")
        if isinstance(nested, list):
            for index, point in enumerate(nested):
                _absorb_point(buckets, point, ["metrics", "metrics", index], fallback)
        else:
            for name, body in blob.items():
                if name in {"metrics", "baseline", "pattern", "pool_max", "note"}:
                    continue
                if isinstance(body, dict) and isinstance(body.get("values"), list):
                    series_max = _gauge(body.get("pool_max"))
                    if series_max is not None and fallback is None:
                        fallback = series_max
                    for index, point in enumerate(body["values"]):
                        if isinstance(point, dict):
                            named = dict(point)
                            named.setdefault("name", name)
                            _absorb_point(
                                buckets, named,
                                ["metrics", name, "values", index],
                                series_max if series_max is not None else fallback,
                            )
                elif isinstance(body, list):
                    for index, point in enumerate(body):
                        if isinstance(point, dict):
                            named = dict(point)
                            named.setdefault("name", name)
                            _absorb_point(
                                buckets, named, ["metrics", name, index], fallback,
                            )
    return _readings_from_buckets(
        buckets, fallback_max=fallback, seq=seq, tool=tool, key=key, service=service,
    )


def iter_pool_readings(evidence: dict | None) -> list[dict]:
    """Pool gauges from every shape workers return.

    Flat Prometheus series (a list of points, or a dict of named series)
    and a ``db_connection_pool`` object on the payload or nested under
    ``signals`` / golden signals. A failed search is not a reading.
    """
    found: list[dict] = []
    for key, val in (evidence or {}).items():
        if str(key).startswith("_") or not isinstance(val, dict):
            continue
        if tool_search_error(val) is not None:
            continue
        seq = val.get("_receipt_sequence_order")
        tool = str(val.get("_receipt_tool") or "")
        service = _text_field(val, "service")
        qid = engine_query_id(val)
        seen: set[tuple] = set()
        for view in (
            _pool_objects(val, str(key), seq, tool, service)
            + _pool_series(val, str(key), seq, tool, service)
        ):
            record = view["record"]
            identity = (
                view.get("timestamp") or "",
                record.get("active"),
                record.get("idle"),
                record.get("max"),
            )
            if identity in seen:
                continue
            seen.add(identity)
            if qid:
                view["query_id"] = qid
            found.append(view)
    return found


def contradicting_pool_readings(evidence: dict | None, start, end) -> list[dict]:
    """In-window readings that show the pool is not saturated.

    A reading with no timestamp was retrieved for this incident and
    still counts. A timestamp outside the alignment window does not.
    """
    chosen = []
    for view in iter_pool_readings(evidence):
        if not is_unsaturated_pool(view.get("record") or {}):
            continue
        ts = str(view.get("timestamp") or "")
        if ts and not _in_window(ts, start, end):
            continue
        chosen.append(view)
    return chosen


def unsaturated_pool_conflict(pool_view: dict, readings: list[dict], incident_type: str) -> dict:
    """Pool exhaustion contradicted by a gauge that is not saturated."""
    pool_ref = _ref(pool_view, "connection_pool_exhausted", "")
    contradictions = [
        {"statement": "connection pool exhaustion", "evidence_refs": [pool_ref]},
    ]
    contributions = [
        _contrib("contradiction", pool_ref, CONFLICT_EACH, "direct", "contradiction"),
    ]
    for reading in readings:
        ref = _ref(reading, "pool_not_saturated", "")
        contradictions.append({
            "statement": pool_reading_text(reading["record"]),
            "evidence_refs": [ref],
        })
        contributions.append(
            _contrib("contradiction", ref, CONFLICT_EACH, "direct", "contradiction"),
        )
    score = _clamp(CONFLICT_BASE + sum(item["delta"] for item in contributions))
    score = min(score, 59)
    label = incident_type or "incident"
    statement = (
        "timeout observed; cause UNKNOWN"
        if label == "timeout"
        else f"{label} observed; cause UNKNOWN"
    )
    return {
        "hypothesis_name": "pool_metric_contradiction",
        "statement": statement,
        "category": "unknown",
        "cause_confidence": score,
        "cause_refs": [],
        "contradictions": contradictions,
        "unknowns": ["a retrieved pool metric shows the pool is not saturated"],
        "contributions": contributions,
        "base": CONFLICT_BASE,
    }


def tool_search_error(payload: dict) -> str | None:
    """Error text when this tool result is a search that did not happen.

    ``{"error": ...}``, ``tool_status: "error"``, and a gateway or mcp
    exception are failed searches. They are not empty results.
    """
    if not isinstance(payload, dict):
        return None
    status = payload.get("tool_status")
    status_error = isinstance(status, str) and status.strip().lower() == "error"
    err_text = ""
    err = payload.get("error")
    if isinstance(err, str) and err.strip() and not is_placeholder(err):
        err_text = err.strip()
    for key in ("gateway_exception", "mcp_exception"):
        val = payload.get(key)
        if err_text:
            break
        if isinstance(val, str) and val.strip() and not is_placeholder(val):
            err_text = val.strip()
    if not status_error and not err_text:
        return None
    return err_text or "tool_status: error"


def tool_search_errors(evidence: dict | None) -> list[dict]:
    """Failed searches, with the tool name and the error text."""
    rows: list[dict] = []
    for key, val in (evidence or {}).items():
        if str(key).startswith("_") or not isinstance(val, dict):
            continue
        text = tool_search_error(val)
        if text is None:
            continue
        tool = val.get("tool") or val.get("_receipt_tool") or key
        rows.append({
            "evidence_key": str(key),
            "tool": str(tool),
            "error": text,
        })
    return rows


def successful_observation(evidence: dict | None) -> bool:
    """True when some tool result is not a failed search."""
    for key, val in (evidence or {}).items():
        if str(key).startswith("_") or not isinstance(val, dict):
            continue
        if tool_search_error(val) is None:
            return True
    return False


def _unavailable(evidence: dict) -> list[str]:
    names: list[str] = []
    for item in evidence.get("_sources_unavailable") or []:
        if isinstance(item, dict):
            names.append(str(item.get("source") or item.get("name") or "unknown"))
        else:
            names.append(str(item))
    for key, val in evidence.items():
        if str(key).startswith("_") or not isinstance(val, dict):
            continue
        if val.get("error"):
            names.append(str(key))
    # Stable, de-duplicated.
    seen = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen


def _clamp(value: float) -> int:
    return max(0, min(100, int(round(value))))
